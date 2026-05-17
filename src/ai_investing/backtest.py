from __future__ import annotations

import math
from statistics import mean, pstdev

from .models import AlignedHistory, BacktestResult, Signal, StrategyParameters
from .strategy import ETFMomentumStrategy


def run_backtest(
    history: AlignedHistory,
    strategy: ETFMomentumStrategy,
    *,
    transaction_cost_bps: float = 5.0,
) -> BacktestResult:
    params = strategy.params
    warmup = strategy.warmup_bars
    if len(history.dates) <= warmup + 1:
        raise ValueError("Not enough data to backtest this strategy.")

    equity = 1.0
    equity_curve = [equity]
    daily_returns: list[float] = []
    last_weights: dict[str, float] = {}
    last_rebalance_index: int | None = None
    turnover_values: list[float] = []

    for index in range(warmup, len(history.dates) - 1):
        if strategy.next_rebalance_index(history, index, last_rebalance_index):
            signal = strategy.signal_for_index(history, index)
            target_weights = signal.weights
            turnover = _turnover(last_weights, target_weights)
            last_weights = target_weights
            last_rebalance_index = index
            turnover_values.append(turnover)
        else:
            signal = Signal(
                as_of=history.dates[index],
                regime="hold",
                weights=last_weights,
                diagnostics={},
            )
            turnover = 0.0

        portfolio_return = _portfolio_return(history, signal.weights, index)
        trading_cost = (turnover * transaction_cost_bps) / 10000.0
        net_return = portfolio_return - trading_cost
        daily_returns.append(net_return)
        equity *= 1.0 + net_return
        equity_curve.append(equity)

    total_return = equity - 1.0
    years = max((history.dates[-1] - history.dates[warmup]).days / 365.25, 1 / 365.25)
    cagr = (equity ** (1.0 / years)) - 1.0 if equity > 0 else -1.0
    daily_vol = pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    annualized_volatility = daily_vol * math.sqrt(252)
    sharpe = (
        (mean(daily_returns) / daily_vol) * math.sqrt(252)
        if daily_vol > 0
        else 0.0
    )
    max_drawdown = _max_drawdown(equity_curve)
    average_turnover = mean(turnover_values) if turnover_values else 0.0
    score = sharpe + (cagr * 0.5) - (max_drawdown * 1.5)

    return BacktestResult(
        params=params,
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        average_turnover=average_turnover,
        equity_curve=equity_curve,
        score=score,
    )


def optimize_strategy(
    history: AlignedHistory,
    *,
    risk_on_universe: tuple[str, ...],
    defensive_universe: tuple[str, ...],
    base_params: StrategyParameters,
) -> BacktestResult:
    seed_strategy = ETFMomentumStrategy(
        risk_on_universe=risk_on_universe,
        defensive_universe=defensive_universe,
        params=base_params,
    )
    results: list[BacktestResult] = []
    for params in seed_strategy.parameter_grid():
        strategy = ETFMomentumStrategy(
            risk_on_universe=risk_on_universe,
            defensive_universe=defensive_universe,
            params=params,
        )
        results.append(run_backtest(history, strategy))
    return max(results, key=lambda result: result.score)


def select_walk_forward_parameters(
    history: AlignedHistory,
    *,
    risk_on_universe: tuple[str, ...],
    defensive_universe: tuple[str, ...],
    base_params: StrategyParameters,
    signal_index: int,
    training_window: int = 756,
) -> BacktestResult:
    if signal_index < 2:
        raise ValueError("Need more history before selecting parameters.")

    training_end = signal_index - 1
    training_start = max(0, training_end - training_window + 1)
    training_history = slice_history(history, training_start, training_end)
    return optimize_strategy(
        training_history,
        risk_on_universe=risk_on_universe,
        defensive_universe=defensive_universe,
        base_params=base_params,
    )


def run_walk_forward_backtest(
    history: AlignedHistory,
    *,
    risk_on_universe: tuple[str, ...],
    defensive_universe: tuple[str, ...],
    base_params: StrategyParameters,
    training_window: int = 756,
    parameter_reselection_frequency: str = "monthly",
    transaction_cost_bps: float = 5.0,
) -> BacktestResult:
    seed_strategy = ETFMomentumStrategy(
        risk_on_universe=risk_on_universe,
        defensive_universe=defensive_universe,
        params=base_params,
    )
    candidate_params = seed_strategy.parameter_grid()
    max_warmup = max(
        ETFMomentumStrategy(
            risk_on_universe=risk_on_universe,
            defensive_universe=defensive_universe,
            params=params,
        ).warmup_bars
        for params in candidate_params
    )
    start_index = max(max_warmup, training_window - 1)
    if len(history.dates) <= start_index + 1:
        raise ValueError("Not enough data to run walk-forward backtest.")

    equity = 1.0
    equity_curve = [equity]
    daily_returns: list[float] = []
    last_weights: dict[str, float] = {}
    last_rebalance_index: int | None = None
    turnover_values: list[float] = []
    current_strategy: ETFMomentumStrategy | None = None
    final_params = base_params
    last_selection_date = None

    for index in range(start_index, len(history.dates) - 1):
        current_date = history.dates[index]
        refresh_parameters = current_strategy is None or _should_refresh_parameter_selection(
            current_date,
            last_selection_date,
            parameter_reselection_frequency,
        )
        params_changed = False
        if refresh_parameters:
            best_result = select_walk_forward_parameters(
                history,
                risk_on_universe=risk_on_universe,
                defensive_universe=defensive_universe,
                base_params=base_params,
                signal_index=index,
                training_window=training_window,
            )
            next_strategy = ETFMomentumStrategy(
                risk_on_universe=risk_on_universe,
                defensive_universe=defensive_universe,
                params=best_result.params,
            )
            params_changed = (
                current_strategy is None
                or next_strategy.params != current_strategy.params
            )
            current_strategy = next_strategy
            final_params = best_result.params
            last_selection_date = current_date

        assert current_strategy is not None
        if params_changed or current_strategy.next_rebalance_index(
            history, index, last_rebalance_index
        ):
            signal = current_strategy.signal_for_index(history, index)
            target_weights = signal.weights
            turnover = _turnover(last_weights, target_weights)
            last_weights = target_weights
            last_rebalance_index = index
            turnover_values.append(turnover)
        else:
            signal = Signal(
                as_of=history.dates[index],
                regime="hold",
                weights=last_weights,
                diagnostics={},
            )
            turnover = 0.0

        portfolio_return = _portfolio_return(history, signal.weights, index)
        trading_cost = (turnover * transaction_cost_bps) / 10000.0
        net_return = portfolio_return - trading_cost
        daily_returns.append(net_return)
        equity *= 1.0 + net_return
        equity_curve.append(equity)

    total_return = equity - 1.0
    years = max(
        (history.dates[-1] - history.dates[start_index]).days / 365.25, 1 / 365.25
    )
    cagr = (equity ** (1.0 / years)) - 1.0 if equity > 0 else -1.0
    daily_vol = pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    annualized_volatility = daily_vol * math.sqrt(252)
    sharpe = (
        (mean(daily_returns) / daily_vol) * math.sqrt(252)
        if daily_vol > 0
        else 0.0
    )
    max_drawdown = _max_drawdown(equity_curve)
    average_turnover = mean(turnover_values) if turnover_values else 0.0
    score = sharpe + (cagr * 0.5) - (max_drawdown * 1.5)

    return BacktestResult(
        params=final_params,
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        average_turnover=average_turnover,
        equity_curve=equity_curve,
        score=score,
    )


def _portfolio_return(
    history: AlignedHistory, weights: dict[str, float], index: int
) -> float:
    total = 0.0
    for symbol, weight in weights.items():
        prior = history.closes[symbol][index]
        current = history.closes[symbol][index + 1]
        if prior is None or current is None:
            raise ValueError(
                f"Missing price history for {symbol} around {history.dates[index].isoformat()}."
            )
        total += weight * ((current / prior) - 1.0)
    return total


def _turnover(previous: dict[str, float], current: dict[str, float]) -> float:
    symbols = set(previous) | set(current)
    return sum(abs(current.get(symbol, 0.0) - previous.get(symbol, 0.0)) for symbol in symbols)


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = (peak - value) / peak if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def slice_history(
    history: AlignedHistory, start_index: int, end_index: int
) -> AlignedHistory:
    if start_index < 0 or end_index >= len(history.dates) or start_index > end_index:
        raise ValueError("Invalid history slice requested.")
    return AlignedHistory(
        dates=history.dates[start_index : end_index + 1],
        closes={
            symbol: closes[start_index : end_index + 1]
            for symbol, closes in history.closes.items()
        },
    )


def _should_refresh_parameter_selection(
    current_date, last_selection_date, frequency: str
) -> bool:
    if last_selection_date is None:
        return True
    if frequency == "monthly":
        return (
            current_date.year != last_selection_date.year
            or current_date.month != last_selection_date.month
        )
    if frequency == "weekly":
        return current_date.isocalendar()[:2] != last_selection_date.isocalendar()[:2]
    raise ValueError(f"Unsupported parameter reselection frequency: {frequency}")
