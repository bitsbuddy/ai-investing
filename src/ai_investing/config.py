from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    values = [value.strip().upper() for value in raw.split(",")]
    return [value for value in values if value]


@dataclass(frozen=True)
class BrokerConfig:
    api_key: str
    secret_key: str
    paper: bool
    trading_base_url: str
    market_data_base_url: str


@dataclass(frozen=True)
class RuntimeConfig:
    enable_live: bool
    enable_official_news: bool
    state_path: Path
    default_feed: str
    risk_on_universe: tuple[str, ...]
    defensive_universe: tuple[str, ...]
    research_snapshot_path: Path | None
    research_max_age_days: int
    official_news_lookback_days: int
    require_official_news: bool
    sec_user_agent: str
    max_price_drift_pct: float


def load_broker_config() -> BrokerConfig:
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper = _bool_env("ALPACA_PAPER", True)
    trading_base_url = (
        "https://paper-api.alpaca.markets"
        if paper
        else "https://api.alpaca.markets"
    )
    market_data_base_url = "https://data.alpaca.markets"
    return BrokerConfig(
        api_key=api_key,
        secret_key=secret_key,
        paper=paper,
        trading_base_url=trading_base_url,
        market_data_base_url=market_data_base_url,
    )


def load_runtime_config() -> RuntimeConfig:
    research_snapshot_raw = os.getenv("AI_INVESTING_RESEARCH_SNAPSHOT_PATH", "").strip()
    return RuntimeConfig(
        enable_live=_bool_env("AI_INVESTING_ENABLE_LIVE", False),
        enable_official_news=_bool_env("AI_INVESTING_ENABLE_OFFICIAL_NEWS", True),
        state_path=Path(
            os.getenv("AI_INVESTING_STATE_PATH", ".ai_investing_state.json")
        ),
        default_feed=os.getenv("AI_INVESTING_DEFAULT_FEED", "iex").strip() or "iex",
        risk_on_universe=tuple(
            _csv_env("AI_INVESTING_RISK_ON", ["SPY", "QQQ", "IWM", "EFA", "EEM"])
        ),
        defensive_universe=tuple(
            _csv_env("AI_INVESTING_DEFENSIVE", ["TLT", "IEF", "GLD", "SHY"])
        ),
        research_snapshot_path=(
            Path(research_snapshot_raw) if research_snapshot_raw else None
        ),
        research_max_age_days=_int_env("AI_INVESTING_RESEARCH_MAX_AGE_DAYS", 45),
        official_news_lookback_days=_int_env(
            "AI_INVESTING_OFFICIAL_NEWS_LOOKBACK_DAYS", 14
        ),
        require_official_news=_bool_env("AI_INVESTING_REQUIRE_OFFICIAL_NEWS", False),
        sec_user_agent=(
            os.getenv(
                "AI_INVESTING_SEC_USER_AGENT",
                "AI-Investing research@example.com",
            ).strip()
            or "AI-Investing research@example.com"
        ),
        max_price_drift_pct=_float_env("AI_INVESTING_MAX_PRICE_DRIFT_PCT", 0.02),
    )


def require_broker_credentials(config: BrokerConfig) -> None:
    if not config.api_key or not config.secret_key:
        raise ValueError(
            "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
        )
