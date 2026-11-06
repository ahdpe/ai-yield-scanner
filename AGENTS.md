# Agent guide: AI Yield Scanner

Context for coding agents (Antigravity, Claude Code, Codex and others) working on this repository. Read `README.md` for what the system does; this file covers how to work on it safely.

## The owner

- Speaks Russian. Reply in Russian; code comments and log messages are in Russian too. Repository docs (`README.md`, this file) stay in English, `MANUAL.md` is Russian.
- Expects verified, working changes: test before you restart anything, and report what you checked and what you could not check.
- Commit and push only when asked. Never reset the portfolio, delete state or change limits without asking.

## Where it runs

Production is a single Linux server. The repository is cloned to `~/ai_yield_scanner` and runs as two **systemd user services**:

| Unit | Process | Notes |
|---|---|---|
| `ai-yield-paper.service` | `paper_trader.py --deposit 5000 --interval 120 --mm-poll 60` | Simulation daemon: full tick every 2 min, MM check every 60 s |
| `ai-yield-bot.service` | `telegram_bot.py` | Telegram UI. Only one instance per bot token may run |

- The live unit files live in `~/.config/systemd/user/`, outside the repo, and differ from the `deploy/` templates: `--interval 120` instead of 900, and `AI_MAX_CALLS_PER_DAY=80`, `AI_REAUDIT_RESERVE_CALLS=32` instead of the 40/16 code defaults. Both units have `ExecStopPost=... service_exit_alert.py %n` (crash alerts).
- Venice AI is reached through a local budget proxy at `127.0.0.1:8787` (`venice-budget-proxy.service`, part of Hermes in `~/.hermes/`). The Venice key and the daily DIEM balance (resets at 00:00 UTC) are shared with Hermes. Do not modify the proxy, Hermes or `~/.hermes/.env`.
- Other projects on the server are not part of this repo. Leave them alone.

### Deploying a change

```bash
cd ~/ai_yield_scanner
git pull
python3 -m py_compile *.py
systemctl --user restart ai-yield-paper ai-yield-bot
journalctl --user -u ai-yield-paper -f      # wait for "Тик #N завершен" and no Traceback
```

If you edit a unit file: `systemctl --user daemon-reload` before restarting. Back up the unit first.

On the server, `git push` over SSH fails (GitHub host key is not in `known_hosts`); push over HTTPS with the GitHub CLI credentials:

```bash
git -c credential.helper= -c credential.helper='!gh auth git-credential' push https://github.com/ahdpe/ai-yield-scanner.git main:main
```

### Do not run a second copy

Never start `paper_trader.py` (daemon mode) or `telegram_bot.py` on another machine or a second time on the server. Two bots polling one token break each other (Telegram returns 409), alerts are duplicated, and two daemons would spend the same AI budget. For local experiments use mocked network calls (see Testing).

## State and secrets

Everything mutable lives in `state/` (git-ignored, never commit it):

| File | Purpose |
|---|---|
| `portfolio_state.json` | The simulated portfolio (schema 4). Written atomically by the daemon |
| `paper_trader.log` | Event log shown by the bot's `/logs` |
| `ai_audits_cache.json` | Cached Venice verdicts (3 h for entries, 6 h for re-audits) |
| `ai_budget.json` | Paid AI calls used today (UTC). Do not delete mid-day: it enforces the daily cap |
| `ai_health.json` | Venice connectivity and emergency mode |
| `error_alerts.json`, `error_alerts_bot.json`, `service_exit_alerts.json` | Error-alert trackers (daemon, bot, crash hook) |
| `telegram_config.json` | Bot token, admin chat ID, `alerts_enabled` (mode 600) |
| `*_runN.*` | Archives of earlier experiment runs |

Secrets: the Telegram token and admin ID are in `state/telegram_config.json`, and the Venice key is in the environment or `~/.hermes/.env`. Never print, log or commit them. The repository is **public**: do not add server IPs, home-directory paths, Telegram IDs, e-mail addresses or keys to code, docs or commit messages (git history was already rewritten once to remove them).

## Experiment rules

- This is paper trading only. The code must never place real orders or hold wallet keys.
- The current run started on 2026-11-04 16:09 UTC on the current fill and accrual model. Check results with `python3 paper_trader.py --status` (read-only).
- Modelling changes that affect P&L (fills, rewards, fees, filters) make earlier results incomparable. Ask the owner before starting a new run. A reset means: stop the daemon, rename `portfolio_state.json`, `paper_trader.log` and `ai_audits_cache.json` to the next `*_runN` suffix, start the daemon (it creates a fresh portfolio).
- Keep the honest-simulation guarantees described in `README.md`: fills come from the trade tape against the quotes that were live at each trade's timestamp, there are no rewards for windows with fills, pauses or broken books, APR is damped and capped, and AI audits fail closed.
- AI budget: about 0.003 DIEM per call with `include_venice_system_prompt: False`. Refill audits at most one fresh candidate per strategy per tick and keeps a reserve for re-audits. Do not add code paths that call Venice in loops.

## External APIs (verified November 2026)

- **Polymarket Gamma**: `GET /markets?active=true&closed=false&...` for scans. Look up a single market with `GET /markets/slug/{slug}`, which also returns closed markets; the `?slug=` filter no longer does.
- **Polymarket CLOB**: `GET /book?token_id=` (the YES book already mirrors NO orders). Occasional HTTP 403 is normal and transient.
- **Polymarket Data API**: v1 was retired on 2026-11-02 (HTTP 410). Use `GET /v2/trades?condition={conditionId}&limit=` with a `{"data": [...], "pagination": {...}}` envelope and snake_case fields (`outcome_index`, `transaction_hash`, `token_id`). Trades arrive newest first, taker side only, with a lag of seconds. v2 rejects `market` and `offset` with HTTP 400.
- **Hyperliquid info API**: `metaAndAssetCtxs`, `fundingHistory`, `spotMetaAndAssetCtxs`.
- **Venice** (OpenAI-compatible) via the proxy. The free `/models` ping is the heartbeat; paid calls can fail while the ping succeeds (for example when DIEM runs out), which `error_alerts` reports.

APIs change without notice. When a call starts failing, check the provider's docs or migration notes (Polymarket publishes `https://docs.polymarket.com/llms.txt`) instead of guessing.

## Code map

- `paper_trader.py`: daemon, slots (`poly_mm`, `poly_bond`, `hl_funding`), fills, accrual, refill, re-audits, `--status/--once/--reset`.
- `polymarket_scanner.py`: Gamma/CLOB/Data API access, quadratic reward model, candidate scans.
- `hyperliquid_scanner.py`: funding scan with spot-hedge check.
- `ai_analyzer.py`: heuristic filter, Venice audit with 2/2 consensus, daily budget, health and emergency mode.
- `telegram_bot.py`, `telegram_notifier.py`: bot UI and alert delivery (HTML with plain-text fallback, 4096-character limit).
- `error_alerts.py`: recurring-error tracker (alert after 20 min of recurrence, a reminder every 6 h, "recovered" after 15 min quiet; `immediate=True` for code errors).
- `service_exit_alert.py`: stdlib-only systemd crash hook. Keep it free of project imports.
- `state_io.py`: `STATE_DIR`, atomic JSON writes, HTML escaping (`esc`).

Only the Python standard library is used (3.10+). Do not add dependencies without asking.

## Testing

There is no automated test suite yet. Before restarting the services:

1. `python3 -m py_compile *.py`
2. Write a throwaway script outside the repo that imports the module, monkeypatches network functions (`fetch_clob_book`, `fetch_recent_trades`, `send_telegram_alert`, `log_event`, ...) and asserts the behaviour on synthetic data. It must not write to `state/`: compare a checksum of `state/portfolio_state.json` before and after.
3. Check live endpoints read-only with `curl` or a small script when an API is involved.
4. After the restart, watch the journal for at least one full tick and one MM check.

Do not run `paper_trader.py --once` or `--reset` while the daemon is running: both write the live state.
