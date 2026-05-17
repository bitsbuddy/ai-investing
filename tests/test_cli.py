from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ai_investing.cli import (
    _resolve_research_validation_date,
    _run_automation_setup_command,
    _run_automation_trade_command,
    _run_paper_setup_command,
)
from ai_investing.config import BrokerConfig, RuntimeConfig
from ai_investing.models import ClockSnapshot


class _FakeClockClient:
    def __init__(self, *, is_open: bool, timestamp: str = "2026-05-17T14:05:51-04:00") -> None:
        self._clock = ClockSnapshot(is_open=is_open, timestamp=timestamp)
        self.clock_calls = 0

    def get_clock(self) -> ClockSnapshot:
        self.clock_calls += 1
        return self._clock


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

    def test_automation_setup_writes_runner_and_cron_template(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env.paper"
            env_path.write_text("ALPACA_PAPER=true\nAI_INVESTING_ENABLE_LIVE=0\n")
            script_path = root / "scripts" / "run_paper_trade.sh"
            cron_path = root / "automation" / "paper_trade.cron"
            control_path = root / "automation" / "paper_trade.enabled"
            status_path = root / "automation" / "paper_trade.status"
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
            args = argparse.Namespace(
                env_file=str(env_path),
                script_file=str(script_path),
                cron_file=str(cron_path),
                status_file=str(status_path),
                hour=9,
                minute=40,
                preview_only=False,
                force=False,
            )

            output = io.StringIO()
            cwd_before = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(output):
                    _run_automation_setup_command(runtime_config, args)
            finally:
                os.chdir(cwd_before)

            script_contents = script_path.read_text()
            cron_contents = cron_path.read_text()
            control_contents = control_path.read_text()
            status_contents = status_path.read_text()
            self.assertIn("-m ai_investing.cli \"${automation_args[@]}\"", script_contents)
            self.assertIn('automation_args=("automation-run")', script_contents)
            self.assertIn(str(env_path), script_contents)
            self.assertIn(str(control_path), script_contents)
            self.assertIn(str(status_path), script_contents)
            self.assertIn("/bin/zsh", cron_contents)
            self.assertIn("40 9 * * 1-5", cron_contents)
            self.assertEqual(control_contents, "enabled=1\n")
            self.assertIn("run_phase=idle", status_contents)
            self.assertIn("last_result=never", status_contents)
            self.assertIn("crontab", output.getvalue())

    def test_automation_trade_manual_run_falls_back_to_preview_when_market_is_closed(self) -> None:
        runtime_config = RuntimeConfig(
            enable_live=False,
            enable_official_news=True,
            state_path=Path(".ai_investing_state.json"),
            default_feed="iex",
            risk_on_universe=("SPY",),
            defensive_universe=("TLT",),
            research_snapshot_path=Path("examples/research_snapshot.example.json"),
            research_max_age_days=45,
            official_news_lookback_days=14,
            require_official_news=False,
            sec_user_agent="AI-Investing tests@example.com",
            max_price_drift_pct=0.02,
        )
        args = argparse.Namespace(
            command="automation-run",
            lookback_days=900,
            feed=None,
            research_snapshot=None,
            training_window_bars=756,
            force=False,
            no_official_news=False,
            official_news_lookback_days=None,
            require_official_news=False,
            manual=True,
            preview_only=False,
        )
        client = _FakeClockClient(is_open=False)

        output = io.StringIO()
        with patch("ai_investing.cli._run_trade_command") as run_trade:
            with redirect_stdout(output):
                _run_automation_trade_command(client, True, runtime_config, args)

        self.assertEqual(client.clock_calls, 1)
        run_trade.assert_called_once()
        forwarded_args = run_trade.call_args.args[3]
        self.assertFalse(forwarded_args.submit)
        self.assertIn("Running a preview instead of submitting orders", output.getvalue())

    def test_automation_trade_scheduled_run_skips_when_market_is_closed(self) -> None:
        runtime_config = RuntimeConfig(
            enable_live=False,
            enable_official_news=True,
            state_path=Path(".ai_investing_state.json"),
            default_feed="iex",
            risk_on_universe=("SPY",),
            defensive_universe=("TLT",),
            research_snapshot_path=Path("examples/research_snapshot.example.json"),
            research_max_age_days=45,
            official_news_lookback_days=14,
            require_official_news=False,
            sec_user_agent="AI-Investing tests@example.com",
            max_price_drift_pct=0.02,
        )
        args = argparse.Namespace(
            command="automation-run",
            lookback_days=900,
            feed=None,
            research_snapshot=None,
            training_window_bars=756,
            force=False,
            no_official_news=False,
            official_news_lookback_days=None,
            require_official_news=False,
            manual=False,
            preview_only=False,
        )
        client = _FakeClockClient(is_open=False)

        output = io.StringIO()
        with patch("ai_investing.cli._run_trade_command") as run_trade:
            with redirect_stdout(output):
                _run_automation_trade_command(client, True, runtime_config, args)

        self.assertEqual(client.clock_calls, 1)
        run_trade.assert_not_called()
        self.assertIn("Skipping scheduled run", output.getvalue())

    def test_automation_trade_preview_only_skips_market_hours_check(self) -> None:
        runtime_config = RuntimeConfig(
            enable_live=False,
            enable_official_news=True,
            state_path=Path(".ai_investing_state.json"),
            default_feed="iex",
            risk_on_universe=("SPY",),
            defensive_universe=("TLT",),
            research_snapshot_path=Path("examples/research_snapshot.example.json"),
            research_max_age_days=45,
            official_news_lookback_days=14,
            require_official_news=False,
            sec_user_agent="AI-Investing tests@example.com",
            max_price_drift_pct=0.02,
        )
        args = argparse.Namespace(
            command="automation-run",
            lookback_days=900,
            feed=None,
            research_snapshot=None,
            training_window_bars=756,
            force=False,
            no_official_news=False,
            official_news_lookback_days=None,
            require_official_news=False,
            manual=False,
            preview_only=True,
        )
        client = _FakeClockClient(is_open=False)

        with patch("ai_investing.cli._run_trade_command") as run_trade:
            _run_automation_trade_command(client, True, runtime_config, args)

        self.assertEqual(client.clock_calls, 0)
        run_trade.assert_called_once()
        forwarded_args = run_trade.call_args.args[3]
        self.assertFalse(forwarded_args.submit)

    def test_research_validation_uses_current_run_date_when_bars_are_older(self) -> None:
        with patch("ai_investing.cli._today_utc", return_value=date(2026, 5, 17)):
            validation_date = _resolve_research_validation_date(date(2026, 5, 15))

        self.assertEqual(validation_date, date(2026, 5, 17))


if __name__ == "__main__":
    unittest.main()
