from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class StrategyParameters:
    top_n: int = 3
    defensive_count: int = 2
    trend_window: int = 200
    volatility_window: int = 20
    rebalance_frequency: str = "weekly"
    cash_buffer: float = 0.05
    max_position_weight: float = 0.5
    momentum_windows: tuple[int, int, int] = (21, 63, 126)
    momentum_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)


@dataclass(frozen=True)
class AlignedHistory:
    dates: list[date]
    closes: dict[str, list[float]]


@dataclass(frozen=True)
class Signal:
    as_of: date
    regime: str
    weights: dict[str, float]
    diagnostics: dict[str, float | str] = field(default_factory=dict)


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
