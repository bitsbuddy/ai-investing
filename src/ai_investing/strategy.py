from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from statistics import mean, pstdev

from .models import AlignedHistory, ResearchAssessment, Signal, StrategyParameters
from .research import ResearchOverlay


@dataclass(frozen=True)
class _Candidate:
    symbol: str
    raw_score: float
    volatility: float


@dataclass(frozen=True)
class _RankedCandidate:
    symbol: str
    raw_score: float
    quant_score: float
    combined_score: float
    volatility: float


def align_history(price_history: dict[str, dict[date, float]]) -> AlignedHistory:
    if not price_history:
        raise ValueError("Price history is empty.")

    non_empty = {symbol: series for symbol, series in price_history.items() if series}
    if not non_empty:
        raise ValueError("Price history contains no bars.")

    common_dates = set.intersection(*(set(series.keys()) for series in non_empty.values()))
    if not common_dates:
        raise ValueError("Symbols do not share a common trading calendar.")

    ordered_dates = sorted(common_dates)
    closes = {
        symbol: [series[current_date] for current_date in ordered_dates]
        for symbol, series in non_empty.items()
    }
    return AlignedHistory(dates=ordered_dates, closes=closes)


class ETFMomentumStrategy:
    def __init__(
        self,
        *,
        risk_on_universe: tuple[str, ...],
        defensive_universe: tuple[str, ...],
        params: StrategyParameters,
        research_overlay: ResearchOverlay | None = None,
    ) -> None:
        self.risk_on_universe = risk_on_universe
        self.defensive_universe = defensive_universe
        self.params = params
        self.research_overlay = research_overlay

    @property
    def warmup_bars(self) -> int:
        return max(
            self.params.trend_window,
            self.params.volatility_window + 1,
            max(self.params.momentum_windows) + 1,
        )

    def signal_for_index(self, history: AlignedHistory, index: int) -> Signal:
        if index < self.warmup_bars:
            raise ValueError(
                f"Need at least {self.warmup_bars} bars before computing a signal."
            )

        current_date = history.dates[index]
        risk_on_candidates: list[_Candidate] = []
        defensive_candidates: list[_Candidate] = []
        diagnostics: dict[str, float | str] = {}
        assessments: dict[str, ResearchAssessment] = {}

        for symbol in self.risk_on_universe:
            price = history.closes[symbol][index]
            trend = self._sma(history.closes[symbol], index, self.params.trend_window)
            score = self._momentum_score(history.closes[symbol], index)
            volatility = self._volatility(history.closes[symbol], index)
            diagnostics[f"{symbol}_raw_score"] = round(score, 6)
            if price > trend and score > 0:
                risk_on_candidates.append(
                    _Candidate(symbol=symbol, raw_score=score, volatility=volatility)
                )

        for symbol in self.defensive_universe:
            score = self._momentum_score(history.closes[symbol], index)
            volatility = self._volatility(history.closes[symbol], index)
            defensive_candidates.append(
                _Candidate(symbol=symbol, raw_score=score, volatility=volatility)
            )

        ranked_risk_on = self._rank_candidates(risk_on_candidates, assessments)
        ranked_defensive = self._rank_candidates(defensive_candidates, assessments)
        eligible_risk_on = [
            candidate
            for candidate in ranked_risk_on
            if self._candidate_is_allowed(candidate.symbol, assessments)
        ]
        eligible_defensive = [
            candidate
            for candidate in ranked_defensive
            if self._candidate_is_allowed(candidate.symbol, assessments)
        ]

        if len(eligible_risk_on) >= self.params.top_n:
            selected = eligible_risk_on[: self.params.top_n]
            regime = "risk_on"
        else:
            selected = eligible_defensive[: self.params.defensive_count]
            regime = "risk_off"

        raw_weights = {
            candidate.symbol: max(candidate.combined_score, 0.05)
            / max(candidate.volatility, 1e-6)
            for candidate in selected
        }
        weights = _cap_and_scale_weights(
            raw_weights,
            investable_weight=1.0 - self.params.cash_buffer,
            max_position_weight=self.params.max_position_weight,
        )
        for symbol, assessment in assessments.items():
            diagnostics[f"{symbol}_combined_score"] = round(assessment.total_score, 6)
            for component, score in assessment.component_scores.items():
                diagnostics[f"{symbol}_{component}_score"] = round(score, 6)
        diagnostics["selected_count"] = float(len(weights))
        diagnostics["cash_buffer"] = round(1.0 - sum(weights.values()), 6)
        diagnostics["rebalance_frequency"] = self.params.rebalance_frequency
        return Signal(
            as_of=current_date,
            regime=regime,
            weights=weights,
            diagnostics=diagnostics,
            assessments=assessments,
        )

    def next_rebalance_index(
        self, history: AlignedHistory, current_index: int, last_rebalance_index: int | None
    ) -> bool:
        if last_rebalance_index is None:
            return True
        if current_index <= last_rebalance_index:
            return False

        current_date = history.dates[current_index]
        previous_rebalance = history.dates[last_rebalance_index]
        frequency = self.params.rebalance_frequency
        if frequency == "weekly":
            return current_date.isocalendar()[:2] != previous_rebalance.isocalendar()[:2]
        if frequency == "monthly":
            return (
                current_date.year != previous_rebalance.year
                or current_date.month != previous_rebalance.month
            )
        raise ValueError(f"Unsupported rebalance frequency: {frequency}")

    def parameter_grid(self) -> list[StrategyParameters]:
        candidates: list[StrategyParameters] = []
        for top_n in (2, 3):
            for defensive_count in (1, 2):
                for trend_window in (150, 200):
                    for rebalance_frequency in ("weekly", "monthly"):
                        candidates.append(
                            replace(
                                self.params,
                                top_n=top_n,
                                defensive_count=defensive_count,
                                trend_window=trend_window,
                                rebalance_frequency=rebalance_frequency,
                            )
                        )
        return candidates

    def _rank_candidates(
        self,
        candidates: list[_Candidate],
        assessments: dict[str, ResearchAssessment],
    ) -> list[_RankedCandidate]:
        if not candidates:
            return []

        quant_scores = _normalize_candidate_scores(candidates)
        ranked: list[_RankedCandidate] = []
        for candidate in candidates:
            quant_score = quant_scores[candidate.symbol]
            if self.research_overlay is None:
                combined_score = quant_score
            else:
                assessment = self.research_overlay.assess_symbol(
                    candidate.symbol, quant_score
                )
                assessments[candidate.symbol] = assessment
                combined_score = assessment.total_score
            ranked.append(
                _RankedCandidate(
                    symbol=candidate.symbol,
                    raw_score=candidate.raw_score,
                    quant_score=quant_score,
                    combined_score=combined_score,
                    volatility=candidate.volatility,
                )
            )
        return sorted(ranked, key=lambda item: item.combined_score, reverse=True)

    def _candidate_is_allowed(
        self, symbol: str, assessments: dict[str, ResearchAssessment]
    ) -> bool:
        if self.research_overlay is None:
            return True
        assessment = assessments.get(symbol)
        if assessment is None:
            return True
        return self.research_overlay.eligible_for_risk_on(assessment)

    def _momentum_score(self, closes: list[float], index: int) -> float:
        score = 0.0
        for window, weight in zip(
            self.params.momentum_windows, self.params.momentum_weights, strict=True
        ):
            current_price = closes[index]
            previous_price = closes[index - window]
            score += ((current_price / previous_price) - 1.0) * weight
        return score

    @staticmethod
    def _sma(closes: list[float], index: int, window: int) -> float:
        return mean(closes[index - window + 1 : index + 1])

    def _volatility(self, closes: list[float], index: int) -> float:
        returns = []
        start = index - self.params.volatility_window + 1
        for current_index in range(start, index + 1):
            prior = closes[current_index - 1]
            current = closes[current_index]
            returns.append((current / prior) - 1.0)
        return pstdev(returns) * math.sqrt(252)


def _cap_and_scale_weights(
    raw_weights: dict[str, float],
    *,
    investable_weight: float,
    max_position_weight: float,
) -> dict[str, float]:
    if not raw_weights:
        return {}

    remaining = dict(raw_weights)
    results: dict[str, float] = {}
    remaining_weight = investable_weight

    while remaining and remaining_weight > 0:
        total = sum(remaining.values())
        capped_symbol: str | None = None

        for symbol, value in remaining.items():
            proposed = remaining_weight * (value / total)
            if proposed > max_position_weight:
                results[symbol] = max_position_weight
                remaining_weight -= max_position_weight
                capped_symbol = symbol
                break

        if capped_symbol is not None:
            del remaining[capped_symbol]
            continue

        for symbol, value in remaining.items():
            results[symbol] = remaining_weight * (value / total)
        break

    return {symbol: weight for symbol, weight in results.items() if weight > 0}


def _normalize_candidate_scores(candidates: list[_Candidate]) -> dict[str, float]:
    if len(candidates) == 1:
        return {candidates[0].symbol: 1.0}
    raw_values = [candidate.raw_score for candidate in candidates]
    low = min(raw_values)
    high = max(raw_values)
    if math.isclose(low, high):
        return {candidate.symbol: 0.5 for candidate in candidates}
    return {
        candidate.symbol: (candidate.raw_score - low) / (high - low)
        for candidate in candidates
    }
