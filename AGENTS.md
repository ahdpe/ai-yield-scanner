# Agent guide: AI Yield Scanner (LIVE)

Context for coding agents working on this repository. Read `README.md` for what the system does; this file covers how to work on it safely. The old paper-trading instructions are legacy; live trading is a different service and a different risk profile.

## The owner

- Speaks Russian. Reply in Russian; code comments and log messages are in Russian. Repository docs (`README.md`, this file) stay in English; `MANUAL.md` is legacy Russian and is not authoritative for the live service.
- Expects verified, working changes: test before restarting anything, and report what you checked and what you could not check.
- Commit and push only on explicit request. Never reset the portfolio, delete `state/`, change limits or move funds without asking.
- Use a feature branch for changes; do not commit directly to `main` unless the owner explicitly asks.

## Where it runs

Production is a single Linux VPS. The project is deployed with a local virtualenv and runs as two **systemd system services** (not `--user`):

| Unit | Process | Notes |
|---|---|---|
| `ai-yield-live.service` | `live_trader.py --interval 120` | Live Smart Bonding daemon; one instance only |
| `ai-yield-live-bot.service` | `telegram_bot.py` | Telegram UI; only one instance per bot token |

- `WorkingDirectory` is the project directory; `EnvironmentFile=state/live_config.env` supplies wallet/AI credentials to both units. Keep `state/live_config.env` mode `600`.
- The live units live in `/etc/systemd/system/` and are not installed from the repository root; the templates in `deploy/` use a `__PROJECT_DIR__` placeholder.
- The server working tree is synced from the owner's machine with `scp`. Do **not** run `git pull` on the server until the server-side changes have been reconciled with Git.
- Venice AI is reached through the local budget proxy at `127.0.0.1:8787` (shared with Hermes). Do not modify the proxy, Hermes or its `.env`.
- SSH is key-only (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`). Do not weaken it; if a change is needed, ask the owner and keep a second key session open.
- Other projects on the server are not part of this repository. Leave them alone.

## Non-negotiable safety rules

1. This is **live money**. Never print, log, commit or send wallet keys, API keys, Telegram tokens or admin IDs. The repository is public: do not add server IPs, home-directory paths, Telegram IDs, e-mail addresses or key material to code, docs or commit messages.
2. Never reset the portfolio, delete files in `state/`, change `LIVE_*` limits, or move funds without an explicit owner instruction.
3. Never start a second `live_trader.py` or `telegram_bot.py`. The trader has a `flock` (`state/live_trader.lock`); the bot relies on Telegram's 409 conflict, so a duplicate is an operational bug, not a fallback.
4. Do not run `paper_trader.py` on the production host. It is legacy, writes a different state file and shares the AI budget.
5. An order intent is persisted before submission. If an order result is ambiguous, entries and protective exits may pause until reconciliation; do not bypass this with a manual retry that could double the position.

## State and secrets

Everything mutable lives in `state/` (git-ignored):

| File | Purpose |
|---|---|
| `live_portfolio_state.json` | Live portfolio, positions, PnL, cash snapshot |
| `live_trader.log` | Event log shown by the bot's `/logs` |
| `ai_audits_cache.json` | Cached Venice verdicts |
| `ai_budget.json` | Venice paid calls used today (UTC) |
| `ai_budget_backup.json` | Command Code paid calls used today (UTC) |
| `ai_health.json` | Venice connectivity and emergency mode |
| `error_alerts.json`, `error_alerts_bot.json`, `service_exit_alerts.json` | Error-alert trackers |
| `live_config.env` | Wallet and AI credentials (mode 600) |
| `telegram_config.json` | Bot token and admin chat id (mode 600) |
| `live_trader.lock` | Single-instance lock |

`state_io.py` writes JSON atomically (`tempfile` + `os.replace` + `fsync`). `save_live_state()` also fsyncs the directory so an order intent is durable before submission. Do not replace these writes with plain `open(..., "w")`.

## Deploying a change

Before any restart:

1. `python3 -m py_compile *.py` locally.
2. Run throwaway mock tests **outside the repository**; they must not write to `state/`. Compare a checksum of `state/live_portfolio_state.json` before and after.
3. Back up the changed files and `state/live_portfolio_state.json` on the VPS (use the safety directory, not the repository).
4. Copy only the changed files with `scp`; run `venv/bin/python3 -m py_compile *.py` on the VPS.
5. Restart `ai-yield-live.service`. Restart the bot only if its code changed.
6. Watch the journal for one full tick with no Traceback:
   `journalctl -u ai-yield-live -n 40 --no-pager -o cat`
7. Verify `state.available_cash_usd` against the CLOB/on-chain balance and confirm that open positions are still `CONFIRMED` with no pending flags.

Do not use `systemctl --user` for the live units. Do not run `systemctl restart` while a second trader holds the lock.

## Current live mechanics

Limits live in `state/live_config.env` (production 2026-11-21: **30 slots × $50**, strategy limit **$1,500**, Venice **55/15**, Command Code **480/192**). Read them from the host, not from this file.

- **Entry ladder** (`get_entry_tier_requirements`): `<50%` filled → APR ≥ 35%, ≤ 45 d, confidence ≥ 75; `50–80%` → 50%, ≤ 21 d, ≥ 80; `>=80%` → 80%, ≤ 14 d, ≥ 85.
- **Quality guards:** risk score ≥ 80, 1h move < 1.0¢, 24h move < 2.5¢, top-500 Gamma universe, bid depth ≥ $250 within 2¢, max 40% long-horizon slots (> 21 d), one slot per `event_id`, max 2 slots per topic cluster.
- **Fast guard:** every 20 s `run_fast_stop_guard` re-reads books. Two consecutive breaches inside a 3-minute window are required, with a panic override for a deep gap and a > 3¢ spread treated as a book hole.
- **Take-profit** (`should_trigger_take_profit`): remaining APR < 20% with ≥ 10 days left, ≥ +1.8¢ bid (or ≥ $0.988), net ≥ $0.20 and ≥ 0.3¢/share after the SELL fee. Pending take-profit must not bypass the stop guard.
- **Adaptive re-audit** (`compute_reaudit_plan`): calm 24h via Command Code, watch 6h via Venice, stressed 1h via Venice. Exit needs two hard `SKIP` (confidence ≥ 75).
- **Budgets** are split per provider into entry/re-audit pools (`_read_budget_state` migrates the legacy single-counter file).

## Testing

There is no committed test suite. For changes to `live_trader.py`, `live_execution.py`, `polymarket_scanner.py` or `telegram_bot.py`:

- Use `unittest.mock` and synthetic books/positions. Cover at least: an unfilled SELL older than 60s, a SELL above the current best bid, a cancel that is not yet confirmed, a zero-fill SELL that disappears, a missing/invalid `endDate`, an early return with full slots, the tier ladder boundaries, and the stop-breach confirmation streak.
- Monkeypatch `save_live_state`, `notify_live`, `log_live_event`, `_safe_fetch_book`, `reconcile_position`, `fetch_polymarket_markets` and the SDK client; no network and no state writes.
- For the bot, assert `format_portfolio_page` lengths stay under 3800 and that `_parse_portfolio_callback` falls back safely for malformed `pf:` callbacks.
- For read-only API checks, use a separate script and do not place orders.

## Code map

- `live_trader.py`: daemon, guard rails, entry ladder, fast stop guard, take-profit, adaptive re-audits, state.
- `live_execution.py`: `read_execution`, fee calculation, conditional-token balance; all reconciliation is fail-closed.
- `polymarket_scanner.py`: Gamma/CLOB access, fee schedule parsing, quality filters, Smart Bonding scan. Missing or invalid `endDate` is rejected.
- `ai_analyzer.py`: Venice audit, 2/2 consensus, prompt v3, split daily budgets, health, Command Code failover, fail-closed.
- `telegram_bot.py`, `telegram_notifier.py`: read-only paginated UI (open/closed views, sorting, edit-in-place) and alert delivery (HTML with plain-text fallback, 4096-character limit).
- `error_alerts.py`: recurring-error tracker (alert after 20 min, reminders every 6h, recovery after 15 min quiet; `immediate=True` for code errors).
- `service_exit_alert.py`: stdlib-only systemd crash hook; keep it free of project imports.
- `state_io.py`: `STATE_DIR`, atomic JSON writes, HTML escaping (`esc`).
- `paper_trader.py`, `hyperliquid_scanner.py`, `run_scanner.py`: legacy paper/research code; do not use on the live host.

## AI and API notes (verified November 2026)

- **Venice** (OpenAI-compatible) via the local proxy. Entry consensus uses `claude-sonnet-5-5` + `grok-4-6` with `enable_web_search="on"`; re-audits use one model.
- **Command Code** is the failover: Claude through `/messages` (no `temperature`) and Grok through `/chat/completions`; fresh news via Google News RSS. It has a separate daily budget.
- **Polymarket Gamma:** `GET /markets?active=true&closed=false&...`; single market via `GET /markets/slug/{slug}` (returns closed markets too).
- **Polymarket CLOB:** `GET /book?token_id=`; occasional HTTP 403 is transient. `/fee-rate?token_id=` is used to validate fees before entry.
- **Polymarket Data API:** v1 retired; use v2 with snake_case fields. Trades are newest-first, taker-side only, with a lag of seconds.
- **SDK:** `polymarket-client` `SecureClient` (Deposit Wallet, pUSD, ERC-7739, V2/V3). It also exposes `redeem_positions()`; the bot does not call it yet.
- APIs change without notice. Check the provider docs instead of guessing.

## Known gaps

- An ambiguous order without an `order_id` still requires manual reconciliation; there is no automatic account-trade discovery yet. A partial SELL that vanishes is recovered, but a BUY is not.
- `redemption_pending` is recorded but never confirmed or cleared; auto-redeem is assumed.
- The Telegram bot has no second-instance lock. It also loads `state/live_config.env` (wallet key) even though it only needs the portfolio.
- The stop-loss can gap or have no bid; the 20s guard, panic override and re-quote loop retry but cannot guarantee a fill.
- `state/live_portfolio_state.json` is not backed up automatically and `state/live_trader.log` has no rotation (it grows ~1 MB / 2 days).
- `state.initial_deposit_usd` can lag behind `LIVE_TOTAL_DEPOSIT` after a limit change; config wins.
- `AI_MAX_CALLS_PER_DAY` in production is lower than the Command Code budget, so Venice exhaustion is expected and failover carries the day; watch `ai_budget_alerts.json` accordingly.
