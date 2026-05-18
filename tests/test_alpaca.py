from __future__ import annotations

import json
import unittest
from urllib import error
from unittest.mock import patch

from ai_investing.alpaca import AlpacaClient
from ai_investing.config import BrokerConfig


class AlpacaTests(unittest.TestCase):
    def test_request_surfaces_tls_guidance_on_url_error(self) -> None:
        client = AlpacaClient(
            BrokerConfig(
                api_key="key",
                secret_key="secret",
                paper=True,
                trading_base_url="https://paper-api.alpaca.markets",
                market_data_base_url="https://data.alpaca.markets",
            )
        )
        with patch("ai_investing.alpaca.request.urlopen", side_effect=error.URLError("ssl failure")):
            with self.assertRaisesRegex(RuntimeError, "trusted HTTPS connection to Alpaca"):
                client.get_clock()

    def test_close_all_positions_sends_delete_request(self) -> None:
        client = AlpacaClient(
            BrokerConfig(
                api_key="key",
                secret_key="secret",
                paper=True,
                trading_base_url="https://paper-api.alpaca.markets",
                market_data_base_url="https://data.alpaca.markets",
            )
        )

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps([{"symbol": "SPY"}]).encode("utf-8")

        seen = {}

        def _fake_urlopen(req, timeout, context):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            return _FakeResponse()

        with patch("ai_investing.alpaca.request.urlopen", side_effect=_fake_urlopen):
            payload = client.close_all_positions(cancel_orders=True)

        self.assertEqual(payload, [{"symbol": "SPY"}])
        self.assertEqual(seen["method"], "DELETE")
        self.assertIn("/v2/positions?cancel_orders=true", seen["url"])


if __name__ == "__main__":
    unittest.main()
