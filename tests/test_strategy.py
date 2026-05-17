from __future__ import annotations

import unittest
from datetime import date, timedelta

from ai_investing.models import StrategyParameters
from ai_investing.strategy import ETFMomentumStrategy, align_history


def _series(start: float, daily_return: float, length: int) -> list[float]:
    values = [start]
    for _ in range(length - 1):
        values.append(values[-1] * (1.0 + daily_return))
    return values


def _build_history() -> dict[str, dict[date, float]]:
    start = date(2023, 1, 2)
    length = 320
    symbols = {
        "SPY": _series(100, 0.0012, length),
        "QQQ": _series(100, 0.0014, length),
        "IWM": _series(100, 0.0011, length),
        "EFA": _series(100, 0.0009, length),
        "EEM": _series(100, 0.0008, length),
        "TLT": _series(100, -0.0002, length),
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


class StrategyTests(unittest.TestCase):
    def test_strategy_prefers_risk_on_assets_when_they_are_trending(self) -> None:
        history = align_history(_build_history())
        strategy = ETFMomentumStrategy(
            risk_on_universe=("SPY", "QQQ", "IWM", "EFA", "EEM"),
            defensive_universe=("TLT", "IEF", "GLD", "SHY"),
            params=StrategyParameters(),
        )

        signal = strategy.signal_for_index(history, len(history.dates) - 1)

        self.assertEqual(signal.regime, "risk_on")
        self.assertIn("QQQ", signal.weights)
        self.assertIn("SPY", signal.weights)
        self.assertLess(sum(signal.weights.values()), 1.0)

    def test_align_history_does_not_truncate_for_late_starting_symbols(self) -> None:
        history = _build_history()
        late_start = {
            current_date: price
            for current_date, price in list(history["QQQ"].items())[120:]
        }
        history["NEW"] = late_start

        aligned = align_history(history)

        self.assertEqual(aligned.dates[0], min(history["SPY"]))
        self.assertIsNone(aligned.closes["NEW"][0])
        self.assertEqual(len(aligned.dates), len(history["SPY"]))


if __name__ == "__main__":
    unittest.main()
