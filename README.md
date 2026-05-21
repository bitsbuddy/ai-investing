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
- Runs a multi-sleeve momentum and trend-following strategy across ETFs and equities with a defensive regime.
- Uses walk-forward parameter selection instead of full-sample parameter fitting.
- Optionally overlays company, index, ETF, and official-source news context onto the signal engine.
- Generates target portfolio weights for the current session.
- Supports multiple risk-profile variants, each with its own Alpaca account and state file.
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

The default strategy now starts from three liquid sleeves:

- Risk-on ETFs: `SPY, QQQ, IWM, EFA, EEM`
- Equity sleeve: `MSFT, NVDA, AMZN, GOOGL, META, JPM, LLY, XOM, COST, AVGO`
- Defensive ETFs: `TLT, IEF, GLD, SHY`

Base quant logic:

1. Compute weighted momentum over multiple lookback windows.
2. Require ETFs and equities to be above a long-term moving average before they are eligible for risk-on allocation.
3. Select separate ETF and equity sleeves instead of a single top-`N` list.
4. Blend quant, index, ETF, company, and official-news scores before ranking candidates.
5. Apply diversification constraints such as sector-aware equity selection and per-position caps.
6. Fall back to defensive ETFs when the risk-on opportunity set is too narrow.
7. Keep a cash buffer and explicit exposure caps rather than fully concentrating in a few names.

This remains deliberately simple on the execution side. Alpaca's own automated-trading disclosures warn against over-optimization and explicitly note that its platform is not intended for high-frequency trading.

## Risk Profiles

The system now supports three built-in risk appetites through `AI_INVESTING_RISK_PROFILE`:

- `conservative`
- `balanced`
- `aggressive`

They differ in base cash buffer, position concentration, rebalance cadence, and tactical aggressiveness. The walk-forward optimizer still runs inside each profile, but each profile starts from a different base parameter set.

This is intended for profile-level experimentation such as:

- separate Alpaca paper accounts
- separate state files
- separate performance baselines
- side-by-side comparison of conservative vs balanced vs aggressive behavior

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
  - optional LLM-based structured classification of official-source documents for more nuanced scoring

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

### Optional LLM News Layer

The default official-news layer is deterministic and rule-based. If you want the system to interpret release text with more nuance, you can enable the LLM overlay on top of the official-source fetchers.

Set:

```env
OPENAI_API_KEY=your_openai_key
AI_INVESTING_ENABLE_LLM_NEWS=1
AI_INVESTING_LLM_NEWS_MODEL=gpt-5-mini
```

Optional controls:

```env
AI_INVESTING_REQUIRE_LLM_NEWS=0
AI_INVESTING_LLM_NEWS_MAX_ITEMS=8
AI_INVESTING_LLM_NEWS_MAX_CHARS=6000
AI_INVESTING_OPENAI_BASE_URL=https://api.openai.com/v1
```

What it does:

- fetches official-source items as before
- pulls actual page text when available, especially Fed releases and SEC filing documents
- asks the model for strict JSON-schema output
- stores a short machine summary plus confidence per item
- blends the model scores with the rule-based scores instead of blindly replacing them

If the LLM call fails and `AI_INVESTING_REQUIRE_LLM_NEWS=0`, the system falls back to the rule-based official-news layer.

The equity sleeve is configurable through `AI_INVESTING_EQUITIES`. For example:

```bash
export AI_INVESTING_EQUITIES=MSFT,NVDA,AMZN,GOOGL,META,JPM
```

You can still change the ETF sleeves independently:

```bash
export AI_INVESTING_RISK_ON=SPY,QQQ,IWM,XLF,SMH
```

If you broaden or replace the equity sleeve, provide company metrics and sectors for those names in the research snapshot.

## Quick Start

If you want a step-by-step operator guide, read [HOW_TO_USE.md](/Users/rishik/AI-Investing/HOW_TO_USE.md:1).

1. Create an Alpaca account and generate API credentials.
2. Run `paper-setup` for a paper-trading env file, or copy `.env.example` to your own environment file and set the values manually.
3. Run commands with `PYTHONPATH=src` or install the package locally.

### One-Command Paper Setup

```bash
PYTHONPATH=src python3 -m ai_investing.cli paper-setup
```

By default this writes `.env.paper` with safe paper-trading settings:

- `ALPACA_PAPER=true`
- `AI_INVESTING_ENABLE_LIVE=0`
- a separate paper state file
- the example research snapshot path when available

Then load it and run the paper workflow:

```bash
set -a; source .env.paper; set +a
PYTHONPATH=src python3 -m ai_investing.cli signal --research-snapshot examples/research_snapshot.example.json
PYTHONPATH=src python3 -m ai_investing.cli trade --research-snapshot examples/research_snapshot.example.json
PYTHONPATH=src python3 -m ai_investing.cli trade --submit --research-snapshot examples/research_snapshot.example.json
```

### Multi-Profile Setup

If you want to run multiple risk appetites in parallel with different Alpaca keys, generate a profile matrix:

```bash
PYTHONPATH=src python3 -m ai_investing.cli multi-profile-setup
```

This writes:

- `profiles/conservative.paper.env`
- `profiles/balanced.paper.env`
- `profiles/aggressive.paper.env`
- `profiles/profile_matrix.json`

Each env file gets:

- its own `AI_INVESTING_RISK_PROFILE`
- its own `AI_INVESTING_STATE_PATH`
- a default `AI_INVESTING_PERFORMANCE_BASELINE=100000`

Replace the Alpaca key and secret in each env file with a different paper account if you want a clean live paper comparison.

Run all profiles:

```bash
PYTHONPATH=src python3 -m ai_investing.cli multi-profile-run --manifest profiles/profile_matrix.json
```

Compare which profile is currently performing best:

```bash
PYTHONPATH=src python3 -m ai_investing.cli multi-profile-report --manifest profiles/profile_matrix.json
```

### One-Command Automation Setup

If you want unattended paper trading, generate a runner script and cron template:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-setup
```

This writes:

- `scripts/run_paper_trade.sh`
- `automation/paper_trade.cron`

Then install the cron schedule:

```bash
crontab automation/paper_trade.cron
```

The default schedule is weekdays at `09:40`, `12:30`, and `15:45` in the machine's local timezone, and the log file is `logs/paper-trade.log`.

### Simple UI For Start / Stop

If you want a small local control panel, run:

```bash
PYTHONPATH=src python3 -m ai_investing.cli automation-ui
```

Then open `http://127.0.0.1:8787`.

The UI toggles the automation control file used by the scheduled runner, lets you force an immediate manual run, and shows high-level progress for the latest run, current runner state, and recent log output so you can see what the automation is doing without tailing files manually.

If `profiles/profile_matrix.json` exists, the same UI also exposes multi-profile controls:

- edit `AI_INVESTING_PROFILE_NAME`
- edit `AI_INVESTING_RISK_PROFILE`
- edit each profile's Alpaca paper key and secret
- edit each profile's performance baseline
- run one profile now
- run all profiles now in parallel

Those profile controls update the env files in `profiles/*.paper.env` directly and write per-profile UI run logs under `logs/profiles/`.

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

When the LLM layer is enabled, the source status list will also include an `llm` line and the headline summaries will include the model-generated short summaries.

If you hit local TLS certificate issues on macOS or another custom Python install, you can point the system at a CA bundle with `AI_INVESTING_CA_BUNDLE=/path/to/cacert.pem`. As a last resort for local paper testing only, you can set `AI_INVESTING_SSL_NO_VERIFY=1`, but that should not be your steady-state configuration.

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

You can now generate a repo-local version of that schedule automatically with `automation-setup`.

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
