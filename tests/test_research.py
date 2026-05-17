from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ai_investing.models import (
    OfficialNewsContext,
    OfficialNewsItem,
    StrategyParameters,
)
from ai_investing.research import ResearchOverlay, load_research_snapshot
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
        "SPY": _series(100, 0.0010, length),
        "QQQ": _series(100, 0.0012, length),
        "TLT": _series(100, 0.0001, length),
        "IEF": _series(100, 0.0001, length),
    }
    return {
        symbol: {
            start + timedelta(days=index): price for index, price in enumerate(prices)
        }
        for symbol, prices in symbols.items()
    }


class ResearchTests(unittest.TestCase):
    def test_overlay_loads_and_scores_assets(self) -> None:
        snapshot = """{
  "as_of": "2026-05-17",
  "weights": {
    "quant": 0.35,
    "company": 0.30,
    "index": 0.20,
    "etf": 0.15,
    "minimum_total_score": 0.55
  },
  "assets": {
    "SPY": {
      "asset_type": "etf",
      "benchmark_index": "SPX",
      "etf": {
        "expense_ratio": 0.0009,
        "assets_under_management_billion": 500.0,
        "average_daily_dollar_volume_billion": 25.0,
        "tracking_error": 0.0007,
        "flow_1m_percent": 0.01,
        "flow_3m_percent": 0.02,
        "portfolio_quality_score": 0.70,
        "portfolio_valuation_score": 0.60
      }
    },
    "QQQ": {
      "asset_type": "etf",
      "benchmark_index": "NDX",
      "etf": {
        "expense_ratio": 0.0050,
        "assets_under_management_billion": 10.0,
        "average_daily_dollar_volume_billion": 0.10,
        "tracking_error": 0.0120,
        "flow_1m_percent": -0.04,
        "flow_3m_percent": -0.09,
        "portfolio_quality_score": 0.35,
        "portfolio_valuation_score": 0.25
      }
    },
    "SPX": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.65,
        "trend_score": 0.72,
        "relative_strength_score": 0.68,
        "volatility_percentile": 0.40,
        "credit_spread_percentile": 0.30,
        "yield_curve_slope_bps": 30.0
      }
    },
    "NDX": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.38,
        "trend_score": 0.41,
        "relative_strength_score": 0.33,
        "volatility_percentile": 0.75,
        "credit_spread_percentile": 0.70,
        "yield_curve_slope_bps": -40.0
      }
    }
  }
}"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            path.write_text(snapshot)
            overlay = ResearchOverlay(load_research_snapshot(path))

        spy = overlay.assess_symbol("SPY", 0.70)
        qqq = overlay.assess_symbol("QQQ", 0.90)

        self.assertGreater(spy.total_score, qqq.total_score)
        self.assertTrue(overlay.eligible_for_risk_on(spy))
        self.assertFalse(overlay.eligible_for_risk_on(qqq))

    def test_overlay_rejects_stale_and_future_snapshots(self) -> None:
        snapshot = """{
  "as_of": "2026-05-17",
  "assets": {}
}"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            path.write_text(snapshot)
            overlay = ResearchOverlay(load_research_snapshot(path))

        with self.assertRaisesRegex(ValueError, "future-dated"):
            overlay.validate_for_date(date(2026, 5, 16), max_age_days=45)
        with self.assertRaisesRegex(ValueError, "stale"):
            overlay.validate_for_date(date(2026, 8, 1), max_age_days=30)

    def test_strategy_uses_research_overlay_to_override_pure_momentum(self) -> None:
        snapshot = """{
  "as_of": "2026-05-17",
  "weights": {
    "quant": 0.35,
    "company": 0.30,
    "index": 0.20,
    "etf": 0.15,
    "minimum_total_score": 0.55
  },
  "assets": {
    "SPY": {
      "asset_type": "etf",
      "benchmark_index": "SPX",
      "etf": {
        "expense_ratio": 0.0009,
        "assets_under_management_billion": 500.0,
        "average_daily_dollar_volume_billion": 25.0,
        "tracking_error": 0.0007,
        "flow_1m_percent": 0.01,
        "flow_3m_percent": 0.02,
        "portfolio_quality_score": 0.70,
        "portfolio_valuation_score": 0.60
      }
    },
    "QQQ": {
      "asset_type": "etf",
      "benchmark_index": "NDX",
      "etf": {
        "expense_ratio": 0.0050,
        "assets_under_management_billion": 10.0,
        "average_daily_dollar_volume_billion": 0.10,
        "tracking_error": 0.0120,
        "flow_1m_percent": -0.04,
        "flow_3m_percent": -0.09,
        "portfolio_quality_score": 0.35,
        "portfolio_valuation_score": 0.25
      }
    },
    "SPX": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.65,
        "trend_score": 0.72,
        "relative_strength_score": 0.68,
        "volatility_percentile": 0.40,
        "credit_spread_percentile": 0.30,
        "yield_curve_slope_bps": 30.0
      }
    },
    "NDX": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.38,
        "trend_score": 0.41,
        "relative_strength_score": 0.33,
        "volatility_percentile": 0.75,
        "credit_spread_percentile": 0.70,
        "yield_curve_slope_bps": -40.0
      }
    }
  }
}"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            path.write_text(snapshot)
            overlay = ResearchOverlay(load_research_snapshot(path))

            history = align_history(_build_history())
            strategy = ETFMomentumStrategy(
                risk_on_universe=("SPY", "QQQ"),
                defensive_universe=("TLT", "IEF"),
                params=StrategyParameters(top_n=1, defensive_count=1),
                research_overlay=overlay,
            )
            signal = strategy.signal_for_index(history, len(history.dates) - 1)

        self.assertEqual(signal.regime, "risk_on")
        self.assertIn("SPY", signal.weights)
        self.assertNotIn("QQQ", signal.weights)

    def test_defensive_assets_are_also_filtered_by_research(self) -> None:
        snapshot = """{
  "as_of": "2026-05-17",
  "weights": {
    "quant": 0.35,
    "company": 0.30,
    "index": 0.20,
    "etf": 0.15,
    "minimum_total_score": 0.60
  },
  "assets": {
    "TLT": {
      "asset_type": "etf",
      "benchmark_index": "UST20",
      "etf": {
        "expense_ratio": 0.0090,
        "assets_under_management_billion": 2.0,
        "average_daily_dollar_volume_billion": 0.05,
        "tracking_error": 0.0180,
        "flow_1m_percent": -0.03,
        "flow_3m_percent": -0.08,
        "portfolio_quality_score": 0.20,
        "portfolio_valuation_score": 0.20
      }
    },
    "IEF": {
      "asset_type": "etf",
      "benchmark_index": "UST7_10",
      "etf": {
        "expense_ratio": 0.0090,
        "assets_under_management_billion": 2.0,
        "average_daily_dollar_volume_billion": 0.05,
        "tracking_error": 0.0180,
        "flow_1m_percent": -0.03,
        "flow_3m_percent": -0.08,
        "portfolio_quality_score": 0.20,
        "portfolio_valuation_score": 0.20
      }
    },
    "UST20": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.35,
        "trend_score": 0.30,
        "relative_strength_score": 0.25,
        "volatility_percentile": 0.80,
        "credit_spread_percentile": 0.80,
        "yield_curve_slope_bps": -60.0
      }
    },
    "UST7_10": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.35,
        "trend_score": 0.30,
        "relative_strength_score": 0.25,
        "volatility_percentile": 0.80,
        "credit_spread_percentile": 0.80,
        "yield_curve_slope_bps": -60.0
      }
    }
  }
}"""
        start = date(2023, 1, 2)
        length = 320
        prices = {
            "SPY": _series(100, -0.0010, length),
            "QQQ": _series(100, -0.0012, length),
            "TLT": _series(100, 0.0008, length),
            "IEF": _series(100, 0.0006, length),
        }
        history = align_history(
            {
                symbol: {
                    start + timedelta(days=index): price
                    for index, price in enumerate(series)
                }
                for symbol, series in prices.items()
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            path.write_text(snapshot)
            overlay = ResearchOverlay(load_research_snapshot(path))

            strategy = ETFMomentumStrategy(
                risk_on_universe=("SPY", "QQQ"),
                defensive_universe=("TLT", "IEF"),
                params=StrategyParameters(top_n=1, defensive_count=1),
                research_overlay=overlay,
            )
            signal = strategy.signal_for_index(history, len(history.dates) - 1)

        self.assertEqual(signal.regime, "risk_off")
        self.assertEqual(signal.weights, {})

    def test_overlay_can_include_official_news_component(self) -> None:
        snapshot = """{
  "as_of": "2026-05-17",
  "weights": {
    "quant": 0.35,
    "company": 0.30,
    "index": 0.20,
    "etf": 0.15,
    "minimum_total_score": 0.45
  },
  "assets": {
    "SPY": {
      "asset_type": "etf",
      "benchmark_index": "SPX",
      "etf": {
        "expense_ratio": 0.0009,
        "assets_under_management_billion": 500.0,
        "average_daily_dollar_volume_billion": 25.0,
        "tracking_error": 0.0007,
        "flow_1m_percent": 0.01,
        "flow_3m_percent": 0.02,
        "portfolio_quality_score": 0.70,
        "portfolio_valuation_score": 0.60
      }
    },
    "SPX": {
      "asset_type": "index",
      "index": {
        "breadth_percent_above_200dma": 0.65,
        "trend_score": 0.72,
        "relative_strength_score": 0.68,
        "volatility_percentile": 0.40,
        "credit_spread_percentile": 0.30,
        "yield_curve_slope_bps": 30.0
      }
    }
  }
}"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            path.write_text(snapshot)
            official_news = OfficialNewsContext(
                as_of=date(2026, 5, 17),
                lookback_days=14,
                risk_on_score=0.30,
                duration_score=0.60,
                cash_score=0.55,
                gold_score=0.50,
                items=(
                    OfficialNewsItem(
                        source="bls",
                        published_on=date(2026, 5, 13),
                        title="PPI for final demand advances 1.4% in April",
                        url="https://www.bls.gov/ppi/home.htm",
                        impact_scores={"risk_on": 0.30},
                    ),
                ),
            )
            overlay = ResearchOverlay(
                load_research_snapshot(path),
                official_news=official_news,
            )

        assessment = overlay.assess_symbol("SPY", 0.70)
        self.assertIn("news", assessment.component_scores)
        self.assertLess(assessment.component_scores["news"], 0.5)


if __name__ == "__main__":
    unittest.main()
