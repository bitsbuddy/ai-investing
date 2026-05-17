# How To Use This System

This file is the practical guide for running the repo.

## 1. What You Need

- Python 3.11+
- An Alpaca account
- Alpaca API key and secret
- Optional: a research snapshot file if you want company/index/ETF analysis
- Optional but recommended: a declared SEC user agent so the official news collector can access SEC data cleanly

## 2. First-Time Setup

From the repo root:

```bash
PYTHONPATH=src python3 -m ai_investing.cli paper-setup
```

This writes `.env.paper` by default. Then load it:

```bash
set -a; source .env.paper; set +a
```

If you prefer manual setup, you can still do:

```bash
cp .env.example .env.local
```

Set at least these values in `.env.local` or `.env.paper`:

```env
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_PAPER=true
AI_INVESTING_ENABLE_LIVE=0
```

If you want the research overlay, also set:

```env
AI_INVESTING_RESEARCH_SNAPSHOT_PATH=examples/research_snapshot.example.json
AI_INVESTING_SEC_USER_AGENT=AI-Investing your-email@example.com
```

When running commands, load your env file in your shell first or export the variables manually.

Useful `paper-setup` variants:

```bash
PYTHONPATH=src python3 -m ai_investing.cli paper-setup --env-file .env.local --force
PYTHONPATH=src python3 -m ai_investing.cli paper-setup --research-snapshot examples/research_snapshot.example.json
PYTHONPATH=src python3 -m ai_investing.cli paper-setup --sec-user-agent "AI-Investing your-email@example.com"
```

If HTTPS certificate verification fails on your machine:

```env
AI_INVESTING_CA_BUNDLE=/path/to/cacert.pem
```

Only for local paper-testing as a last resort:

```env
AI_INVESTING_SSL_NO_VERIFY=1
```

## 3. Main Ways To Use It

There are 7 main commands:

- `paper-setup`: create a safe paper-trading env file
- `automation-setup`: generate a weekday runner script and cron template
- `automation-ui`: open a local start/stop control panel for automation
- `backtest`: test the strategy on history
- `research`: score assets using the research snapshot
- `signal`: generate the current target portfolio
- `trade`: preview or submit a rebalance

`research`, `signal`, and `trade` now also pull latest official-source news by default from the SEC, Federal Reserve, BLS, and Treasury.

## 4. Backtest It

Basic backtest:

```bash
PYTHONPATH=src python3 -m ai_investing.cli backtest --start 2021-01-01 --end 2026-05-16
```

Notes:

- This uses walk-forward parameter selection by default.
- If you want a fixed-parameter run instead:

```bash
PYTHONPATH=src python3 -m ai_investing.cli backtest --start 2021-01-01 --end 2026-05-16 --no-optimize
```

## 5. Run Research Analysis

If you have a snapshot file:

```bash
PYTHONPATH=src python3 -m ai_investing.cli research --research-snapshot examples/research_snapshot.example.json
```

If you do not have Alpaca credentials loaded, `research` can still run from the snapshot file alone.

If you want to disable current official news for a one-off run:

```bash
PYTHONPATH=src python3 -m ai_investing.cli research --research-snapshot examples/research_snapshot.example.json --no-official-news
```

## 6. Generate Today’s Signal

Without research overlay:

```bash
PYTHONPATH=src python3 -m ai_investing.cli signal
```

With research overlay:

```bash
PYTHONPATH=src python3 -m ai_investing.cli signal --research-snapshot examples/research_snapshot.example.json
```

This prints:

- regime (`risk_on` or `risk_off`)
- target weights
- official-source news context
- selected research scores when the overlay is enabled

## 7. Preview Trades Without Sending Orders

Dry run:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade
```

With research overlay:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --research-snapshot examples/research_snapshot.example.json
```

This is the normal command to use before submitting anything.

## 8. Submit To Alpaca Paper Trading

Make sure:

- `ALPACA_PAPER=true`
- `AI_INVESTING_ENABLE_LIVE=0`

If you used `paper-setup`, those are already set. Then run:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit
```

The system will:

- check buying power
- check price drift versus the signal reference prices
- resume a partially submitted rebalance instead of blindly duplicating it
- keep pending orders open until they are reconciled
- ingest latest official macro and filing news unless you explicitly disable it

## 9. Automate The Daily Run

Generate a weekday runner script and cron template:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-setup
```

This writes:

- `scripts/run_paper_trade.sh`
- `automation/paper_trade.cron`

By default it schedules a weekday run at `09:40` in your machine's local timezone and submits paper orders automatically.

Install the cron schedule:

```bash
crontab automation/paper_trade.cron
crontab -l
```

Watch the automation log:

```bash
tail -f logs/paper-trade.log
```

If you want to test the automation without sending orders first:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-setup --preview-only --force
```

## 10. Start And Stop Automation From A UI

Run the local control panel:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-ui
```

Then open:

```text
http://127.0.0.1:8787
```

The UI gives you:

- a simple enabled / disabled status
- a `Start Automation` button
- a `Stop Automation` button
- a `Run Now` button to trigger the runner immediately
- a high-level progress summary of the last run
- the current runner phase and result
- the recent automation log output
- the key file paths and cron schedule being used by the scheduler

The scheduled runner checks the control file before each run, so stopping automation in the UI prevents the next scheduled trade from executing.

## 11. Move To Live Trading

Only do this after paper trading for a while.

Change:

```env
ALPACA_PAPER=false
AI_INVESTING_ENABLE_LIVE=1
```

Then submit:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit
```

## 12. Typical Daily Workflow

Use this order:

1. Update or regenerate the research snapshot if you use it.
2. Run `research`.
3. Review the `Official News Context` section for fresh macro or SEC filing changes.
4. Run `signal`.
5. Run `trade` without `--submit`.
6. Review the output.
7. If it looks correct, run `trade --submit`.

## 13. Optional: Broaden The Universe

Default risk-on universe:

```env
AI_INVESTING_RISK_ON=SPY,QQQ,IWM,EFA,EEM
```

Example with equities:

```env
AI_INVESTING_RISK_ON=SPY,QQQ,MSFT,NVDA,AMZN
```

If you add equities, put company metrics for them in the research snapshot.

## 14. Important Safety Notes

- Start with paper trading.
- Start with small size.
- Do not assume backtest results will match live performance.
- Automation should stay on paper until you have reviewed logs and fills for a while.
- The UI only enables or disables scheduled runs. It does not replace the scheduler itself.
- Research snapshots must be current. Future-dated or stale snapshots are rejected.
- Official news is live-only context. It is not included in backtests, by design.
- This system is designed for low-frequency rebalancing, not intraday trading or HFT.

## 15. Useful Commands

Show CLI help:

```bash
PYTHONPATH=src python3 -m ai_investing.cli --help
```

Show research help:

```bash
PYTHONPATH=src python3 -m ai_investing.cli research --help
```

Show automation help:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-setup --help
```

Show automation UI help:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-ui --help
```

## 16. Recommended Starting Point

If you want the shortest path:

1. Set Alpaca paper-trading credentials.
2. Run:

```bash
PYTHONPATH=src python3 -m ai_investing.cli backtest --start 2021-01-01 --end 2026-05-16
PYTHONPATH=src python3 -m ai_investing.cli signal --research-snapshot examples/research_snapshot.example.json
PYTHONPATH=src python3 -m ai_investing.cli trade --research-snapshot examples/research_snapshot.example.json
```

3. If the dry run looks right:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit --research-snapshot examples/research_snapshot.example.json
```
