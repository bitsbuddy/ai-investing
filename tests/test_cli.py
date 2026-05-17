from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ai_investing.cli import _run_paper_setup_command
from ai_investing.config import BrokerConfig, RuntimeConfig


class CLITests(unittest.TestCase):
    def test_paper_setup_writes_env_file_with_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env.paper"
            runtime_config = RuntimeConfig(
                enable_live=False,
                enable_official_news=True,
                state_path=Path(".ai_investing_state.json"),
                default_feed="iex",
                risk_on_universe=("SPY", "QQQ"),
                defensive_universe=("TLT", "IEF"),
                research_snapshot_path=Path("examples/research_snapshot.example.json"),
                research_max_age_days=45,
                official_news_lookback_days=14,
                require_official_news=False,
                sec_user_agent="AI-Investing tests@example.com",
                max_price_drift_pct=0.02,
            )
            broker_config = BrokerConfig(
                api_key="paper-key",
                secret_key="paper-secret",
                paper=True,
                trading_base_url="https://paper-api.alpaca.markets",
                market_data_base_url="https://data.alpaca.markets",
            )
            args = argparse.Namespace(
                env_file=str(env_path),
                research_snapshot=None,
                sec_user_agent=None,
                force=False,
            )

            output = io.StringIO()
            with redirect_stdout(output):
                _run_paper_setup_command(broker_config, runtime_config, args)

            contents = env_path.read_text()
            self.assertIn("ALPACA_PAPER=true", contents)
            self.assertIn("AI_INVESTING_ENABLE_LIVE=0", contents)
            self.assertIn(
                "AI_INVESTING_RESEARCH_SNAPSHOT_PATH=examples/research_snapshot.example.json",
                contents,
            )
            rendered = output.getvalue()
            self.assertIn("Wrote paper config", rendered)
            self.assertIn("trade --submit", rendered)

    def test_paper_setup_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env.paper"
            env_path.write_text("ALPACA_PAPER=true\n")
            runtime_config = RuntimeConfig(
                enable_live=False,
                enable_official_news=True,
                state_path=Path(".ai_investing_state.json"),
                default_feed="iex",
                risk_on_universe=("SPY",),
                defensive_universe=("TLT",),
                research_snapshot_path=None,
                research_max_age_days=45,
                official_news_lookback_days=14,
                require_official_news=False,
                sec_user_agent="AI-Investing tests@example.com",
                max_price_drift_pct=0.02,
            )
            broker_config = BrokerConfig(
                api_key="",
                secret_key="",
                paper=True,
                trading_base_url="https://paper-api.alpaca.markets",
                market_data_base_url="https://data.alpaca.markets",
            )
            args = argparse.Namespace(
                env_file=str(env_path),
                research_snapshot=None,
                sec_user_agent=None,
                force=False,
            )

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                _run_paper_setup_command(broker_config, runtime_config, args)


if __name__ == "__main__":
    unittest.main()
