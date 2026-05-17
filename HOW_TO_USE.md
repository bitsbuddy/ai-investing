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
cp .env.example .env.local
```

Set at least these values in `.env.local`:

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

## 3. Main Ways To Use It

There are 4 main commands:

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

Then run:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit
```

The system will:

- check buying power
- check price drift versus the signal reference prices
- resume a partially submitted rebalance instead of blindly duplicating it
- keep pending orders open until they are reconciled
- ingest latest official macro and filing news unless you explicitly disable it

## 9. Move To Live Trading

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

## 10. Typical Daily Workflow

Use this order:

1. Update or regenerate the research snapshot if you use it.
2. Run `research`.
3. Review the `Official News Context` section for fresh macro or SEC filing changes.
4. Run `signal`.
5. Run `trade` without `--submit`.
6. Review the output.
7. If it looks correct, run `trade --submit`.

## 11. Optional: Broaden The Universe

Default risk-on universe:

```env
AI_INVESTING_RISK_ON=SPY,QQQ,IWM,EFA,EEM
```

Example with equities:

```env
AI_INVESTING_RISK_ON=SPY,QQQ,MSFT,NVDA,AMZN
```

If you add equities, put company metrics for them in the research snapshot.

## 12. Important Safety Notes

- Start with paper trading.
- Start with small size.
- Do not assume backtest results will match live performance.
- Research snapshots must be current. Future-dated or stale snapshots are rejected.
- Official news is live-only context. It is not included in backtests, by design.
- This system is designed for low-frequency rebalancing, not intraday trading or HFT.

## 13. Useful Commands

Show CLI help:

```bash
PYTHONPATH=src python3 -m ai_investing.cli --help
```

Show research help:

```bash
PYTHONPATH=src python3 -m ai_investing.cli research --help
```

## 14. Recommended Starting Point

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
