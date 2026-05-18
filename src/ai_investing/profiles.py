from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from .models import StrategyParameters


_PROFILE_PRESETS: dict[str, StrategyParameters] = {
    "conservative": StrategyParameters(
        top_n=2,
        defensive_count=2,
        trend_window=200,
        rebalance_frequency="monthly",
        cash_buffer=0.15,
        max_position_weight=0.30,
        momentum_weights=(0.4, 0.35, 0.25),
    ),
    "balanced": StrategyParameters(),
    "aggressive": StrategyParameters(
        top_n=3,
        defensive_count=1,
        trend_window=150,
        rebalance_frequency="weekly",
        cash_buffer=0.02,
        max_position_weight=0.65,
        momentum_windows=(21, 42, 84),
        momentum_weights=(0.6, 0.3, 0.1),
    ),
}


@dataclass(frozen=True)
class ProfileMatrixEntry:
    name: str
    env_file: Path


def strategy_parameters_for_risk_profile(risk_profile: str) -> StrategyParameters:
    normalized = (risk_profile or "balanced").strip().lower()
    if normalized not in _PROFILE_PRESETS:
        raise ValueError(
            f"Unsupported AI_INVESTING_RISK_PROFILE={risk_profile!r}. "
            "Expected one of: conservative, balanced, aggressive."
        )
    return _PROFILE_PRESETS[normalized]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not raw_value:
            values[key] = ""
            continue
        if raw_value[0] in {'"', "'"} and raw_value[-1] == raw_value[0]:
            values[key] = shlex.split(raw_value)[0]
        else:
            values[key] = raw_value
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={_env_quote(value)}" for key, value in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def default_profile_matrix_entries(base_dir: Path) -> list[ProfileMatrixEntry]:
    return [
        ProfileMatrixEntry(name=name, env_file=base_dir / f"{name}.paper.env")
        for name in ("conservative", "balanced", "aggressive")
    ]


def write_profile_matrix(path: Path, entries: list[ProfileMatrixEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profiles": [
            {"name": entry.name, "env_file": str(entry.env_file)}
            for entry in entries
        ]
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_profile_matrix(path: Path) -> list[ProfileMatrixEntry]:
    payload = json.loads(path.read_text())
    entries: list[ProfileMatrixEntry] = []
    for item in payload.get("profiles", []):
        entries.append(
            ProfileMatrixEntry(
                name=str(item["name"]).strip().lower(),
                env_file=Path(str(item["env_file"])).expanduser(),
            )
        )
    if not entries:
        raise ValueError(f"{path} does not define any profiles.")
    return entries


def _env_quote(value: str) -> str:
    if value == "":
        return ""
    if any(character.isspace() for character in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value
