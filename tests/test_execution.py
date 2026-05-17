from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_investing.execution import execute_rebalance, load_state
from ai_investing.models import AccountSnapshot, Position, Signal


class _FakeClient:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.fail_on_call = fail_on_call
        self.submit_calls = 0
        self.orders_by_client_order_id: dict[str, dict[str, object]] = {}

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(equity=100000.0, cash=100000.0, buying_power=100000.0)

    def get_positions(self) -> list[Position]:
        return []

    def get_latest_trade_prices(self, *, symbols: list[str], feed: str) -> dict[str, float]:
        prices = {"SPY": 100.0, "QQQ": 200.0}
        return {symbol: prices[symbol] for symbol in symbols}

    def get_order_by_client_order_id(self, client_order_id: str) -> dict[str, object] | None:
        return self.orders_by_client_order_id.get(client_order_id)

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, object]:
        self.submit_calls += 1
        if self.fail_on_call is not None and self.submit_calls == self.fail_on_call:
            raise RuntimeError("simulated order failure")
        response = {
            "id": f"order-{self.submit_calls}",
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "notional": notional,
            "client_order_id": client_order_id,
        }
        if client_order_id is not None:
            self.orders_by_client_order_id[client_order_id] = response
        return response


class ExecutionTests(unittest.TestCase):
    def test_execute_rebalance_resumes_after_partial_failure(self) -> None:
        signal = Signal(
            as_of=date(2026, 5, 16),
            regime="risk_on",
            weights={"SPY": 0.40, "QQQ": 0.40},
            diagnostics={},
        )
        latest_prices = {"SPY": 100.0, "QQQ": 200.0}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"

            first_client = _FakeClient(fail_on_call=2)
            with self.assertRaisesRegex(RuntimeError, "simulated order failure"):
                execute_rebalance(
                    client=first_client,
                    signal=signal,
                    state_path=state_path,
                    latest_prices=latest_prices,
                    allow_live=False,
                    is_paper=True,
                    submit=True,
                    force=False,
                    live_price_feed="iex",
                    max_price_drift_pct=0.02,
                )

            state_after_failure = load_state(state_path)
            self.assertIsNotNone(state_after_failure.pending_rebalance)
            assert state_after_failure.pending_rebalance is not None
            submitted = [
                order
                for order in state_after_failure.pending_rebalance.actions
                if order.submitted_order_id
            ]
            self.assertEqual(len(submitted), 1)

            second_client = _FakeClient()
            second_client.orders_by_client_order_id.update(
                first_client.orders_by_client_order_id
            )
            actions, responses, final_state = execute_rebalance(
                client=second_client,
                signal=signal,
                state_path=state_path,
                latest_prices=latest_prices,
                allow_live=False,
                is_paper=True,
                submit=True,
                force=False,
                live_price_feed="iex",
                max_price_drift_pct=0.02,
            )

            self.assertEqual(len(actions), 2)
            self.assertEqual(len(responses), 1)
            self.assertIsNone(final_state.pending_rebalance)
            self.assertEqual(final_state.last_rebalance_date, "2026-05-16")


if __name__ == "__main__":
    unittest.main()
