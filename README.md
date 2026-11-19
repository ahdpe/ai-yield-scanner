# AI Yield Scanner — Live Smart Bonding

A real-money **Polymarket Smart Bonding** bot with a two-model LLM risk audit (Venice AI, with Command Code failover). It scans high-probability outcomes, buys them near resolution, and protects open positions with a relative stop-loss. A Telegram bot provides a read-only portfolio view and alerts.

> **LIVE — REAL FUNDS.** This repository now runs a live trading bot. It holds a Polymarket wallet key in `state/live_config.env` on the production host. Never commit `state/`, never print keys or chat IDs, and never start a second copy of the trader or the bot. The old paper-trading system is legacy; see `deploy/legacy-paper/`.

## Strategy

- **Universe:** top-500 active Polymarket markets by 24h volume (Gamma API, paginated).
- **Entry:** leading outcome **94.5¢–98.5¢**, expiry **0.5–45 days**, liquidity ≥ **$25,000**, book spread ≤ **1.5¢**, fee rate ≤ **0.06**. Sports and crypto price targets are excluded. A market without a valid `endDate` is rejected.
- **Position:** **10 slots × $100** (strategy limit $1,000). Buy at the best ask, then reconcile confirmed fills and fees against the actual conditional-token balance.
- **Risk:** relative stop-loss **-3.5¢** from entry (floor $0.85), max **2 open slots per topic cluster**, 12h reopen cooldown, 6h AI re-audit. Two fresh `SKIP` verdicts close a position.
- **Execution:** `polymarket-client` `SecureClient` (Polymarket Deposit Wallet, `signature_type=3`, pUSD). The intent is persisted before the order is submitted; an ambiguous submission fails closed and requires reconciliation.

### Stop-loss behavior

A stop-loss SELL is placed at the current best bid (one tick below it for a higher fill probability). If the order is still unfilled after 60 seconds, or if the market has moved so that its limit is above the current best bid, the trader cancels it, waits for the cancellation to be confirmed by reconciliation, and re-quotes at the new best bid. A new SELL is never sent while an old one is still live or unconfirmed.

## AI audit

- Every entry requires **2/2 APPROVE** with confidence ≥ 70 from `claude-sonnet-5-5` + `grok-4-6` (Venice web search enabled). Anything else is `SKIP` (fail-closed).
- Open positions are re-audited every 6 hours with one model; the first `SKIP` triggers a recheck in 1 hour, and a second fresh `SKIP` closes the position.
- **Budget:** Venice allows 240 paid calls per UTC day, 96 reserved for re-audits. Command Code is the automatic failover provider with a separate 240/96 budget. If both are unavailable or exhausted, new entries are blocked.
- Prompt version 3: English prompt, Russian `summary`, market rules capped at 4,000 characters, verdict cache 6h.

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

- A stop-loss can gap through its trigger, and a market can have no bid. The re-quote logic keeps retrying, but it cannot guarantee a fill.
- An ambiguous order submission without an `order_id` still requires manual reconciliation; automatic discovery by account trades is not implemented yet.
- Resolved positions rely on Polymarket auto-redeem. The bot records `redemption_pending` but does not confirm it or call `redeem_positions()` itself.
- The Telegram bot is read-only and has no second-instance lock (the trader has `flock`); never run two bots with the same token.
- There is no committed test suite yet; use throwaway mock tests before every restart.
- Smart Bonding sells tail risk: a single full loss can exceed many small gains. Profitability is not guaranteed.
