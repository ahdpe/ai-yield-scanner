# AI Yield Scanner — Live Smart Bonding

A real-money **Polymarket Smart Bonding** bot with a two-model LLM risk audit (Venice AI, with Command Code failover). It scans high-probability outcomes, buys them near resolution, and protects open positions with a relative stop-loss. A Telegram bot provides a read-only portfolio view and alerts.

> **LIVE — REAL FUNDS.** This repository now runs a live trading bot. It holds a Polymarket wallet key in `state/live_config.env` on the production host. Never commit `state/`, never print keys or chat IDs, and never start a second copy of the trader or the bot. The old paper-trading system is legacy; see `deploy/legacy-paper/`.

## Strategy

Slots and limits are configured in `state/live_config.env` (not in the repo). Production currently runs **30 slots × $50** for a **$1,500** strategy limit; the defaults below describe the mechanics, not the current numbers.

- **Universe:** top-500 active Polymarket markets by 24h volume (Gamma API, paginated). The scan is skipped entirely when slots are full, cash is below $10 or the AI entry budget is exhausted.
- **Entry:** leading outcome **94.5¢–98.5¢**, expiry **0.5–45 days**, liquidity ≥ **$25,000**, book spread ≤ **1.5¢**, fee rate ≤ **0.06**, risk score ≥ **80**, and no "falling knife": 1h price move **< 1.0¢** and 24h move **< 2.5¢**. Sports and crypto price targets are excluded, as are markets without a valid `endDate`.
- **Entry ladder by fill ratio:** `<50%` filled → APR ≥ **35%**, expiry ≤ 45 days, AI confidence ≥ **75%**; `50–80%` → APR ≥ **50%**, ≤ 21 days, ≥ **80%**; `>=80%` → APR ≥ **80%**, ≤ 14 days, ≥ **85%**. This keeps a reserve for the best setups instead of tapering quality.
- **Quality guards:** at most **40%** of slots in long-horizon positions (> 21 days), at most **1 open slot per Gamma event** (`event_id`), bid depth ≥ **$250** within 2¢ of the best bid, and max **2 open slots per topic cluster**.
- **Risk:** relative stop-loss **-3.5¢** from entry (floor $0.85), checked every **20 s**. A breach must persist for **2 consecutive checks** inside a 3-minute window, unless it is a panic gap (bid ≤ stop − 3¢ and ask ≤ stop + 0.5¢). A hole wider than 3¢ at mid ≥ stop is treated as a book artifact, not a crash. 12h reopen cooldown.
- **Take-profit:** rotates capital out of a position whose remaining APR has fallen below **20%** with ≥ 10 days left, provided the bid rose ≥ 1.8¢ (or ≥ $0.988) and the net gain after the SELL fee is ≥ $0.20 and ≥ 0.3¢/share. It never fires on an incomplete entry or with an unknown fee schedule.
- **Execution:** `polymarket-client` `SecureClient` (Polymarket Deposit Wallet, `signature_type=3`, pUSD). The intent is persisted before the order is submitted; an ambiguous submission fails closed and requires reconciliation.

### Stop-loss behavior

A stop-loss SELL is placed one tick below the current best bid. If it is still unfilled after 60 seconds, or if the market has moved so that its limit is above the current best bid, the trader cancels it, waits for the cancellation to be confirmed by reconciliation, and re-quotes at the new best bid. A new SELL is never sent while an old one is still live or unconfirmed. Zero-fill SELLs that disappear from the book are recovered as `RECOVERED_CANCELLED` instead of leaving the slot stuck.

## AI audit

- Every entry requires **2/2 APPROVE** from `claude-sonnet-5-5` + `grok-4-6` (Venice web search enabled) with a confidence at least equal to the current entry tier (75/80/85%). Anything else is `SKIP` (fail-closed).
- **Adaptive re-audit:** calm positions (drawdown < 1¢, > 3 days left, no SKIP streak) every **24h via Command Code**; watch (drawdown 1–2¢ or ≤ 3 days) every **6h via Venice**; stressed (drawdown ≥ 2¢ or a SKIP streak) every **1h via Venice**. Two hard `SKIP` verdicts (confidence ≥ 75%) close the position.
- **Budget:** separate per-provider daily pools with entry/re-audit split. Production currently uses Venice **55/15** (total/reserve) and Command Code **480/192**. If both are unavailable or exhausted, new entries are blocked. Budget files are read with a soft migration from the older single-counter format.
- Prompt version 3: English prompt, Russian `summary`, market rules capped at 4,000 characters, verdict cache 6h for APPROVE and 24–36h for SKIP.

## Architecture

```
live_trader.py ──► polymarket_scanner.py (Gamma/CLOB candidate scan)
      │                 │
      │                 └─► ai_analyzer.py (Venice AI + Command Code failover)
      │
      ├─► live_execution.py (fill/fee reconciliation via SecureClient)
      ├─► error_alerts.py (recurring-error alerts)
      └─► state/live_portfolio_state.json (atomic state, fsync before orders)

telegram_bot.py (read-only UI, alerts) ──► state/live_portfolio_state.json
```

## Project layout

| File | Purpose |
|---|---|
| `live_trader.py` | Live daemon: guard rails, stop-loss, entries, re-audits, state |
| `live_execution.py` | Fill confirmation, fee calculation, conditional-token balance |
| `polymarket_scanner.py` | Gamma/CLOB access and Smart Bonding candidate scan |
| `ai_analyzer.py` | Venice audit, 2/2 consensus, budget, health, Command Code failover |
| `telegram_bot.py` | Read-only Telegram UI (portfolio, AI verdicts, logs, settings) |
| `telegram_notifier.py` | Alert delivery with HTML escaping and plain-text fallback |
| `error_alerts.py` | Recurring-error tracker (20 min to alert, 6h reminders, recovery) |
| `service_exit_alert.py` | stdlib-only systemd `ExecStopPost` crash hook |
| `state_io.py` | Atomic JSON writes and HTML escaping |
| `deploy/` | Live systemd units, config examples, deployment notes |
| `paper_trader.py`, `hyperliquid_scanner.py`, `run_scanner.py` | Legacy paper/research code; not used by the live service |

The live service needs `polymarket-client` and `py_clob_client` (see `requirements.txt`); the rest is the Python standard library (3.10+).

## Setup (production VPS)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

mkdir -p state
cp deploy/env.example state/live_config.env
chmod 600 state/live_config.env                 # fill in wallet + AI keys
cp deploy/telegram_config.example.json state/telegram_config.json
chmod 600 state/telegram_config.json            # fill in bot token + admin chat id

# Replace __PROJECT_DIR__ in the unit files with the project path.
cp deploy/ai-yield-live.service deploy/ai-yield-live-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ai-yield-live.service ai-yield-live-bot.service
systemctl is-active ai-yield-live.service ai-yield-live-bot.service
```

The units are **system** services (no `--user`). `EnvironmentFile` loads `state/live_config.env`; keep that file mode `600`. See `deploy/README.md` for details.

## State and secrets

Everything mutable lives in `state/` (git-ignored, never commit it):

| File | Purpose |
|---|---|
| `live_portfolio_state.json` | Live portfolio state, positions, PnL, cash snapshot |
| `live_trader.log` | Event log shown by the bot's `/logs` |
| `ai_audits_cache.json` | Cached Venice verdicts |
| `ai_budget.json`, `ai_budget_backup.json` | Paid AI calls used today (UTC) |
| `ai_health.json` | Venice connectivity and emergency mode |
| `error_alerts*.json`, `service_exit_alerts.json` | Error-alert trackers |
| `live_config.env` | Wallet and AI credentials (mode 600) |
| `telegram_config.json` | Bot token and admin chat id (mode 600) |
| `live_trader.lock` | Single-instance lock for the trader |

Secrets are never stored in git. The repository is public: do not add server IPs, home-directory paths, Telegram IDs, e-mail addresses or keys to code, docs or commit messages.

## Deploying a change

1. Run `python3 -m py_compile *.py`.
2. Run mock tests outside the repo; they must not write to `state/` (compare a checksum before and after).
3. Back up the changed files and `state/live_portfolio_state.json` on the VPS.
4. `scp` the changed files, run `venv/bin/python3 -m py_compile *.py` on the VPS.
5. `systemctl restart ai-yield-live.service` (restart the bot only if its code changed).
6. Watch the journal for one full tick with no Traceback, then verify `state.available_cash_usd` and open positions.

Do not run `git pull` on the VPS until the server-side working tree is reconciled with the repository. Do not run `paper_trader.py` on the production host: it is legacy, shares the AI budget, and writes a different state file.

## Known limitations

- A stop-loss can gap through its trigger, and a market can have no bid. The 20s guard, panic override and re-quote loop keep retrying, but they cannot guarantee a fill.
- An ambiguous order submission without an `order_id` still requires manual reconciliation; automatic discovery by account trades is not implemented yet. Missing partial SELLs are recovered, but not via a full order search.
- Resolved positions rely on Polymarket auto-redeem. The bot records `redemption_pending` but does not confirm it or call `redeem_positions()` itself.
- The Telegram bot is read-only and has no second-instance lock (the trader has `flock`); never run two bots with the same token. The bot loads `state/live_config.env`, so it also sees the wallet key even though it only reads the portfolio.
- There is no committed test suite yet; use throwaway mock tests before every restart.
- `state/live_trader.log` has no rotation and `state/live_portfolio_state.json` is only backed up manually before deploys.
- `state.initial_deposit_usd` can lag behind `LIVE_TOTAL_DEPOSIT` after a limit change; treat the config as authoritative and the state field as historical.
- Smart Bonding sells tail risk: a single full loss can exceed many small gains. Profitability is not guaranteed.
