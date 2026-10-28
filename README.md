# AI Yield Scanner

A paper-trading research system that looks for yield on **Polymarket** (prediction markets) and **Hyperliquid** (perpetual futures), screens every entry with an LLM risk audit (Venice AI), and simulates a $5,000 USDC portfolio against live market data. A Telegram bot shows the portfolio and sends alerts.

> **Paper trading only.** The system never places orders and holds no wallet or exchange keys. All P&L is simulated from public market data.

## Strategies

The portfolio has five $1,000 slots:

| Slots | Strategy | Idea |
|---|---|---|
| 1–3 | **Polymarket liquidity rewards** (`poly_mm`) | Quote two-sided limit orders inside the reward spread and collect Polymarket's daily USDC liquidity rewards. |
| 4 | **Polymarket bonding** (`poly_bond`) | Buy an outcome priced 96.5¢–99.4¢ shortly before resolution and hold it to the $1.00 payout. |
| 5 | **Hyperliquid funding carry** (`hl_funding`) | Short a perp with persistently positive funding and hedge it with the matching Hyperliquid spot token. |

When a slot closes (resolution, stop-loss, AI rejection, repeated fills, funding reversal), its capital is reallocated to a new candidate automatically.

## How it works

```
             ┌──────────── every 15 min ─────────────┐   ┌──── every 60 s ────┐
Gamma API ──►│ scan markets → heuristic risk filter  │   │ CLOB order book    │
CLOB API  ──►│ → Venice AI audit (2/2 consensus)     │   │ + trade tape       │
Hyperliquid ►│ → open / re-audit / close slots       │   │ → fills, kill-     │
             │ → mark bonds, accrue funding          │   │   switch, rewards  │
             └───────────────┬───────────────────────┘   └─────────┬──────────┘
                             └───────► state/portfolio_state.json ◄┘
                                                │
                                        telegram_bot.py (read-only UI + alerts)
```

### Market-making simulation (`poly_mm`)

- **Rewards** use Polymarket's quadratic scoring, `S = ((v − s) / v)² × size`, computed against the live CLOB book (only orders ≥ `rewardsMinSize` count; the min/÷3 rule for one-sided books is applied).
- **Honest accrual.** Rewards accrue only for windows in which our quotes rested without a fill and the book was healthy. The lower of the start/end APR is used, the APR may at most double per hour, and it is capped at 365%/year. A book emptied by a crash produces a huge formula APR, but that is not income.
- **Fill detection from the trade tape.** Any trade at or through our bid/ask (including the NO side, converted to YES-equivalent prices) is treated as a fill. The Polymarket Data API shows trades 1.5–4 minutes late, so each check reads the last 15 minutes of trades, deduplicates them, and compares each trade with the quotes that were live at that trade's timestamp (`quote_log`).
- **After a fill**, the inventory is unwound conservatively at the worse of mid and the crossing trade price, plus half the book spread (≥ 0.8¢/share). Quoting pauses for 3 hours, and a second fill within 24 hours closes the slot.
- **Kill-switch.** Quotes are re-centred when the mid moves ≥ 1¢ from the price at which they were placed, so slow drift is caught too.
- **Expiry filter.** MM only opens on markets that expire in ≥ 7 days and is closed when < 2 days remain.

### Bonding (`poly_bond`)

The position is marked to market every tick, and a stop-loss sells it below 95¢. Resolved markets pay $1 or $0 per share.

### Funding carry (`hl_funding`)

Only coins with a liquid Hyperliquid spot hedge (≥ $100k daily volume) and positive funding qualify. Funding accrues on the perp leg (half the slot). The slot closes after two consecutive ticks of adverse funding or on delisting, with fees charged on entry and exit.

### AI risk audit (Venice AI)

- Every entry requires **two independent APPROVE verdicts** with confidence ≥ 70. Anything else is SKIP (fail-closed).
- The prompt includes the current date, the market rules, and the market's end date, plus a rule to skip markets that depend on elections or other events happening this week.
- Open Polymarket positions are re-audited every 6 hours; two fresh SKIPs in a row close the position.
- **Budget control.** There is a hard cap of 40 paid calls per UTC day, of which 16 are reserved for re-audits. Slot refill checks at most one new candidate per strategy per tick, and cached verdicts are reused for free. A typical call costs ~0.003 DIEM.
- **Emergency mode.** A free `/models` heartbeat runs every tick. Two consecutive failures pause all MM quoting until the connection recovers.

## Project layout

| File | Purpose |
|---|---|
| `paper_trader.py` | Simulation daemon: slots, fills, rewards, refill, re-audits, CLI status |
| `polymarket_scanner.py` | Gamma/CLOB/Data API access, quadratic reward model, candidate scans |
| `hyperliquid_scanner.py` | Funding anomaly scan with spot-hedge check |
| `ai_analyzer.py` | Heuristic risk filter, Venice AI audit, consensus, daily budget, health tracking |
| `telegram_bot.py` | Interactive Telegram bot (portfolio, AI verdicts, logs, settings) |
| `telegram_notifier.py` | Alert delivery with HTML escaping and plain-text fallback |
| `state_io.py` | Atomic JSON writes and HTML escaping |
| `run_scanner.py` | One-off market scan in the terminal |
| `deploy/` | systemd unit templates and configuration examples |
| `MANUAL.md` | Detailed technical manual (Russian) |

The code uses only the Python standard library (3.10+).

## Setup

```bash
git clone git@github.com:ahdpe/ai-yield-scanner.git ~/ai_yield_scanner
cd ~/ai_yield_scanner
mkdir -p state

# Telegram bot credentials (state/ is git-ignored)
cp deploy/telegram_config.example.json state/telegram_config.json
chmod 600 state/telegram_config.json      # then fill in the token and your user ID

# Venice AI key: export it, or put VENICE_API_KEY=... into ~/.hermes/.env
export VENICE_API_KEY=...                  # see deploy/env.example for all variables
```

Run it manually:

```bash
python3 run_scanner.py --deposit 1000        # one-off scan
python3 paper_trader.py --once               # one simulation tick
python3 paper_trader.py --status             # portfolio report
python3 paper_trader.py --reset              # start a fresh portfolio
```

Or run it as systemd user services:

```bash
cp deploy/ai-yield-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ai-yield-paper ai-yield-bot
loginctl enable-linger "$USER"               # keep running after logout
```

By default `VENICE_BASE_URL` points to a local OpenAI-compatible budget proxy at `127.0.0.1:8787`. To call Venice directly, set it to `https://api.venice.ai/api/v1`.

## Secrets and data

- No credentials are stored in the repository. Telegram credentials live in `state/telegram_config.json`, and the Venice key comes from the environment or `~/.hermes/.env`.
- `state/` (portfolio, logs, AI cache, budget counters) and all `.env` files are git-ignored.

## Results so far

The simulation has been rebuilt several times as modelling flaws were found and fixed: same-day expiry markets, re-quoting into a crash, inflated rewards on empty books, and trade-feed lag hiding fills. The early runs are therefore not comparable.

With the current honest fill model, thin "high-APR" markets have so far lost more to fills than they earned in rewards, while calm markets earn small but steady rewards. There is not yet enough history to claim profitability.
