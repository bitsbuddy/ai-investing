from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .news import score_official_news_for_asset
from .models import (
    CompanyMetrics,
    ETFMetrics,
    IndexMetrics,
    OfficialNewsContext,
    ResearchAsset,
    ResearchAssessment,
    ResearchSnapshot,
    ResearchWeights,
)


def load_research_snapshot(path: Path) -> ResearchSnapshot:
    payload = json.loads(path.read_text())
    weights = ResearchWeights(**payload.get("weights", {}))
    assets: dict[str, ResearchAsset] = {}
    for symbol, asset_payload in payload.get("assets", {}).items():
        company_payload = asset_payload.get("company")
        etf_payload = asset_payload.get("etf")
        index_payload = asset_payload.get("index")
        assets[symbol.upper()] = ResearchAsset(
            symbol=symbol.upper(),
            asset_type=str(asset_payload.get("asset_type", "unknown")).lower(),
            benchmark_index=_maybe_upper(asset_payload.get("benchmark_index")),
            company=CompanyMetrics(**company_payload) if company_payload else None,
            etf=ETFMetrics(**etf_payload) if etf_payload else None,
            index=IndexMetrics(**index_payload) if index_payload else None,
        )

    return ResearchSnapshot(
        as_of=_parse_date(payload["as_of"]),
        weights=weights,
        assets=assets,
    )


class ResearchOverlay:
    def __init__(
        self,
        snapshot: ResearchSnapshot,
        *,
        official_news: OfficialNewsContext | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.official_news = official_news

    def validate_for_date(self, decision_date: date, *, max_age_days: int) -> None:
        if self.snapshot.as_of > decision_date:
            raise ValueError(
                "Research snapshot is future-dated relative to the decision date."
            )
        age_days = (decision_date - self.snapshot.as_of).days
        if age_days > max_age_days:
            raise ValueError(
                f"Research snapshot is stale ({age_days} days old). "
                f"Maximum allowed age is {max_age_days} days."
            )

    def assess_symbol(self, symbol: str, quant_score: float) -> ResearchAssessment:
        symbol = symbol.upper()
        asset = self.snapshot.assets.get(symbol)
        notes: list[str] = []
        components = {"quant": _clamp01(quant_score)}
        benchmark_index = asset.benchmark_index if asset is not None else None
        asset_type = asset.asset_type if asset is not None else None

        if asset is None:
            notes.append("No research snapshot entry; using quant and live official news only.")

        if asset is not None and asset.company is not None:
            components["company"] = _score_company(asset.company)
        if asset is not None and asset.etf is not None:
            components["etf"] = _score_etf(asset.etf)

        if asset is not None and asset.index is not None:
            components["index"] = _score_index(asset.index)
        elif benchmark_index:
            benchmark_asset = self.snapshot.assets.get(benchmark_index)
            if benchmark_asset and benchmark_asset.index is not None:
                components["index"] = _score_index(benchmark_asset.index)
            else:
                notes.append(
                    f"Missing index metrics for benchmark {benchmark_index}."
                )

        news_score = None
        if self.official_news is not None:
            news_score = score_official_news_for_asset(
                self.official_news,
                symbol=symbol,
                asset_type=asset_type,
                benchmark_index=benchmark_index,
            )
        if news_score is not None:
            components["news"] = news_score

        total_score = self._blend(components)
        research_components = {
            name: score for name, score in components.items() if name != "quant"
        }
        return ResearchAssessment(
            symbol=symbol,
            total_score=total_score,
            research_score=(
                self._blend(research_components) if research_components else None
            ),
            component_scores=components,
            asset_type=asset_type,
            benchmark_index=benchmark_index,
            notes=tuple(notes),
        )

    def eligible_for_risk_on(self, assessment: ResearchAssessment) -> bool:
        threshold = self.snapshot.weights.minimum_total_score
        if assessment.research_score is not None:
            return assessment.research_score >= threshold
        return assessment.total_score >= threshold

    def _blend(self, components: dict[str, float]) -> float:
        raw_weights = {
            "quant": self.snapshot.weights.quant,
            "company": self.snapshot.weights.company,
            "index": self.snapshot.weights.index,
            "etf": self.snapshot.weights.etf,
            "news": self.snapshot.weights.news,
        }
        available = {
            component: raw_weights[component]
            for component in components
            if raw_weights.get(component, 0.0) > 0
        }
        if not available:
            return 0.5
        total_weight = sum(available.values())
        return sum(
            components[component] * weight for component, weight in available.items()
        ) / total_weight


def _score_company(metrics: CompanyMetrics) -> float:
    quality = _weighted_average(
        [
            (_high(metrics.return_on_equity, 0.08, 0.30), 0.35),
            (_high(metrics.gross_margin, 0.25, 0.70), 0.30),
            (_high(metrics.free_cash_flow_margin, 0.05, 0.25), 0.35),
        ]
    )
    growth = _weighted_average(
        [
            (_high(metrics.revenue_growth, 0.00, 0.20), 0.45),
            (_high(metrics.earnings_growth, 0.00, 0.25), 0.55),
        ]
    )
    balance_sheet = _weighted_average(
        [
            (_low(metrics.debt_to_equity, 0.20, 2.50), 0.35),
            (_low(metrics.net_debt_to_ebitda, 0.50, 4.00), 0.35),
            (_high(metrics.interest_coverage, 2.0, 15.0), 0.30),
        ]
    )
    valuation = _weighted_average(
        [
            (_low(metrics.pe_ratio, 10.0, 35.0), 0.50),
            (_low(metrics.ev_to_ebitda, 8.0, 25.0), 0.50),
        ]
    )
    return _weighted_average(
        [
            (quality, 0.30),
            (growth, 0.25),
            (balance_sheet, 0.25),
            (valuation, 0.20),
        ]
    )


def _score_etf(metrics: ETFMetrics) -> float:
    cost = _low(metrics.expense_ratio, 0.0003, 0.0100)
    liquidity = _weighted_average(
        [
            (_high(metrics.assets_under_management_billion, 2.0, 100.0), 0.45),
            (
                _high(metrics.average_daily_dollar_volume_billion, 0.05, 10.0),
                0.55,
            ),
        ]
    )
    tracking = _low(metrics.tracking_error, 0.0005, 0.0200)
    flow = _weighted_average(
        [
            (_high(metrics.flow_1m_percent, -0.05, 0.05), 0.45),
            (_high(metrics.flow_3m_percent, -0.10, 0.10), 0.55),
        ]
    )
    underlying = _weighted_average(
        [
            (metrics.portfolio_quality_score, 0.55),
            (metrics.portfolio_valuation_score, 0.45),
        ]
    )
    return _weighted_average(
        [
            (cost, 0.15),
            (liquidity, 0.25),
            (tracking, 0.20),
            (flow, 0.15),
            (underlying, 0.25),
        ]
    )


def _score_index(metrics: IndexMetrics) -> float:
    breadth = _high(metrics.breadth_percent_above_200dma, 0.30, 0.80)
    trend = _clamp01(metrics.trend_score)
    relative_strength = _clamp01(metrics.relative_strength_score)
    volatility = _low(metrics.volatility_percentile, 0.20, 0.90)
    credit = _low(metrics.credit_spread_percentile, 0.20, 0.90)
    curve = _high(metrics.yield_curve_slope_bps, -100.0, 150.0)
    return _weighted_average(
        [
            (_weighted_average([(breadth, 0.55), (trend, 0.45)]), 0.35),
            (relative_strength, 0.20),
            (volatility, 0.15),
            (credit, 0.15),
            (curve, 0.15),
        ]
    )


def _weighted_average(values: list[tuple[float | None, float]]) -> float:
    available = [(value, weight) for value, weight in values if value is not None]
    if not available:
        return 0.5
    total_weight = sum(weight for _, weight in available)
    return sum(value * weight for value, weight in available) / total_weight


def _high(value: float | None, low: float, high: float) -> float | None:
    if value is None:
        return None
    if high <= low:
        raise ValueError("high must be greater than low")
    return _clamp01((value - low) / (high - low))


def _low(value: float | None, low: float, high: float) -> float | None:
    scaled = _high(value, low, high)
    if scaled is None:
        return None
    return 1.0 - scaled


def _clamp01(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, value))


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _maybe_upper(value: str | None) -> str | None:
    if value is None:
        return None
    return value.upper()
