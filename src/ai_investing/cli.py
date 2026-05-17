from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .alpaca import AlpacaClient
from .backtest import (
    run_backtest,
    run_walk_forward_backtest,
    select_walk_forward_parameters,
)
from .config import (
    load_broker_config,
    load_runtime_config,
    require_broker_credentials,
)
from .execution import execute_rebalance
from .models import OfficialNewsContext, ResearchSnapshot, ResearchWeights, StrategyParameters
from .news import build_official_news_context, summarize_official_news
from .research import ResearchOverlay, load_research_snapshot
from .strategy import ETFMomentumStrategy, align_history


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Investing CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    paper_setup = subparsers.add_parser(
        "paper-setup",
        help="Create a paper-trading env file and print the next commands to run.",
    )
    paper_setup.add_argument("--env-file", default=".env.paper")
    paper_setup.add_argument("--research-snapshot", default=None)
    paper_setup.add_argument("--sec-user-agent", default=None)
    paper_setup.add_argument("--force", action="store_true")

    backtest = subparsers.add_parser("backtest", help="Run a historical backtest.")
    backtest.add_argument("--start", type=_parse_date, required=True)
    backtest.add_argument("--end", type=_parse_date, default=_today_utc())
    backtest.add_argument("--feed", default=None)
    backtest.add_argument("--no-optimize", action="store_true")
    backtest.add_argument("--training-window-bars", type=int, default=756)

    signal = subparsers.add_parser("signal", help="Generate current target weights.")
    signal.add_argument("--lookback-days", type=int, default=900)
    signal.add_argument("--feed", default=None)
    signal.add_argument("--research-snapshot", default=None)
    signal.add_argument("--training-window-bars", type=int, default=756)
    signal.add_argument("--no-official-news", action="store_true")
    signal.add_argument("--official-news-lookback-days", type=int, default=None)
    signal.add_argument("--require-official-news", action="store_true")

    trade = subparsers.add_parser("trade", help="Preview or submit a rebalance.")
    trade.add_argument("--lookback-days", type=int, default=900)
    trade.add_argument("--feed", default=None)
    trade.add_argument("--research-snapshot", default=None)
    trade.add_argument("--training-window-bars", type=int, default=756)
    trade.add_argument("--submit", action="store_true")
    trade.add_argument("--force", action="store_true")
    trade.add_argument("--no-official-news", action="store_true")
    trade.add_argument("--official-news-lookback-days", type=int, default=None)
    trade.add_argument("--require-official-news", action="store_true")

    research = subparsers.add_parser(
        "research", help="Run multi-layer company/index/ETF analysis."
    )
    research.add_argument("--lookback-days", type=int, default=900)
    research.add_argument("--feed", default=None)
    research.add_argument("--research-snapshot", default=None)
    research.add_argument("--training-window-bars", type=int, default=756)
    research.add_argument("--no-official-news", action="store_true")
    research.add_argument("--official-news-lookback-days", type=int, default=None)
    research.add_argument("--require-official-news", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    broker_config = load_broker_config()
    runtime_config = load_runtime_config()

    if args.command == "paper-setup":
        _run_paper_setup_command(broker_config, runtime_config, args)
        return

    if args.command == "research":
        research_snapshot_path = _resolve_research_snapshot_path(runtime_config, args)
        if _has_broker_credentials(broker_config):
            client = AlpacaClient(broker_config)
            _run_research_command(
                client, runtime_config, args, research_snapshot_path=research_snapshot_path
            )
            return
        _run_snapshot_only_research(runtime_config, research_snapshot_path, args)
        return

    require_broker_credentials(broker_config)
    client = AlpacaClient(broker_config)

    if args.command == "backtest":
        _run_backtest_command(client, runtime_config, args)
        return
    if args.command == "signal":
        _run_signal_command(client, runtime_config, args)
        return
    if args.command == "trade":
        _run_trade_command(client, broker_config.paper, runtime_config, args)
        return
    raise ValueError(f"Unsupported command: {args.command}")


def _run_backtest_command(
    client: AlpacaClient, runtime_config, args: argparse.Namespace
) -> None:
    history = _load_history(
        client=client,
        symbols=list(
            dict.fromkeys(
                list(runtime_config.risk_on_universe)
                + list(runtime_config.defensive_universe)
            )
        ),
        start=args.start,
        end=args.end,
        feed=args.feed or runtime_config.default_feed,
    )

    base_params = StrategyParameters()
    if args.no_optimize:
        strategy = ETFMomentumStrategy(
            risk_on_universe=runtime_config.risk_on_universe,
            defensive_universe=runtime_config.defensive_universe,
            params=base_params,
        )
        result = run_backtest(history, strategy)
        print("Selection mode: fixed parameters")
    else:
        result = run_walk_forward_backtest(
            history,
            risk_on_universe=runtime_config.risk_on_universe,
            defensive_universe=runtime_config.defensive_universe,
            base_params=base_params,
            training_window=args.training_window_bars,
        )
        print("Selection mode: walk-forward optimization")
    print("")

    print("Final Active Parameters")
    print(result.params)
    print("")
    print("Performance")
    print(f"Total return: {result.total_return:.2%}")
    print(f"CAGR: {result.cagr:.2%}")
    print(f"Annualized volatility: {result.annualized_volatility:.2%}")
    print(f"Sharpe: {result.sharpe:.2f}")
    print(f"Max drawdown: {result.max_drawdown:.2%}")
    print(f"Average turnover: {result.average_turnover:.2%}")
    print(f"Score: {result.score:.4f}")


def _run_paper_setup_command(
    broker_config,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    env_path = Path(args.env_file)
    if env_path.exists() and not args.force:
        raise FileExistsError(
            f"{env_path} already exists. Re-run with --force to overwrite it."
        )

    research_snapshot = _resolve_paper_setup_snapshot_path(runtime_config, args)
    sec_user_agent = _resolve_paper_setup_sec_user_agent(runtime_config, args)
    env_payload = _build_paper_env_payload(
        broker_config=broker_config,
        runtime_config=runtime_config,
        research_snapshot_path=research_snapshot,
        sec_user_agent=sec_user_agent,
    )
    env_path.write_text(_render_env_file(env_payload))

    print(f"Wrote paper config: {env_path}")
    print("")
    print("Paper Trading Settings")
    print("- ALPACA_PAPER=true")
    print("- AI_INVESTING_ENABLE_LIVE=0")
    print(f"- state file: {env_payload['AI_INVESTING_STATE_PATH']}")
    print(
        f"- credentials: {'present' if _has_broker_credentials(broker_config) else 'missing placeholders'}"
    )
    print("")
    print("Next Commands")
    print(f"- set -a; source {env_path}; set +a")
    if research_snapshot is not None:
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli research "
            f"--research-snapshot {research_snapshot}"
        )
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli signal "
            f"--research-snapshot {research_snapshot}"
        )
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli trade "
            f"--research-snapshot {research_snapshot}"
        )
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli trade --submit "
            f"--research-snapshot {research_snapshot}"
        )
    else:
        print("- PYTHONPATH=src python3 -m ai_investing.cli signal")
        print("- PYTHONPATH=src python3 -m ai_investing.cli trade")
        print("- PYTHONPATH=src python3 -m ai_investing.cli trade --submit")

    if not _has_broker_credentials(broker_config):
        print("")
        print(
            "Replace ALPACA_API_KEY and ALPACA_SECRET_KEY in the env file with your Alpaca paper credentials before submitting orders."
        )


def _run_signal_command(
    client: AlpacaClient, runtime_config, args: argparse.Namespace
) -> None:
    signal, _history, params, _signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        args=args,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=_resolve_research_snapshot_path(runtime_config, args),
        training_window_bars=args.training_window_bars,
    )
    print(f"Using parameters: {params}")
    _print_signal(signal)
    _print_official_news(signal.official_news)
    _print_selected_research(signal)


def _run_trade_command(
    client: AlpacaClient,
    is_paper: bool,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    clock = client.get_clock()
    if args.submit and not clock.is_open:
        raise RuntimeError(
            f"Market is closed at {clock.timestamp}. Refusing to submit market orders."
        )

    signal, history, params, signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        args=args,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=_resolve_research_snapshot_path(runtime_config, args),
        training_window_bars=args.training_window_bars,
    )
    latest_prices = {
        symbol: closes[signal_index] for symbol, closes in history.closes.items()
    }
    actions, responses, state = execute_rebalance(
        client=client,
        signal=signal,
        state_path=runtime_config.state_path,
        latest_prices=latest_prices,
        allow_live=runtime_config.enable_live,
        is_paper=is_paper,
        submit=args.submit,
        force=args.force,
        live_price_feed=args.feed or runtime_config.default_feed,
        max_price_drift_pct=runtime_config.max_price_drift_pct,
    )

    print(f"Using parameters: {params}")
    _print_signal(signal)
    _print_official_news(signal.official_news)
    _print_selected_research(signal)
    print("")
    if not actions:
        print("No rebalance required.")
    else:
        print("Planned Actions")
        for action in actions:
            if action.qty is None:
                details = f"${action.notional:.2f}"
            else:
                details = f"{action.qty:.6f} shares"
            print(
                f"- {action.side.upper()} {action.symbol}: {details} ({action.reason})"
            )

    if args.submit:
        print("")
        print(f"Submitted {len(responses)} orders.")
        print(f"High-water mark: ${state.high_water_mark:.2f}")


def _run_research_command(
    client: AlpacaClient,
    runtime_config,
    args: argparse.Namespace,
    *,
    research_snapshot_path: Path | None,
) -> None:
    if research_snapshot_path is None:
        raise RuntimeError(
            "Research mode requires a snapshot file. Set AI_INVESTING_RESEARCH_SNAPSHOT_PATH or pass --research-snapshot."
        )
    signal, _history, params, _signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        args=args,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=research_snapshot_path,
        training_window_bars=args.training_window_bars,
    )
    print(f"Using parameters: {params}")
    _print_signal(signal)
    _print_official_news(signal.official_news)
    print("")
    print("Research Scorecard")
    _print_all_research(signal)


def _run_snapshot_only_research(
    runtime_config,
    research_snapshot_path: Path | None,
    args: argparse.Namespace | None = None,
) -> None:
    if research_snapshot_path is None:
        raise RuntimeError(
            "Research mode without broker credentials requires a local research snapshot."
        )
    snapshot = load_research_snapshot(research_snapshot_path)
    official_news = _load_official_news_context(
        runtime_config=runtime_config,
        args=args,
        snapshot=snapshot,
    )
    overlay = _build_research_overlay(snapshot, official_news)
    assert overlay is not None
    print("Mode: research snapshot only")
    print("Quant component: neutral 0.50")
    _print_official_news(official_news)
    print("")
    assessments = [
        overlay.assess_symbol(symbol, 0.5)
        for symbol in sorted(overlay.snapshot.assets)
    ]
    for assessment in sorted(
        assessments, key=lambda item: item.total_score, reverse=True
    ):
        print(_format_assessment_line(assessment))


def _compute_latest_signal(
    client: AlpacaClient,
    runtime_config,
    args: argparse.Namespace,
    lookback_days: int,
    feed: str,
    research_snapshot_path: Path | None = None,
    training_window_bars: int = 756,
):
    end = _today_utc()
    start = end - timedelta(days=lookback_days)
    symbols = list(
        dict.fromkeys(
            list(runtime_config.risk_on_universe) + list(runtime_config.defensive_universe)
        )
    )
    history = _load_history(
        client=client,
        symbols=symbols,
        start=start,
        end=end,
        feed=feed,
    )
    signal_index = len(history.dates) - 1
    if history.dates[signal_index] >= _today_utc():
        signal_index -= 1
    snapshot = (
        load_research_snapshot(research_snapshot_path)
        if research_snapshot_path is not None
        else None
    )
    official_news = _load_official_news_context(
        runtime_config=runtime_config,
        args=args,
        snapshot=snapshot,
    )
    research_overlay = _build_research_overlay(snapshot, official_news)
    if snapshot is not None and research_overlay is not None:
        research_overlay.validate_for_date(
            history.dates[signal_index],
            max_age_days=runtime_config.research_max_age_days,
        )
    best_result = select_walk_forward_parameters(
        history,
        risk_on_universe=runtime_config.risk_on_universe,
        defensive_universe=runtime_config.defensive_universe,
        base_params=StrategyParameters(),
        signal_index=signal_index,
        training_window=training_window_bars,
    )
    strategy = ETFMomentumStrategy(
        risk_on_universe=runtime_config.risk_on_universe,
        defensive_universe=runtime_config.defensive_universe,
        params=best_result.params,
        research_overlay=research_overlay,
    )
    signal = replace(strategy.signal_for_index(history, signal_index), official_news=official_news)
    return signal, history, best_result.params, signal_index


def _load_history(
    *,
    client: AlpacaClient,
    symbols: list[str],
    start: date,
    end: date,
    feed: str,
):
    raw_history = client.get_daily_closes(
        symbols=symbols,
        start=start,
        end=end,
        feed=feed,
    )
    return align_history(raw_history)


def _print_signal(signal) -> None:
    print(f"As of: {signal.as_of.isoformat()}")
    print(f"Regime: {signal.regime}")
    print("Target weights:")
    for symbol, weight in sorted(signal.weights.items()):
        print(f"- {symbol}: {weight:.2%}")
    cash = 1.0 - sum(signal.weights.values())
    print(f"- CASH: {cash:.2%}")


def _print_official_news(official_news: OfficialNewsContext | None) -> None:
    if official_news is None:
        return
    print("")
    print("Official News Context")
    if official_news.risk_on_score is not None:
        print(f"- Risk assets: {official_news.risk_on_score:.2f}")
    if official_news.duration_score is not None:
        print(f"- Duration: {official_news.duration_score:.2f}")
    if official_news.cash_score is not None:
        print(f"- Cash / short duration: {official_news.cash_score:.2f}")
    if official_news.gold_score is not None:
        print(f"- Gold: {official_news.gold_score:.2f}")
    for source_name, status in sorted(official_news.source_status.items()):
        print(f"- {source_name}: {status}")
    for line in summarize_official_news(official_news, limit=4):
        print(f"- headline: {line}")


def _print_selected_research(signal) -> None:
    if not signal.assessments:
        return
    print("")
    print("Selected Research")
    selected_symbols = set(signal.weights)
    selected = [
        signal.assessments[symbol]
        for symbol in selected_symbols
        if symbol in signal.assessments
    ]
    if not selected:
        return
    for assessment in sorted(selected, key=lambda item: item.total_score, reverse=True):
        print(_format_assessment_line(assessment))


def _print_all_research(signal) -> None:
    if not signal.assessments:
        print("No research overlay loaded.")
        return
    for assessment in sorted(
        signal.assessments.values(), key=lambda item: item.total_score, reverse=True
    ):
        print(_format_assessment_line(assessment))


def _format_assessment_line(assessment) -> str:
    component_parts = [
        f"{name}={score:.2f}"
        for name, score in sorted(assessment.component_scores.items())
    ]
    research = (
        f" | research={assessment.research_score:.2f}"
        if assessment.research_score is not None
        else ""
    )
    benchmark = (
        f" | benchmark={assessment.benchmark_index}"
        if assessment.benchmark_index
        else ""
    )
    return (
        f"- {assessment.symbol}: total={assessment.total_score:.2f}"
        f"{research} | {' '.join(component_parts)}{benchmark}"
    )


def _build_research_overlay(
    snapshot: ResearchSnapshot | None,
    official_news: OfficialNewsContext | None,
) -> ResearchOverlay | None:
    if snapshot is None and official_news is None:
        return None
    if snapshot is None:
        snapshot = ResearchSnapshot(
            as_of=official_news.as_of,
            weights=ResearchWeights(
                quant=0.80,
                company=0.0,
                index=0.0,
                etf=0.0,
                news=0.20,
                minimum_total_score=0.35,
            ),
            assets={},
        )
    return ResearchOverlay(snapshot, official_news=official_news)


def _load_official_news_context(
    *,
    runtime_config,
    args: argparse.Namespace | None,
    snapshot: ResearchSnapshot | None,
) -> OfficialNewsContext | None:
    if args is not None and getattr(args, "no_official_news", False):
        return None
    if not runtime_config.enable_official_news:
        return None
    lookback_days = (
        getattr(args, "official_news_lookback_days", None)
        or runtime_config.official_news_lookback_days
    )
    require_success = runtime_config.require_official_news or bool(
        args is not None and getattr(args, "require_official_news", False)
    )
    return build_official_news_context(
        as_of=_today_utc(),
        lookback_days=lookback_days,
        user_agent=runtime_config.sec_user_agent,
        assets=snapshot.assets if snapshot is not None else None,
        require_success=require_success,
    )


def _has_broker_credentials(broker_config) -> bool:
    return bool(broker_config.api_key and broker_config.secret_key)


def _resolve_research_snapshot_path(
    runtime_config, args: argparse.Namespace
) -> Path | None:
    raw_value = getattr(args, "research_snapshot", None)
    if raw_value:
        return Path(raw_value)
    return runtime_config.research_snapshot_path


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _build_paper_env_payload(
    *,
    broker_config,
    runtime_config,
    research_snapshot_path: Path | None,
    sec_user_agent: str,
) -> dict[str, str]:
    return {
        "ALPACA_API_KEY": broker_config.api_key or "your-paper-key",
        "ALPACA_SECRET_KEY": broker_config.secret_key or "your-paper-secret",
        "ALPACA_PAPER": "true",
        "AI_INVESTING_ENABLE_LIVE": "0",
        "AI_INVESTING_STATE_PATH": ".ai_investing_paper_state.json",
        "AI_INVESTING_DEFAULT_FEED": runtime_config.default_feed,
        "AI_INVESTING_RISK_ON": ",".join(runtime_config.risk_on_universe),
        "AI_INVESTING_DEFENSIVE": ",".join(runtime_config.defensive_universe),
        "AI_INVESTING_RESEARCH_SNAPSHOT_PATH": (
            str(research_snapshot_path) if research_snapshot_path is not None else ""
        ),
        "AI_INVESTING_RESEARCH_MAX_AGE_DAYS": str(
            runtime_config.research_max_age_days
        ),
        "AI_INVESTING_ENABLE_OFFICIAL_NEWS": (
            "1" if runtime_config.enable_official_news else "0"
        ),
        "AI_INVESTING_OFFICIAL_NEWS_LOOKBACK_DAYS": str(
            runtime_config.official_news_lookback_days
        ),
        "AI_INVESTING_REQUIRE_OFFICIAL_NEWS": (
            "1" if runtime_config.require_official_news else "0"
        ),
        "AI_INVESTING_SEC_USER_AGENT": sec_user_agent,
        "AI_INVESTING_MAX_PRICE_DRIFT_PCT": str(runtime_config.max_price_drift_pct),
    }


def _render_env_file(values: dict[str, str]) -> str:
    lines = [f"{key}={_env_quote(value)}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def _env_quote(value: str) -> str:
    if value == "":
        return ""
    if any(character.isspace() for character in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _resolve_paper_setup_snapshot_path(
    runtime_config,
    args: argparse.Namespace,
) -> Path | None:
    raw_value = getattr(args, "research_snapshot", None)
    if raw_value:
        return Path(raw_value)
    if runtime_config.research_snapshot_path is not None:
        return runtime_config.research_snapshot_path
    default_snapshot = Path("examples/research_snapshot.example.json")
    if default_snapshot.exists():
        return default_snapshot
    return None


def _resolve_paper_setup_sec_user_agent(
    runtime_config,
    args: argparse.Namespace,
) -> str:
    raw_value = getattr(args, "sec_user_agent", None)
    if raw_value:
        return raw_value
    return runtime_config.sec_user_agent
