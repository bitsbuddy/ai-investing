from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class StrategyParameters:
    top_n: int = 3
    equity_count: int = 4
    defensive_count: int = 2
    min_risk_on_positions: int = 3
    trend_window: int = 200
    volatility_window: int = 20
    rebalance_frequency: str = "weekly"
    cash_buffer: float = 0.05
    target_equity_allocation: float = 0.45
    max_position_weight: float = 0.5
    max_equity_position_weight: float = 0.12
    max_sector_weight: float = 0.30
    max_benchmark_weight: float = 0.55
    momentum_windows: tuple[int, int, int] = (21, 63, 126)
    momentum_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)


@dataclass(frozen=True)
class ResearchWeights:
    quant: float = 0.35
    company: float = 0.30
    index: float = 0.20
    etf: float = 0.15
    news: float = 0.20
    minimum_total_score: float = 0.45


@dataclass(frozen=True)
class CompanyMetrics:
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    gross_margin: float | None = None
    free_cash_flow_margin: float | None = None
    return_on_equity: float | None = None
    debt_to_equity: float | None = None
    net_debt_to_ebitda: float | None = None
    interest_coverage: float | None = None
    pe_ratio: float | None = None
    ev_to_ebitda: float | None = None


@dataclass(frozen=True)
class ETFMetrics:
    expense_ratio: float | None = None
    assets_under_management_billion: float | None = None
    average_daily_dollar_volume_billion: float | None = None
    tracking_error: float | None = None
    flow_1m_percent: float | None = None
    flow_3m_percent: float | None = None
    portfolio_quality_score: float | None = None
    portfolio_valuation_score: float | None = None


@dataclass(frozen=True)
class IndexMetrics:
    breadth_percent_above_200dma: float | None = None
    trend_score: float | None = None
    relative_strength_score: float | None = None
    volatility_percentile: float | None = None
    credit_spread_percentile: float | None = None
    yield_curve_slope_bps: float | None = None


@dataclass(frozen=True)
class ResearchAsset:
    symbol: str
    asset_type: str
    benchmark_index: str | None = None
    sector: str | None = None
    company: CompanyMetrics | None = None
    etf: ETFMetrics | None = None
    index: IndexMetrics | None = None


@dataclass(frozen=True)
class ResearchSnapshot:
    as_of: date
    weights: ResearchWeights
    assets: dict[str, ResearchAsset]


@dataclass(frozen=True)
class OfficialNewsItem:
    source: str
    published_on: date
    title: str
    url: str
    impact_scores: dict[str, float] = field(default_factory=dict)
    symbols: tuple[str, ...] = ()
    summary: str | None = None
    analysis_summary: str | None = None
    analysis_confidence: float | None = None


@dataclass(frozen=True)
class OfficialNewsContext:
    as_of: date
    lookback_days: int
    risk_on_score: float | None = None
    duration_score: float | None = None
    cash_score: float | None = None
    gold_score: float | None = None
    company_scores: dict[str, float] = field(default_factory=dict)
    items: tuple[OfficialNewsItem, ...] = ()
    source_status: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchAssessment:
    symbol: str
    total_score: float
    research_score: float | None = None
    component_scores: dict[str, float] = field(default_factory=dict)
    asset_type: str | None = None
    benchmark_index: str | None = None
    sector: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlignedHistory:
    dates: list[date]
    closes: dict[str, list[float | None]]


@dataclass(frozen=True)
class Signal:
    as_of: date
    regime: str
    weights: dict[str, float]
    diagnostics: dict[str, float | str] = field(default_factory=dict)
    assessments: dict[str, ResearchAssessment] = field(default_factory=dict)
    official_news: OfficialNewsContext | None = None


@dataclass(frozen=True)
class BacktestResult:
    params: StrategyParameters
    total_return: float
    cagr: float
    annualized_volatility: float
    sharpe: float
    max_drawdown: float
    average_turnover: float
    equity_curve: list[float]
    score: float


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float


@dataclass(frozen=True)
class ClockSnapshot:
    is_open: bool
    timestamp: str


@dataclass(frozen=True)
class RebalanceAction:
    side: str
    symbol: str
    notional: float
    qty: float | None
    reason: str
    reference_price: float | None = None
