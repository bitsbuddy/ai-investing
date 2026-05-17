from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
