from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class PendingRebalanceStatus:
    has_open_orders: bool
    completed_actions: int
    open_actions: int
    terminal_actions: int


@dataclass
class RuntimeState:
    high_water_mark: float = 0.0
    last_rebalance_date: str | None = None
    pending_rebalance: PendingRebalanceState | None = None


@dataclass(frozen=True)
class ExecutionResult:
    actions: list[RebalanceAction]
    responses: list[dict[str, object]]
    state: RuntimeState
    submitted_actions: list[RebalanceAction] = field(default_factory=list)
    skipped_messages: list[str] = field(default_factory=list)


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
    payload = json.dumps(_state_to_payload(state), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(payload)
        temp_name = handle.name
    os.replace(temp_name, path)


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
) -> ExecutionResult:
    account = client.get_account()
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
    pending_status: PendingRebalanceStatus | None = None
    if state.pending_rebalance is not None:
        pending_status = _reconcile_pending_rebalance(client, state.pending_rebalance)
        save_state(state_path, state)
        if pending_status.has_open_orders:
            actions = [order.to_action() for order in state.pending_rebalance.actions]
            return ExecutionResult(actions=actions, responses=[], state=state)
        state.pending_rebalance = None
        save_state(state_path, state)

    positions = client.get_positions()
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
    _ensure_pending_rebalance_matches_signal(
        state=state,
        signal=signal,
        planned_actions=planned_actions,
        force=force,
    )

    if not force and state.last_rebalance_date == signal_date and pending_status is None:
        return ExecutionResult(actions=[], responses=[], state=state)

    if not planned_actions:
        state.last_rebalance_date = signal_date
        save_state(state_path, state)
        return ExecutionResult(actions=[], responses=[], state=state)

    if not submit:
        return ExecutionResult(actions=planned_actions, responses=[], state=state)

    if not is_paper and not allow_live:
        raise RuntimeError(
            "Live trading is blocked. Set AI_INVESTING_ENABLE_LIVE=1 to allow it."
        )

    live_prices = client.get_latest_trade_prices(
        symbols=sorted({action.symbol for action in planned_actions}),
        feed=live_price_feed,
    )
    eligible_actions, skipped_messages = _filter_actions_for_price_drift(
        planned_actions,
        live_prices,
        max_price_drift_pct=max_price_drift_pct,
    )
    if not eligible_actions:
        return ExecutionResult(
            actions=planned_actions,
            responses=[],
            state=state,
            submitted_actions=[],
            skipped_messages=skipped_messages,
        )

    pending_rebalance = _build_pending_rebalance(signal, eligible_actions)
    state.pending_rebalance = pending_rebalance
    save_state(state_path, state)

    _validate_rebalance_capacity(
        account,
        eligible_actions,
        max_price_drift_pct=max_price_drift_pct,
    )

    responses: list[dict[str, object]] = []
    for order in pending_rebalance.actions:
        existing_order = _get_existing_order(client, order)
        if existing_order is not None:
            order.submitted_order_id = str(existing_order.get("id", ""))
            responses.append(existing_order)
            save_state(state_path, state)
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

    pending_status = _reconcile_pending_rebalance(client, pending_rebalance)
    save_state(state_path, state)
    if not pending_status.has_open_orders:
        state.pending_rebalance = None
        save_state(state_path, state)
        positions = client.get_positions()
        current_positions = {position.symbol: position.qty for position in positions}
        current_market_values = {
            position.symbol: position.market_value for position in positions
        }
        residual_actions = plan_rebalance(
            equity=account.equity,
            signal=signal,
            current_positions=current_positions,
            current_market_values=current_market_values,
            latest_prices=latest_prices,
        )
        if not residual_actions:
            state.last_rebalance_date = signal_date
            save_state(state_path, state)
        return ExecutionResult(
            actions=residual_actions,
            responses=responses,
            state=state,
            submitted_actions=eligible_actions,
            skipped_messages=skipped_messages,
        )

    return ExecutionResult(
        actions=eligible_actions,
        responses=responses,
        state=state,
        submitted_actions=eligible_actions,
        skipped_messages=skipped_messages,
    )


def _ensure_pending_rebalance_matches_signal(
    *,
    state: RuntimeState,
    signal: Signal,
    planned_actions: list[RebalanceAction],
    force: bool,
) -> None:
    pending = state.pending_rebalance
    if pending is None:
        return
    if force:
        return

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
    return


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


def _get_existing_order(
    client: AlpacaClient, order: PendingOrderState
) -> dict[str, object] | None:
    existing_order = client.get_order_by_client_order_id(order.client_order_id)
    if existing_order is not None:
        return existing_order
    if order.submitted_order_id:
        return client.get_order_by_id(order.submitted_order_id)
    return None


def _reconcile_pending_rebalance(
    client: AlpacaClient, pending_rebalance: PendingRebalanceState
) -> PendingRebalanceStatus:
    open_actions = 0
    terminal_actions = 0
    completed_actions = 0
    for order in pending_rebalance.actions:
        if order.submitted_order_id is None:
            continue
        existing_order = _get_existing_order(client, order)
        if existing_order is None:
            raise RuntimeError(
                f"Unable to reconcile previously submitted order {order.client_order_id}."
            )
        order.submitted_order_id = str(existing_order.get("id", order.submitted_order_id))
        status = str(existing_order.get("status", "")).lower()
        if _is_order_open(status, existing_order):
            open_actions += 1
        else:
            terminal_actions += 1
            if _is_order_completed(existing_order):
                completed_actions += 1
    return PendingRebalanceStatus(
        has_open_orders=open_actions > 0,
        completed_actions=completed_actions,
        open_actions=open_actions,
        terminal_actions=terminal_actions,
    )


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


def _filter_actions_for_price_drift(
    actions: list[RebalanceAction],
    live_prices: dict[str, float],
    *,
    max_price_drift_pct: float,
) -> tuple[list[RebalanceAction], list[str]]:
    eligible_actions: list[RebalanceAction] = []
    skipped_messages: list[str] = []
    for action in actions:
        if action.reference_price is None or action.reference_price <= 0:
            raise RuntimeError(
                f"Missing reference price for {action.symbol}; refusing to submit orders."
            )
        live_price = live_prices.get(action.symbol)
        if live_price is None:
            skipped_messages.append(
                f"{action.symbol}: missing current market price; skipped for this run."
            )
            continue
        drift_pct = abs(live_price - action.reference_price) / action.reference_price
        if drift_pct > max_price_drift_pct:
            skipped_messages.append(
                f"{action.symbol}: price drift is {drift_pct:.2%}, above the allowed {max_price_drift_pct:.2%}; skipped for this run."
            )
            continue
        eligible_actions.append(action)
    return eligible_actions, skipped_messages


def _is_order_open(status: str, order_payload: dict[str, object]) -> bool:
    if status == "filled":
        return False
    if status == "calculated" and _is_order_completed(order_payload):
        return False
    return status not in {"canceled", "expired", "rejected", "replaced"}


def _is_order_completed(order_payload: dict[str, object]) -> bool:
    status = str(order_payload.get("status", "")).lower()
    if status == "filled":
        return True
    filled_qty_raw = order_payload.get("filled_qty")
    qty_raw = order_payload.get("qty")
    if filled_qty_raw is None or qty_raw is None:
        return False
    try:
        filled_qty = float(filled_qty_raw)
        qty = float(qty_raw)
    except (TypeError, ValueError):
        return False
    return qty > 0 and filled_qty >= qty


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
