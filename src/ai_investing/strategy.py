from __future__ import annotations

import math
from collections import defaultdict
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
    asset_class: str


@dataclass(frozen=True)
class _RankedCandidate:
    symbol: str
    raw_score: float
    quant_score: float
    combined_score: float
    volatility: float
    asset_class: str


def align_history(price_history: dict[str, dict[date, float]]) -> AlignedHistory:
    if not price_history:
        raise ValueError("Price history is empty.")

    non_empty = {symbol: series for symbol, series in price_history.items() if series}
    if not non_empty:
        raise ValueError("Price history contains no bars.")

    ordered_dates = sorted(set().union(*(series.keys() for series in non_empty.values())))
    closes = {
        symbol: [series.get(current_date) for current_date in ordered_dates]
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
        equity_universe: tuple[str, ...] = (),
        research_overlay: ResearchOverlay | None = None,
    ) -> None:
        self.risk_on_universe = risk_on_universe
        self.equity_universe = equity_universe
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
        etf_candidates: list[_Candidate] = []
        equity_candidates: list[_Candidate] = []
        defensive_candidates: list[_Candidate] = []
        diagnostics: dict[str, float | str] = {}
        assessments: dict[str, ResearchAssessment] = {}

        for symbol in self.risk_on_universe:
            closes = history.closes[symbol]
            price = closes[index]
            score = self._momentum_score(closes, index)
            trend = self._sma(closes, index, self.params.trend_window)
            volatility = self._volatility(closes, index)
            if (
                price is None
                or score is None
                or trend is None
                or volatility is None
            ):
                continue
            diagnostics[f"{symbol}_raw_score"] = round(score, 6)
            if price > trend and score > 0:
                etf_candidates.append(
                    _Candidate(
                        symbol=symbol,
                        raw_score=score,
                        volatility=volatility,
                        asset_class="etf",
                    )
                )

        for symbol in self.equity_universe:
            closes = history.closes.get(symbol)
            if closes is None:
                continue
            price = closes[index]
            score = self._momentum_score(closes, index)
            trend = self._sma(closes, index, self.params.trend_window)
            volatility = self._volatility(closes, index)
            if (
                price is None
                or score is None
                or trend is None
                or volatility is None
            ):
                continue
            diagnostics[f"{symbol}_raw_score"] = round(score, 6)
            if price > trend and score > 0:
                equity_candidates.append(
                    _Candidate(
                        symbol=symbol,
                        raw_score=score,
                        volatility=volatility,
                        asset_class="equity",
                    )
                )

        for symbol in self.defensive_universe:
            closes = history.closes[symbol]
            score = self._momentum_score(closes, index)
            volatility = self._volatility(closes, index)
            if score is None or volatility is None:
                continue
            defensive_candidates.append(
                _Candidate(
                    symbol=symbol,
                    raw_score=score,
                    volatility=volatility,
                    asset_class="defensive",
                )
            )

        ranked_etfs = self._rank_candidates(etf_candidates, assessments)
        ranked_equities = self._rank_candidates(equity_candidates, assessments)
        ranked_defensive = self._rank_candidates(defensive_candidates, assessments)
        eligible_etfs = [
            candidate
            for candidate in ranked_etfs
            if self._candidate_is_allowed(candidate.symbol, assessments)
        ]
        eligible_equities = [
            candidate
            for candidate in ranked_equities
            if self._candidate_is_allowed(candidate.symbol, assessments)
        ]
        eligible_defensive = [
            candidate
            for candidate in ranked_defensive
            if self._candidate_is_allowed(candidate.symbol, assessments)
        ]

        selected_etfs = _select_diversified_candidates(
            eligible_etfs,
            count=self.params.top_n,
            max_per_group=_group_position_limit(
                self.params.max_benchmark_weight, self.params.max_position_weight
            ),
            group_key_fn=lambda symbol: _assessment_group_key(
                assessments, symbol, "benchmark"
            ),
        )
        selected_equities = _select_diversified_candidates(
            eligible_equities,
            count=self.params.equity_count,
            max_per_group=_group_position_limit(
                self.params.max_sector_weight,
                self.params.max_equity_position_weight,
            ),
            group_key_fn=lambda symbol: _assessment_group_key(
                assessments, symbol, "sector"
            ),
        )
        selected_risk_on = selected_etfs + selected_equities
        required_risk_on_positions = min(
            self.params.min_risk_on_positions,
            max(
                1,
                self.params.top_n
                + (self.params.equity_count if self.equity_universe else 0),
            ),
        )

        if len(selected_risk_on) >= required_risk_on_positions:
            selected = selected_risk_on
            regime = "risk_on"
            raw_weights = _build_risk_on_raw_weights(
                selected_etfs=selected_etfs,
                selected_equities=selected_equities,
                target_equity_allocation=self.params.target_equity_allocation,
            )
            max_position_by_symbol = {
                candidate.symbol: (
                    self.params.max_equity_position_weight
                    if candidate.asset_class == "equity"
                    else self.params.max_position_weight
                )
                for candidate in selected
            }
            group_for_symbol, max_group_weight = _build_group_caps(
                selected,
                assessments=assessments,
                max_sector_weight=self.params.max_sector_weight,
                max_benchmark_weight=self.params.max_benchmark_weight,
            )
        else:
            selected = _select_diversified_candidates(
                eligible_defensive,
                count=self.params.defensive_count,
                max_per_group=_group_position_limit(
                    self.params.max_benchmark_weight, self.params.max_position_weight
                ),
                group_key_fn=lambda symbol: _assessment_group_key(
                    assessments, symbol, "benchmark"
                ),
            )
            regime = "risk_off"
            selected_etfs = ()
            selected_equities = ()
            raw_weights = _build_generic_raw_weights(selected)
            max_position_by_symbol = {
                candidate.symbol: self.params.max_position_weight for candidate in selected
            }
            group_for_symbol, max_group_weight = _build_group_caps(
                selected,
                assessments=assessments,
                max_sector_weight=self.params.max_sector_weight,
                max_benchmark_weight=self.params.max_benchmark_weight,
            )

        weights = _cap_and_scale_weights(
            raw_weights,
            investable_weight=1.0 - self.params.cash_buffer,
            max_position_by_symbol=max_position_by_symbol,
            group_for_symbol=group_for_symbol,
            max_group_weight=max_group_weight,
        )
        for symbol, assessment in assessments.items():
            diagnostics[f"{symbol}_combined_score"] = round(assessment.total_score, 6)
            for component, score in assessment.component_scores.items():
                diagnostics[f"{symbol}_{component}_score"] = round(score, 6)
        diagnostics["selected_count"] = float(len(weights))
        diagnostics["selected_etf_count"] = float(len(selected_etfs))
        diagnostics["selected_equity_count"] = float(len(selected_equities))
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
        for top_n in (3, 4):
            for equity_count in (4, 6):
                for defensive_count in (1, 2):
                    for trend_window in (150, 200):
                        for target_equity_allocation in (0.35, 0.50):
                            for rebalance_frequency in ("weekly", "monthly"):
                                candidates.append(
                                    replace(
                                        self.params,
                                        top_n=top_n,
                                        equity_count=equity_count,
                                        defensive_count=defensive_count,
                                        trend_window=trend_window,
                                        target_equity_allocation=target_equity_allocation,
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
                    asset_class=candidate.asset_class,
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

    def _momentum_score(self, closes: list[float | None], index: int) -> float | None:
        score = 0.0
        for window, weight in zip(
            self.params.momentum_windows, self.params.momentum_weights, strict=True
        ):
            current_price = closes[index]
            previous_price = closes[index - window]
            if current_price is None or previous_price is None:
                return None
            score += ((current_price / previous_price) - 1.0) * weight
        return score

    @staticmethod
    def _sma(closes: list[float | None], index: int, window: int) -> float | None:
        window_values = closes[index - window + 1 : index + 1]
        if any(value is None for value in window_values):
            return None
        return mean(value for value in window_values if value is not None)

    def _volatility(self, closes: list[float | None], index: int) -> float | None:
        returns = []
        start = index - self.params.volatility_window + 1
        for current_index in range(start, index + 1):
            prior = closes[current_index - 1]
            current = closes[current_index]
            if prior is None or current is None:
                return None
            returns.append((current / prior) - 1.0)
        return pstdev(returns) * math.sqrt(252)


def _cap_and_scale_weights(
    raw_weights: dict[str, float],
    *,
    investable_weight: float,
    max_position_by_symbol: dict[str, float],
    group_for_symbol: dict[str, str],
    max_group_weight: dict[str, float],
) -> dict[str, float]:
    if not raw_weights:
        return {}
    if investable_weight <= 0:
        return {}

    total_raw = sum(raw_weights.values())
    if total_raw <= 0:
        return {}

    weights = {
        symbol: investable_weight * (value / total_raw)
        for symbol, value in raw_weights.items()
    }
    for _ in range(12):
        excess = 0.0
        for symbol, current_weight in list(weights.items()):
            position_cap = max_position_by_symbol.get(symbol, investable_weight)
            if current_weight > position_cap:
                excess += current_weight - position_cap
                weights[symbol] = position_cap

        for group, cap in max_group_weight.items():
            members = [
                symbol
                for symbol, symbol_group in group_for_symbol.items()
                if symbol_group == group and symbol in weights
            ]
            group_total = sum(weights[symbol] for symbol in members)
            if group_total <= cap or group_total <= 0:
                continue
            scale = cap / group_total
            for symbol in members:
                reduced = weights[symbol] * scale
                excess += weights[symbol] - reduced
                weights[symbol] = reduced

        if excess <= 1e-9:
            break

        headroom = _symbol_headroom(
            weights=weights,
            max_position_by_symbol=max_position_by_symbol,
            group_for_symbol=group_for_symbol,
            max_group_weight=max_group_weight,
        )
        if not headroom:
            break
        allocated = 0.0
        total_headroom_raw = sum(raw_weights[symbol] for symbol in headroom)
        if total_headroom_raw <= 0:
            break
        for symbol, symbol_headroom in headroom.items():
            proposed = excess * (raw_weights[symbol] / total_headroom_raw)
            addition = min(proposed, symbol_headroom)
            weights[symbol] += addition
            allocated += addition
        if allocated <= 1e-9:
            break

    return {symbol: weight for symbol, weight in weights.items() if weight > 0}


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


def _build_generic_raw_weights(
    candidates: list[_RankedCandidate],
) -> dict[str, float]:
    return {
        candidate.symbol: max(candidate.combined_score, 0.05)
        / max(candidate.volatility, 1e-6)
        for candidate in candidates
    }


def _build_risk_on_raw_weights(
    *,
    selected_etfs: list[_RankedCandidate],
    selected_equities: list[_RankedCandidate],
    target_equity_allocation: float,
) -> dict[str, float]:
    raw_weights: dict[str, float] = {}
    sleeve_targets = {}
    if selected_etfs:
        sleeve_targets["etf"] = max(0.0, 1.0 - target_equity_allocation)
    if selected_equities:
        sleeve_targets["equity"] = max(0.0, target_equity_allocation)
    if not sleeve_targets:
        return {}

    total_target = sum(sleeve_targets.values())
    normalized_targets = {
        sleeve: target / total_target for sleeve, target in sleeve_targets.items()
    }

    for sleeve_name, candidates in (
        ("etf", selected_etfs),
        ("equity", selected_equities),
    ):
        if not candidates:
            continue
        sleeve_raw = _build_generic_raw_weights(candidates)
        sleeve_total = sum(sleeve_raw.values())
        if sleeve_total <= 0:
            continue
        multiplier = normalized_targets[sleeve_name] / sleeve_total
        for symbol, value in sleeve_raw.items():
            raw_weights[symbol] = value * multiplier
    return raw_weights


def _assessment_group_key(
    assessments: dict[str, ResearchAssessment],
    symbol: str,
    group_kind: str,
) -> str | None:
    assessment = assessments.get(symbol)
    if assessment is None:
        return None
    if group_kind == "sector":
        return assessment.sector
    if group_kind == "benchmark":
        return assessment.benchmark_index
    raise ValueError(f"Unsupported group kind: {group_kind}")


def _group_position_limit(max_group_weight: float, max_position_weight: float) -> int:
    if max_position_weight <= 0:
        return 1
    return max(1, int(math.floor(max_group_weight / max_position_weight)))


def _select_diversified_candidates(
    candidates: list[_RankedCandidate],
    *,
    count: int,
    max_per_group: int,
    group_key_fn,
) -> list[_RankedCandidate]:
    if count <= 0:
        return []
    selected: list[_RankedCandidate] = []
    group_counts: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        group_key = group_key_fn(candidate.symbol)
        if group_key and group_counts[group_key] >= max_per_group:
            continue
        selected.append(candidate)
        if group_key:
            group_counts[group_key] += 1
        if len(selected) >= count:
            break
    return selected


def _build_group_caps(
    selected: list[_RankedCandidate],
    *,
    assessments: dict[str, ResearchAssessment],
    max_sector_weight: float,
    max_benchmark_weight: float,
) -> tuple[dict[str, str], dict[str, float]]:
    group_for_symbol: dict[str, str] = {}
    max_group_weight: dict[str, float] = {}
    for candidate in selected:
        assessment = assessments.get(candidate.symbol)
        if candidate.asset_class == "equity" and assessment and assessment.sector:
            group_key = f"sector:{assessment.sector}"
            group_for_symbol[candidate.symbol] = group_key
            max_group_weight[group_key] = max_sector_weight
        elif candidate.asset_class in {"etf", "defensive"} and assessment and assessment.benchmark_index:
            group_key = f"benchmark:{assessment.benchmark_index}"
            group_for_symbol[candidate.symbol] = group_key
            max_group_weight[group_key] = max_benchmark_weight
    return group_for_symbol, max_group_weight


def _symbol_headroom(
    *,
    weights: dict[str, float],
    max_position_by_symbol: dict[str, float],
    group_for_symbol: dict[str, str],
    max_group_weight: dict[str, float],
) -> dict[str, float]:
    group_totals: dict[str, float] = defaultdict(float)
    for symbol, group in group_for_symbol.items():
        group_totals[group] += weights.get(symbol, 0.0)

    headroom: dict[str, float] = {}
    for symbol, current_weight in weights.items():
        position_headroom = max(
            0.0, max_position_by_symbol.get(symbol, 1.0) - current_weight
        )
        group_key = group_for_symbol.get(symbol)
        if group_key is None:
            effective_headroom = position_headroom
        else:
            group_headroom = max(0.0, max_group_weight[group_key] - group_totals[group_key])
            effective_headroom = min(position_headroom, group_headroom)
        if effective_headroom > 1e-9:
            headroom[symbol] = effective_headroom
    return headroom
