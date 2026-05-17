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


def _portfolio_return(
    history: AlignedHistory, weights: dict[str, float], index: int
) -> float:
    total = 0.0
    for symbol, weight in weights.items():
        prior = history.closes[symbol][index]
        current = history.closes[symbol][index + 1]
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
