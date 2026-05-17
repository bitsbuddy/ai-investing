from __future__ import annotations

import argparse
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
from .models import StrategyParameters
from .research import ResearchOverlay, load_research_snapshot
from .strategy import ETFMomentumStrategy, align_history


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Investing CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    trade = subparsers.add_parser("trade", help="Preview or submit a rebalance.")
    trade.add_argument("--lookback-days", type=int, default=900)
    trade.add_argument("--feed", default=None)
    trade.add_argument("--research-snapshot", default=None)
    trade.add_argument("--training-window-bars", type=int, default=756)
    trade.add_argument("--submit", action="store_true")
    trade.add_argument("--force", action="store_true")

    research = subparsers.add_parser(
        "research", help="Run multi-layer company/index/ETF analysis."
    )
    research.add_argument("--lookback-days", type=int, default=900)
    research.add_argument("--feed", default=None)
    research.add_argument("--research-snapshot", default=None)
    research.add_argument("--training-window-bars", type=int, default=756)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    broker_config = load_broker_config()
    runtime_config = load_runtime_config()
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
    if args.command == "research":
        _run_research_command(client, runtime_config, args)
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

    print("Best Parameters")
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


def _run_signal_command(
    client: AlpacaClient, runtime_config, args: argparse.Namespace
) -> None:
    signal, _history, params, _signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=_resolve_research_snapshot_path(runtime_config, args),
        training_window_bars=args.training_window_bars,
    )
    print(f"Using parameters: {params}")
    _print_signal(signal)
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
    client: AlpacaClient, runtime_config, args: argparse.Namespace
) -> None:
    research_snapshot_path = _resolve_research_snapshot_path(runtime_config, args)
    if research_snapshot_path is None:
        raise RuntimeError(
            "Research mode requires a snapshot file. Set AI_INVESTING_RESEARCH_SNAPSHOT_PATH or pass --research-snapshot."
        )
    signal, _history, params, _signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=research_snapshot_path,
        training_window_bars=args.training_window_bars,
    )
    print(f"Using parameters: {params}")
    _print_signal(signal)
    print("")
    print("Research Scorecard")
    _print_all_research(signal)


def _compute_latest_signal(
    client: AlpacaClient,
    runtime_config,
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
    research_overlay = _load_research_overlay(research_snapshot_path)
    if research_overlay is not None:
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
    signal = strategy.signal_for_index(history, signal_index)
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


def _load_research_overlay(
    research_snapshot_path: Path | None,
) -> ResearchOverlay | None:
    if research_snapshot_path is None:
        return None
    snapshot = load_research_snapshot(research_snapshot_path)
    return ResearchOverlay(snapshot)


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
