from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _raw_env(name: str, env: Mapping[str, str] | None = None) -> str | None:
    if env is not None:
        return env.get(name)
    return os.getenv(name)


def _float_env(name: str, default: float, env: Mapping[str, str] | None = None) -> float:
    raw = _raw_env(name, env)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


def _optional_float_env(name: str, env: Mapping[str, str] | None = None) -> float | None:
    raw = _raw_env(name, env)
    if raw is None or raw.strip() == "":
        return None
    return float(raw.strip())


def _int_env(name: str, default: int, env: Mapping[str, str] | None = None) -> int:
    raw = _raw_env(name, env)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def _bool_env(name: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    raw = _raw_env(name, env)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(
    name: str, default: list[str], env: Mapping[str, str] | None = None
) -> list[str]:
    raw = _raw_env(name, env)
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
    profile_name: str
    risk_profile: str
    enable_live: bool
    enable_official_news: bool
    enable_llm_news: bool
    state_path: Path
    performance_baseline: float | None
    default_feed: str
    risk_on_universe: tuple[str, ...]
    defensive_universe: tuple[str, ...]
    research_snapshot_path: Path | None
    research_max_age_days: int
    official_news_lookback_days: int
    require_official_news: bool
    require_llm_news: bool
    sec_user_agent: str
    llm_news_api_key: str
    llm_news_model: str
    llm_news_base_url: str
    llm_news_max_items: int
    llm_news_max_chars: int
    max_price_drift_pct: float


def load_broker_config(env: Mapping[str, str] | None = None) -> BrokerConfig:
    api_key = (_raw_env("ALPACA_API_KEY", env) or "").strip()
    secret_key = (_raw_env("ALPACA_SECRET_KEY", env) or "").strip()
    paper = _bool_env("ALPACA_PAPER", True, env)
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


def load_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    research_snapshot_raw = (
        _raw_env("AI_INVESTING_RESEARCH_SNAPSHOT_PATH", env) or ""
    ).strip()
    llm_news_api_key = (
        (_raw_env("AI_INVESTING_OPENAI_API_KEY", env) or _raw_env("OPENAI_API_KEY", env) or "").strip()
    )
    risk_profile = ((_raw_env("AI_INVESTING_RISK_PROFILE", env) or "balanced").strip().lower() or "balanced")
    profile_name = (
        (_raw_env("AI_INVESTING_PROFILE_NAME", env) or "").strip()
        or risk_profile.replace("-", " ").title()
    )
    return RuntimeConfig(
        profile_name=profile_name,
        risk_profile=risk_profile,
        enable_live=_bool_env("AI_INVESTING_ENABLE_LIVE", False, env),
        enable_official_news=_bool_env("AI_INVESTING_ENABLE_OFFICIAL_NEWS", True, env),
        enable_llm_news=_bool_env("AI_INVESTING_ENABLE_LLM_NEWS", False, env),
        state_path=Path(
            (_raw_env("AI_INVESTING_STATE_PATH", env) or ".ai_investing_state.json")
        ),
        performance_baseline=_optional_float_env("AI_INVESTING_PERFORMANCE_BASELINE", env),
        default_feed=((_raw_env("AI_INVESTING_DEFAULT_FEED", env) or "iex").strip() or "iex"),
        risk_on_universe=tuple(
            _csv_env(
                "AI_INVESTING_RISK_ON",
                ["SPY", "QQQ", "IWM", "EFA", "EEM"],
                env,
            )
        ),
        defensive_universe=tuple(
            _csv_env(
                "AI_INVESTING_DEFENSIVE",
                ["TLT", "IEF", "GLD", "SHY"],
                env,
            )
        ),
        research_snapshot_path=(
            Path(research_snapshot_raw) if research_snapshot_raw else None
        ),
        research_max_age_days=_int_env("AI_INVESTING_RESEARCH_MAX_AGE_DAYS", 45, env),
        official_news_lookback_days=_int_env(
            "AI_INVESTING_OFFICIAL_NEWS_LOOKBACK_DAYS", 14, env
        ),
        require_official_news=_bool_env("AI_INVESTING_REQUIRE_OFFICIAL_NEWS", False, env),
        require_llm_news=_bool_env("AI_INVESTING_REQUIRE_LLM_NEWS", False, env),
        sec_user_agent=(
            (_raw_env(
                "AI_INVESTING_SEC_USER_AGENT",
                env,
            ) or "AI-Investing research@example.com").strip()
            or "AI-Investing research@example.com"
        ),
        llm_news_api_key=llm_news_api_key,
        llm_news_model=(
            ((_raw_env("AI_INVESTING_LLM_NEWS_MODEL", env) or "gpt-5-mini").strip())
            or "gpt-5-mini"
        ),
        llm_news_base_url=(
            ((_raw_env("AI_INVESTING_OPENAI_BASE_URL", env) or "https://api.openai.com/v1").strip())
            or "https://api.openai.com/v1"
        ),
        llm_news_max_items=_int_env("AI_INVESTING_LLM_NEWS_MAX_ITEMS", 8, env),
        llm_news_max_chars=_int_env("AI_INVESTING_LLM_NEWS_MAX_CHARS", 6000, env),
        max_price_drift_pct=_float_env("AI_INVESTING_MAX_PRICE_DRIFT_PCT", 0.02, env),
    )


def require_broker_credentials(config: BrokerConfig) -> None:
    if not config.api_key or not config.secret_key:
        raise ValueError(
            "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
        )
