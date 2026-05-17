from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .alpaca import AlpacaClient
from .models import AccountSnapshot, RebalanceAction, Signal


@dataclass
class PendingOrderState:
    client_order_id: str
    side: str
    symbol: str
    notional: float
    qty: float | None
    reason: str
    reference_price: float | None = None
    submitted_order_id: str | None = None

    def to_action(self) -> RebalanceAction:
        return RebalanceAction(
            side=self.side,
            symbol=self.symbol,
            notional=self.notional,
            qty=self.qty,
            reason=self.reason,
            reference_price=self.reference_price,
        )


@dataclass
class PendingRebalanceState:
    rebalance_id: str
    signal_date: str
    actions: list[PendingOrderState]


@dataclass
class RuntimeState:
    high_water_mark: float = 0.0
    last_rebalance_date: str | None = None
    pending_rebalance: PendingRebalanceState | None = None


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    payload = json.loads(path.read_text())
    pending_payload = payload.get("pending_rebalance")
    return RuntimeState(
        high_water_mark=float(payload.get("high_water_mark", 0.0)),
        last_rebalance_date=payload.get("last_rebalance_date"),
        pending_rebalance=_load_pending_rebalance(pending_payload),
    )


def save_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_state_to_payload(state), indent=2, sort_keys=True) + "\n")


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
            sell_notional = abs(delta)
            if price is not None and price > 0:
                sell_notional = min(abs(delta), qty * price)
            actions.append(
                RebalanceAction(
                    side="sell",
                    symbol=symbol,
                    notional=sell_notional,
                    qty=qty,
                    reason="reduce_or_exit",
                    reference_price=price,
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
                    reference_price=price,
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
    live_price_feed: str,
    max_price_drift_pct: float,
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
    current_positions = {position.symbol: position.qty for position in positions}
    current_market_values = {
        position.symbol: position.market_value for position in positions
    }
    planned_actions = plan_rebalance(
        equity=account.equity,
        signal=signal,
        current_positions=current_positions,
        current_market_values=current_market_values,
        latest_prices=latest_prices,
    )
    pending_rebalance = _resolve_pending_rebalance(
        state=state,
        signal=signal,
        planned_actions=planned_actions,
        force=force,
    )

    if pending_rebalance is None and not force and state.last_rebalance_date == signal_date:
        return [], [], state

    actions = (
        [order.to_action() for order in pending_rebalance.actions]
        if pending_rebalance is not None
        else planned_actions
    )

    if not submit:
        return actions, [], state

    if not actions:
        state.last_rebalance_date = signal_date
        save_state(state_path, state)
        return [], [], state

    if not is_paper and not allow_live:
        raise RuntimeError(
            "Live trading is blocked. Set AI_INVESTING_ENABLE_LIVE=1 to allow it."
        )

    if pending_rebalance is None:
        pending_rebalance = _build_pending_rebalance(signal, actions)
        state.pending_rebalance = pending_rebalance
        save_state(state_path, state)

    _validate_rebalance_capacity(
        account,
        [order.to_action() for order in pending_rebalance.actions],
        max_price_drift_pct=max_price_drift_pct,
    )
    live_prices = client.get_latest_trade_prices(
        symbols=sorted({order.symbol for order in pending_rebalance.actions}),
        feed=live_price_feed,
    )
    _validate_price_drift(
        [order.to_action() for order in pending_rebalance.actions],
        live_prices,
        max_price_drift_pct=max_price_drift_pct,
    )

    responses: list[dict[str, object]] = []
    for order in pending_rebalance.actions:
        if order.submitted_order_id:
            continue

        existing_order = client.get_order_by_client_order_id(order.client_order_id)
        if existing_order is not None:
            order.submitted_order_id = str(existing_order.get("id", ""))
            save_state(state_path, state)
            responses.append(existing_order)
            continue

        response = client.submit_market_order(
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            notional=None if order.qty is not None else order.notional,
            client_order_id=order.client_order_id,
        )
        order.submitted_order_id = str(response.get("id", ""))
        save_state(state_path, state)
        responses.append(response)

    state.pending_rebalance = None
    state.last_rebalance_date = signal_date
    save_state(state_path, state)
    return actions, responses, state


def _resolve_pending_rebalance(
    *,
    state: RuntimeState,
    signal: Signal,
    planned_actions: list[RebalanceAction],
    force: bool,
) -> PendingRebalanceState | None:
    pending = state.pending_rebalance
    if pending is None:
        return None
    if force:
        return pending

    signal_date = signal.as_of.isoformat()
    if pending.signal_date != signal_date:
        raise RuntimeError(
            "A previous rebalance is still pending. Resolve it before starting a new one."
        )

    expected_id = _build_rebalance_id(signal, planned_actions)
    if pending.rebalance_id != expected_id:
        raise RuntimeError(
            "Pending rebalance does not match the newly planned actions. Review manually before retrying."
        )
    return pending


def _build_pending_rebalance(
    signal: Signal, actions: list[RebalanceAction]
) -> PendingRebalanceState:
    rebalance_id = _build_rebalance_id(signal, actions)
    signal_token = signal.as_of.strftime("%Y%m%d")
    return PendingRebalanceState(
        rebalance_id=rebalance_id,
        signal_date=signal.as_of.isoformat(),
        actions=[
            PendingOrderState(
                client_order_id=f"aii-{signal_token}-{index:02d}-{rebalance_id[:12]}",
                side=action.side,
                symbol=action.symbol,
                notional=action.notional,
                qty=action.qty,
                reason=action.reason,
                reference_price=action.reference_price,
            )
            for index, action in enumerate(actions)
        ],
    )


def _build_rebalance_id(signal: Signal, actions: list[RebalanceAction]) -> str:
    action_tokens = [
        "|".join(
            [
                action.side,
                action.symbol,
                f"{action.notional:.6f}",
                "" if action.qty is None else f"{action.qty:.6f}",
                action.reason,
            ]
        )
        for action in actions
    ]
    weights_token = "|".join(
        f"{symbol}:{signal.weights[symbol]:.8f}" for symbol in sorted(signal.weights)
    )
    payload = f"{signal.as_of.isoformat()}::{weights_token}::{';'.join(action_tokens)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _validate_rebalance_capacity(
    account: AccountSnapshot,
    actions: list[RebalanceAction],
    *,
    max_price_drift_pct: float,
) -> None:
    buy_notional = sum(action.notional for action in actions if action.side == "buy")
    sell_notional = sum(action.notional for action in actions if action.side == "sell")
    required_buying_power = buy_notional * (1.0 + max_price_drift_pct)
    available_without_margin = account.cash + (sell_notional * (1.0 - max_price_drift_pct))
    if required_buying_power > account.buying_power + 1e-6:
        raise RuntimeError(
            "Rebalance requires more buying power than is currently available."
        )
    if required_buying_power > available_without_margin + 1e-6:
        raise RuntimeError(
            "Rebalance would require margin or insufficient sell proceeds. Review order sizing before submission."
        )


def _validate_price_drift(
    actions: list[RebalanceAction],
    live_prices: dict[str, float],
    *,
    max_price_drift_pct: float,
) -> None:
    for action in actions:
        if action.reference_price is None or action.reference_price <= 0:
            raise RuntimeError(
                f"Missing reference price for {action.symbol}; refusing to submit orders."
            )
        live_price = live_prices.get(action.symbol)
        if live_price is None:
            raise RuntimeError(
                f"Missing current market price for {action.symbol}; refusing to submit orders."
            )
        drift_pct = abs(live_price - action.reference_price) / action.reference_price
        if drift_pct > max_price_drift_pct:
            raise RuntimeError(
                f"Price drift for {action.symbol} is {drift_pct:.2%}, which exceeds the allowed {max_price_drift_pct:.2%}."
            )


def _load_pending_rebalance(
    payload: dict[str, object] | None,
) -> PendingRebalanceState | None:
    if payload is None:
        return None
    return PendingRebalanceState(
        rebalance_id=str(payload["rebalance_id"]),
        signal_date=str(payload["signal_date"]),
        actions=[
            PendingOrderState(
                client_order_id=str(action["client_order_id"]),
                side=str(action["side"]),
                symbol=str(action["symbol"]),
                notional=float(action["notional"]),
                qty=None if action.get("qty") is None else float(action["qty"]),
                reason=str(action["reason"]),
                reference_price=(
                    None
                    if action.get("reference_price") is None
                    else float(action["reference_price"])
                ),
                submitted_order_id=(
                    None
                    if action.get("submitted_order_id") is None
                    else str(action["submitted_order_id"])
                ),
            )
            for action in payload.get("actions", [])
        ],
    )


def _state_to_payload(state: RuntimeState) -> dict[str, object]:
    payload: dict[str, object] = {
        "high_water_mark": state.high_water_mark,
        "last_rebalance_date": state.last_rebalance_date,
    }
    if state.pending_rebalance is not None:
        payload["pending_rebalance"] = {
            "rebalance_id": state.pending_rebalance.rebalance_id,
            "signal_date": state.pending_rebalance.signal_date,
            "actions": [asdict(action) for action in state.pending_rebalance.actions],
        }
    return payload
