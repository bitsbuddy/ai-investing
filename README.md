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
- Uses walk-forward parameter selection instead of full-sample parameter fitting.
- Optionally overlays company, index, ETF, and official-source news context onto the signal engine.
- Generates target portfolio weights for the current session.
- Rebalances an Alpaca account with guardrails:
  - paper trading by default
  - live trading blocked unless explicitly enabled
  - cash buffer
  - max position size
  - drawdown kill switch
  - duplicate rebalance prevention
  - buying-power checks
  - market-price drift checks before submission
  - resumable order batches after partial failures

## Strategy

The default strategy still starts from liquid US ETFs:

- Risk-on universe: `SPY, QQQ, IWM, EFA, EEM`
- Defensive universe: `TLT, IEF, GLD, SHY`

Base quant logic:

1. Compute weighted momentum over 1, 3, and 6 months.
2. Require assets to be above a long-term moving average before they are eligible for risk-on allocation.
3. Use inverse-volatility sizing for selected assets.
4. Fall back to defensive ETFs when the risk-on set is weak.
5. Keep a cash buffer and cap single-name exposure.

This remains deliberately simple on the execution side. Alpaca's own automated-trading disclosures warn against over-optimization and explicitly note that its platform is not intended for high-frequency trading.

## Comprehensive Research Layer

Pure quant is not enough if your goal is to approximate an institutional decision stack. The system now supports a second layer of analysis through a research snapshot file:

- Company analysis:
  - growth
  - margins
  - free cash flow
  - leverage
  - interest coverage
  - valuation
- Index analysis:
  - market breadth
  - trend
  - relative strength
  - volatility regime
  - credit conditions
  - yield curve slope
- ETF analysis:
  - expense ratio
  - AUM
  - dollar volume / liquidity
  - tracking error
  - recent flows
  - look-through portfolio quality and valuation
- Official-source news analysis:
  - SEC EDGAR filing flow for equity names in the snapshot
  - Federal Reserve press releases / RSS
  - BLS latest economic releases
  - Treasury press releases relevant to issuance and funding conditions

The engine blends these with quant instead of replacing quant. The overlay is applied to both risk-on and defensive selection. In practice, that means:

1. Quant tells you what is working.
2. Index analysis tells you whether the backdrop supports taking risk.
3. Company analysis tells you whether an individual equity deserves capital.
4. ETF analysis tells you whether the wrapper is efficient enough to use.
5. Official-source news tells you whether fresh macro or filing developments should lean the system more risk-on, more defensive, or more cautious.

This is closer to how large investment firms think, but it is still a compact retail implementation. It is not equivalent to a real institutional platform with dedicated macro teams, fundamental analysts, alternative data, private research feeds, and execution infrastructure.

## Research Snapshot

Use [examples/research_snapshot.example.json](/Users/rishik/AI-Investing/examples/research_snapshot.example.json:1) as the template. The file is point-in-time and intended for live decision support. Ratios and percentages are entered as decimals, for example `0.15` for `15%`.

The snapshot is validated before use:

- future-dated snapshots are rejected
- stale snapshots are rejected based on `AI_INVESTING_RESEARCH_MAX_AGE_DAYS`

Example commands:

```bash
PYTHONPATH=src python3 -m ai_investing.cli research --research-snapshot examples/research_snapshot.example.json
PYTHONPATH=src python3 -m ai_investing.cli signal --research-snapshot examples/research_snapshot.example.json
PYTHONPATH=src python3 -m ai_investing.cli trade --research-snapshot examples/research_snapshot.example.json
```

Official-source news is enabled by default for `research`, `signal`, and `trade`. It is intentionally not used in `backtest`, because current news would create lookahead bias in historical results.

You can also broaden the universe beyond ETFs. For example:

```bash
export AI_INVESTING_RISK_ON=SPY,QQQ,MSFT,NVDA,AMZN
```

If you do that, provide company metrics for those equities in the research snapshot.

## Quick Start

If you want a step-by-step operator guide, read [HOW_TO_USE.md](/Users/rishik/AI-Investing/HOW_TO_USE.md:1).

1. Create an Alpaca account and generate API credentials.
2. Copy `.env.example` to your own environment file and set the values.
3. Run commands with `PYTHONPATH=src` or install the package locally.

### Backtest

```bash
PYTHONPATH=src python3 -m ai_investing.cli backtest --start 2021-01-01 --end 2026-05-16
```

This optimized backtest now uses walk-forward parameter reselection instead of choosing one parameter set on the full sample.

### Generate Today's Target Weights

```bash
PYTHONPATH=src python3 -m ai_investing.cli signal
```

### Generate a Multi-Layer Research Report

```bash
PYTHONPATH=src python3 -m ai_investing.cli research --research-snapshot examples/research_snapshot.example.json
```

### Dry-Run a Rebalance

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade
```

### Submit Orders to Paper Trading

```bash
PYTHONPATH=src python3 -m ai_investing.cli trade --submit
```

Before submission, the system checks that:

- current market prices have not drifted too far from the signal reference prices
- the rebalance fits within buying power and expected sell proceeds
- any partially submitted rebalance is resumed instead of duplicated

The live-oriented commands also print an `Official News Context` section so you can see which reliable sources were ingested and what macro bucket scores they produced.

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
- The strategy is long-only and ETF-heavy by default, though the universe can include equities.
- The order executor is designed for low-frequency rebalancing, not day trading or HFT.
- Market data access still requires Alpaca API credentials in this implementation.
- The pre-trade drift guard uses latest trade prices, not full order book simulation.
- The research overlay is point-in-time. Historical backtesting of fundamentals, macro, and ETF structure requires properly time-aligned historical snapshots to avoid lookahead bias.
- The latest-news layer is heuristic. It uses official primary sources, but it is not a substitute for full discretionary event interpretation by a human or an institutional macro desk.

## References

- Alpaca Trading API: https://docs.alpaca.markets/us/docs/trading-api
- Alpaca Market Data / historical bars: https://docs.alpaca.markets/us/reference/stockbars
- Alpaca SDKs and tools: https://docs.alpaca.markets/us/docs/sdks-and-tools
- SEC developer resources: https://www.sec.gov/about/developer-resources
- SEC EDGAR data APIs: https://data.sec.gov/
- SEC developer FAQ / declared User-Agent guidance: https://www.sec.gov/about/webmaster-frequently-asked-questions
- Federal Reserve RSS feeds: https://www.federalreserve.gov/feeds/feeds.htm
- Federal Reserve press releases: https://www.federalreserve.gov/newsevents/pressreleases.htm
- BLS latest releases: https://www.bls.gov/home.htm
- BLS CPI release page: https://www.bls.gov/cpi/
- BLS PPI release page: https://www.bls.gov/ppi/home.htm
- Treasury press releases: https://home.treasury.gov/news/press-releases
- FRED API: https://fred.stlouisfed.org/docs/api/fred/fred/
- Alpaca automated trading risk disclosure: https://files.alpaca.markets/disclosures/library/RisksAutoTrading.pdf
- IBKR API overview: https://www.interactivebrokers.com/en/trading/ib-api.php?menu=A
- IBKR Web API session docs: https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/
