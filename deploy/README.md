# Deployment

These units are for the **LIVE** production VPS and use **systemd system services** (no `--user`). They run real orders with real funds.

## Install

1. Install Python 3.10+ and create the virtualenv:

   ```bash
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

2. Create the private state files:

   ```bash
   mkdir -p state
   cp deploy/env.example state/live_config.env
   chmod 600 state/live_config.env
   cp deploy/telegram_config.example.json state/telegram_config.json
   chmod 600 state/telegram_config.json
   ```

   Fill in the wallet/API keys in `live_config.env` and the bot token/admin chat id in `telegram_config.json`. Never commit either file.

3. Replace `__PROJECT_DIR__` in both unit files with the project directory (for example with `sed`), then install them:

   ```bash
   cp deploy/ai-yield-live.service deploy/ai-yield-live-bot.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now ai-yield-live.service ai-yield-live-bot.service
   systemctl is-active ai-yield-live.service ai-yield-live-bot.service
   ```

4. Watch one full tick and verify that the state and CLOB balance agree:

   ```bash
   journalctl -u ai-yield-live -n 40 --no-pager -o cat
   venv/bin/python3 live_trader.py --status
   ```

## Safety notes

- Only one `live_trader.py` and one `telegram_bot.py` may run. The trader takes `state/live_trader.lock`; the bot relies on Telegram's 409 conflict.
- `EnvironmentFile` gives both services the wallet key. The bot does not need it; if you split the files, keep the trader's key restricted.
- Do not run `git pull` on the VPS until the server working tree is reconciled with the repository.
- Do not install `deploy/legacy-paper/` on the live host: those units run `paper_trader.py` with system Python and a $5,000 simulation.
