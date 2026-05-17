# AI Investing

This repo contains a retail-friendly automated trading system built for Alpaca. It does not try to promise "maximum profit", because that is not a real engineering target in markets. The system is designed to optimize for a more defensible goal: disciplined, repeatable execution with backtesting, walk-forward parameter selection, and explicit risk controls.

## Why Alpaca for v1

As of May 17, 2026, Alpaca is the best default starting point for a solo automated system when the main priority is a fast paper-to-live path:

- Alpaca's Trading API is API-first, supports stocks and crypto, and offers free paper trading.
- Alpaca paper trading is available globally and can be tested without funding a live account.
- Alpaca supports fractional trading for many US equities, which helps when deploying smaller balances.
- Alpaca now also ships an official CLI for paper trading and market data workflows, which makes future automation easier.

IBKR remains a stronger choice if you need broader global market access or more asset classes, but its current official API docs still expose materially more session and authentication complexity for trading workflows.

## What This System Does

- Fetches daily historical data from Alpaca Market Data.
- Runs an ETF momentum and trend-following strategy with a defensive regime.
- Backtests multiple parameter sets and selects the highest-scoring configuration.
- Generates target portfolio weights for the current session.
- Rebalances an Alpaca account with guardrails:
  - paper trading by default
  - live trading blocked unless explicitly enabled
  - cash buffer
  - max position size
  - drawdown kill switch
  - duplicate rebalance prevention

## Strategy

The default strategy trades liquid US ETFs only:

- Risk-on universe: `SPY, QQQ, IWM, EFA, EEM`
- Defensive universe: `TLT, IEF, GLD, SHY`

Logic:

1. Compute weighted momentum over 1, 3, and 6 months.
2. Require assets to be above a long-term moving average before they are eligible for risk-on allocation.
3. Use inverse-volatility sizing for selected assets.
4. Fall back to defensive ETFs when the risk-on set is weak.
5. Keep a cash buffer and cap single-name exposure.

This is deliberately simple. Alpaca's own automated-trading disclosures warn against over-optimization and explicitly note that its platform is not intended for high-frequency trading.

## Quick Start

1. Create an Alpaca account and generate API credentials.
2. Copy `.env.example` to your own environment file and set the values.
3. Run commands with `PYTHONPATH=src` or install the package locally.

### Backtest

```bash
PYTHONPATH=src python3 -m ai_investing.cli backtest --start 2021-01-01 --end 2026-05-16
```

### Generate Today's Target Weights

```bash
PYTHONPATH=src python3 -m ai_investing.cli signal
```

### Dry-Run a Rebalance

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade
```

### Submit Orders to Paper Trading

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit
```

### Enable Live Trading

Live trading requires both:

- `ALPACA_PAPER=false`
- `AI_INVESTING_ENABLE_LIVE=1`

Then:

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit
```

## Suggested Deployment

For a first live deployment:

- run the system in paper trading for at least 4-8 weeks
- schedule it once per market day, not continuously
- rebalance near the open or once near the close
- start with a small account and small trade sizes

Example cron schedule for weekday runs at 9:40am New York time:

```cron
40 9 * * 1-5 cd /path/to/AI-Investing && PYTHONPATH=src /usr/local/bin/python3 -m ai_investing.cli trade --submit >> logs/trade.log 2>&1
```

## Limitations

- The backtest uses close-to-close approximations and a simple transaction-cost model.
- The strategy is long-only and ETF-only by default.
- The order executor is designed for low-frequency rebalancing, not day trading or HFT.
- Market data access still requires Alpaca API credentials in this implementation.

## References

- Alpaca Trading API: https://docs.alpaca.markets/us/docs/trading-api
- Alpaca Market Data / historical bars: https://docs.alpaca.markets/us/reference/stockbars
- Alpaca SDKs and tools: https://docs.alpaca.markets/us/docs/sdks-and-tools
- Alpaca automated trading risk disclosure: https://files.alpaca.markets/disclosures/library/RisksAutoTrading.pdf
- IBKR API overview: https://www.interactivebrokers.com/en/trading/ib-api.php?menu=A
- IBKR Web API session docs: https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/
