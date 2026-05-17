from __future__ import annotations

import unittest
from datetime import date, timedelta

from ai_investing.backtest import optimize_strategy, run_backtest
from ai_investing.models import StrategyParameters
from ai_investing.strategy import ETFMomentumStrategy, align_history


def _series(start: float, daily_return: float, length: int) -> list[float]:
    values = [start]
    for _ in range(length - 1):
        values.append(values[-1] * (1.0 + daily_return))
    return values


def _build_history() -> dict[str, dict[date, float]]:
    start = date(2022, 1, 3)
    length = 420
    symbols = {
        "SPY": _series(100, 0.0010, length),
        "QQQ": _series(100, 0.0012, length),
        "IWM": _series(100, 0.0009, length),
        "EFA": _series(100, 0.0008, length),
        "EEM": _series(100, 0.0007, length),
        "TLT": _series(100, 0.0001, length),
        "IEF": _series(100, 0.0001, length),
        "GLD": _series(100, 0.0002, length),
        "SHY": _series(100, 0.0001, length),
    }
    return {
        symbol: {
            start + timedelta(days=index): price for index, price in enumerate(prices)
        }
        for symbol, prices in symbols.items()
    }


class BacktestTests(unittest.TestCase):
    def test_backtest_returns_metrics(self) -> None:
        history = align_history(_build_history())
        strategy = ETFMomentumStrategy(
            risk_on_universe=("SPY", "QQQ", "IWM", "EFA", "EEM"),
            defensive_universe=("TLT", "IEF", "GLD", "SHY"),
            params=StrategyParameters(),
        )

        result = run_backtest(history, strategy)

        self.assertGreater(result.total_return, 0)
        self.assertGreater(result.cagr, 0)
        self.assertGreaterEqual(result.max_drawdown, 0)

    def test_optimizer_returns_best_candidate(self) -> None:
        history = align_history(_build_history())
        result = optimize_strategy(
            history,
            risk_on_universe=("SPY", "QQQ", "IWM", "EFA", "EEM"),
            defensive_universe=("TLT", "IEF", "GLD", "SHY"),
            base_params=StrategyParameters(),
        )

        self.assertIn(result.params.rebalance_frequency, {"weekly", "monthly"})
        self.assertNotEqual(result.score, 0)


if __name__ == "__main__":
    unittest.main()
