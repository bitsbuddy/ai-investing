from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .alpaca import AlpacaClient
from .models import RebalanceAction, Signal


@dataclass
class RuntimeState:
    high_water_mark: float = 0.0
    last_rebalance_date: str | None = None


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    payload = json.loads(path.read_text())
    return RuntimeState(
        high_water_mark=float(payload.get("high_water_mark", 0.0)),
        last_rebalance_date=payload.get("last_rebalance_date"),
    )


def save_state(path: Path, state: RuntimeState) -> None:
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")


def plan_rebalance(
    *,
    equity: float,
    signal: Signal,
    current_positions: dict[str, float],
    current_market_values: dict[str, float],
    latest_prices: dict[str, float],
    min_trade_notional: float = 25.0,
) -> list[RebalanceAction]:
    actions: list[RebalanceAction] = []
    target_values = {symbol: equity * weight for symbol, weight in signal.weights.items()}
    current_values = dict(current_market_values)
    for symbol in target_values:
        current_values.setdefault(
            symbol, current_positions.get(symbol, 0.0) * latest_prices[symbol]
        )

    all_symbols = set(current_positions) | set(target_values)
    for symbol in sorted(all_symbols):
        target_value = target_values.get(symbol, 0.0)
        current_value = current_values.get(symbol, 0.0)
        delta = target_value - current_value
        if abs(delta) < min_trade_notional:
            continue
        price = latest_prices.get(symbol)
        if delta < 0:
            qty = current_positions.get(symbol, 0.0)
            if price is not None and price > 0:
                qty = min(abs(delta) / price, qty)
            if qty <= 0:
                continue
            actions.append(
                RebalanceAction(
                    side="sell",
                    symbol=symbol,
                    notional=abs(delta),
                    qty=qty,
                    reason="reduce_or_exit",
                )
            )
        else:
            if price is None:
                continue
            actions.append(
                RebalanceAction(
                    side="buy",
                    symbol=symbol,
                    notional=delta,
                    qty=None,
                    reason="increase_or_enter",
                )
            )

    return sorted(actions, key=lambda action: 0 if action.side == "sell" else 1)


def execute_rebalance(
    *,
    client: AlpacaClient,
    signal: Signal,
    state_path: Path,
    latest_prices: dict[str, float],
    allow_live: bool,
    is_paper: bool,
    submit: bool,
    force: bool,
    max_drawdown: float = 0.20,
) -> tuple[list[RebalanceAction], list[dict[str, object]], RuntimeState]:
    account = client.get_account()
    positions = client.get_positions()
    state = load_state(state_path)
    state.high_water_mark = max(state.high_water_mark, account.equity)

    if (
        state.high_water_mark > 0
        and account.equity < state.high_water_mark * (1.0 - max_drawdown)
    ):
        raise RuntimeError(
            "Drawdown kill switch triggered. Review the account before resuming trading."
        )

    signal_date = signal.as_of.isoformat()
    if not force and state.last_rebalance_date == signal_date:
        return [], [], state

    current_positions = {position.symbol: position.qty for position in positions}
    current_market_values = {
        position.symbol: position.market_value for position in positions
    }
    actions = plan_rebalance(
        equity=account.equity,
        signal=signal,
        current_positions=current_positions,
        current_market_values=current_market_values,
        latest_prices=latest_prices,
    )

    if not submit:
        return actions, [], state

    if not is_paper and not allow_live:
        raise RuntimeError(
            "Live trading is blocked. Set AI_INVESTING_ENABLE_LIVE=1 to allow it."
        )

    responses: list[dict[str, object]] = []
    for action in actions:
        response = client.submit_market_order(
            symbol=action.symbol,
            side=action.side,
            qty=action.qty,
            notional=None if action.qty is not None else action.notional,
        )
        responses.append(response)

    state.last_rebalance_date = signal_date
    save_state(state_path, state)
    return actions, responses, state
