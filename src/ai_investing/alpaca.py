from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from urllib import error, parse, request

from .config import BrokerConfig
from .models import AccountSnapshot, ClockSnapshot, Position
from .tls import build_ssl_context, tls_help_message


class AlpacaClient:
    def __init__(self, config: BrokerConfig) -> None:
        self._config = config

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._config.api_key,
            "APCA-API-SECRET-KEY": self._config.secret_key,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        *,
        base_url: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        query_string = ""
        if query:
            clean_query = {
                key: value
                for key, value in query.items()
                if value is not None and value != ""
            }
            query_string = "?" + parse.urlencode(clean_query, doseq=True)

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = request.Request(
            f"{base_url}{path}{query_string}",
            method=method,
            headers=self._headers(),
            data=data,
        )

        try:
            with request.urlopen(req, timeout=30, context=build_ssl_context()) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else None
        except error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8")
            raise RuntimeError(
                f"Alpaca API request failed ({exc.code}) {method} {path}: {detail}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(
                "Unable to establish a trusted HTTPS connection to Alpaca. "
                f"{tls_help_message()} Original error: {exc}"
            ) from exc

    def get_account(self) -> AccountSnapshot:
        payload = self._request(
            base_url=self._config.trading_base_url,
            method="GET",
            path="/v2/account",
        )
        return AccountSnapshot(
            equity=float(payload["equity"]),
            cash=float(payload["cash"]),
            buying_power=float(payload["buying_power"]),
        )

    def get_positions(self) -> list[Position]:
        payload = self._request(
            base_url=self._config.trading_base_url,
            method="GET",
            path="/v2/positions",
        )
        return [
            Position(
                symbol=item["symbol"],
                qty=float(item["qty"]),
                market_value=float(item["market_value"]),
            )
            for item in payload
        ]

    def get_clock(self) -> ClockSnapshot:
        payload = self._request(
            base_url=self._config.trading_base_url,
            method="GET",
            path="/v2/clock",
        )
        return ClockSnapshot(
            is_open=bool(payload["is_open"]),
            timestamp=str(payload["timestamp"]),
        )

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if qty is None and notional is None:
            raise ValueError("Either qty or notional must be provided.")
        if qty is not None and notional is not None:
            raise ValueError("Provide either qty or notional, not both.")

        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        if qty is not None:
            body["qty"] = f"{qty:.6f}".rstrip("0").rstrip(".")
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        if client_order_id is not None:
            body["client_order_id"] = client_order_id

        return self._request(
            base_url=self._config.trading_base_url,
            method="POST",
            path="/v2/orders",
            body=body,
        )

    def get_order_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        return self._request(
            base_url=self._config.trading_base_url,
            method="GET",
            path="/v2/orders:by_client_order_id",
            query={"client_order_id": client_order_id},
            allow_not_found=True,
        )

    def get_order_by_id(self, order_id: str) -> dict[str, Any] | None:
        return self._request(
            base_url=self._config.trading_base_url,
            method="GET",
            path=f"/v2/orders/{order_id}",
            allow_not_found=True,
        )

    def get_daily_closes(
        self,
        *,
        symbols: list[str],
        start: date,
        end: date,
        feed: str,
    ) -> dict[str, dict[date, float]]:
        bars_by_symbol: dict[str, dict[date, float]] = {symbol: {} for symbol in symbols}
        page_token: str | None = None

        while True:
            query = {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": f"{start.isoformat()}T00:00:00Z",
                "end": f"{end.isoformat()}T23:59:59Z",
                "adjustment": "split",
                "feed": feed,
                "sort": "asc",
                "limit": 10000,
                "page_token": page_token,
            }
            payload = self._request(
                base_url=self._config.market_data_base_url,
                method="GET",
                path="/v2/stocks/bars",
                query=query,
            )

            for symbol, bars in payload.get("bars", {}).items():
                series = bars_by_symbol.setdefault(symbol, {})
                for bar in bars:
                    bar_time = datetime.fromisoformat(
                        bar["t"].replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    series[bar_time.date()] = float(bar["c"])

            page_token = payload.get("next_page_token")
            if not page_token:
                return bars_by_symbol

    def get_latest_trade_prices(
        self, *, symbols: list[str], feed: str
    ) -> dict[str, float]:
        payload = self._request(
            base_url=self._config.market_data_base_url,
            method="GET",
            path="/v2/stocks/trades/latest",
            query={"symbols": ",".join(symbols), "feed": feed},
        )
        trades = payload.get("trades", {})
        return {
            symbol: float(trade["p"])
            for symbol, trade in trades.items()
            if trade and trade.get("p") is not None
        }
