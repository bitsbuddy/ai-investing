from __future__ import annotations

import argparse
import io
import os
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
    _run_multi_profile_run_command,
    _run_multi_profile_setup_command,
    _run_paper_setup_command,
    _run_reset_account_command,
    _write_trade_rationale_report,
)
from ai_investing.config import BrokerConfig, RuntimeConfig
from ai_investing.execution import ExecutionResult, RuntimeState
from ai_investing.models import (
    ClockSnapshot,
    OfficialNewsContext,
    OfficialNewsItem,
    RebalanceAction,
    ResearchAssessment,
    Signal,
    StrategyParameters,
)


class _FakeClockClient:
    def __init__(self, *, is_open: bool, timestamp: str = "2026-05-17T14:05:51-04:00") -> None:
        self._clock = ClockSnapshot(is_open=is_open, timestamp=timestamp)
        self.clock_calls = 0

    def get_clock(self) -> ClockSnapshot:
        self.clock_calls += 1
        return self._clock


class _FakeAccount:
    def __init__(self, equity: float) -> None:
        self.equity = equity


class _FakeAlpacaClient:
    def __init__(self, _broker_config: BrokerConfig) -> None:
        self.account = _FakeAccount(101000.0)

    def get_account(self) -> _FakeAccount:
        return self.account


class _FakeResetClient:
    def __init__(self, positions=None) -> None:
        self._positions = list(positions or [])
        self.cancel_calls = 0
        self.close_calls = 0

    def get_positions(self):
        return list(self._positions)

    def cancel_all_orders(self):
        self.cancel_calls += 1
        return [{"id": "order-1"}]

    def close_all_positions(self, *, cancel_orders: bool):
        self.close_calls += 1
        return [{"id": "liq-1", "cancel_orders": cancel_orders}]


def _runtime_config(**overrides) -> RuntimeConfig:
    values = {
        "profile_name": "Balanced",
        "risk_profile": "balanced",
        "enable_live": False,
        "enable_official_news": True,
        "enable_llm_news": False,
        "state_path": Path(".ai_investing_state.json"),
        "performance_baseline": None,
        "default_feed": "iex",
        "risk_on_universe": ("SPY",),
        "equity_universe": ("MSFT",),
        "defensive_universe": ("TLT",),
        "research_snapshot_path": Path("examples/research_snapshot.example.json"),
        "research_max_age_days": 45,
        "official_news_lookback_days": 14,
        "require_official_news": False,
        "require_llm_news": False,
        "sec_user_agent": "AI-Investing tests@example.com",
        "llm_news_api_key": "",
        "llm_news_model": "gpt-5-mini",
        "llm_news_base_url": "https://api.openai.com/v1",
        "llm_news_max_items": 8,
        "llm_news_max_chars": 6000,
        "max_price_drift_pct": 0.02,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


class CLITests(unittest.TestCase):
    def test_paper_setup_writes_env_file_with_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env.paper"
            runtime_config = _runtime_config(
                risk_on_universe=("SPY", "QQQ"),
                defensive_universe=("TLT", "IEF"),
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
            self.assertIn("AI_INVESTING_EQUITIES=MSFT", contents)
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
            runtime_config = _runtime_config(research_snapshot_path=None)
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
            runtime_config = _runtime_config(research_snapshot_path=None)
            args = argparse.Namespace(
                env_file=str(env_path),
                script_file=str(script_path),
                cron_file=str(cron_path),
                status_file=str(status_path),
                manifest=str(root / "profiles" / "profile_matrix.json"),
                times="",
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

    def test_automation_setup_can_write_multiple_intraday_schedule_times(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env.paper"
            env_path.write_text("ALPACA_PAPER=true\nAI_INVESTING_ENABLE_LIVE=0\n")
            script_path = root / "scripts" / "run_paper_trade.sh"
            cron_path = root / "automation" / "paper_trade.cron"
            status_path = root / "automation" / "paper_trade.status"
            runtime_config = _runtime_config(research_snapshot_path=None)
            args = argparse.Namespace(
                env_file=str(env_path),
                script_file=str(script_path),
                cron_file=str(cron_path),
                status_file=str(status_path),
                manifest=str(root / "profiles" / "profile_matrix.json"),
                times="09:40,12:30,15:45",
                hour=9,
                minute=40,
                preview_only=False,
                force=False,
            )

            cwd_before = Path.cwd()
            try:
                os.chdir(root)
                _run_automation_setup_command(runtime_config, args)
            finally:
                os.chdir(cwd_before)

            cron_contents = cron_path.read_text()
            self.assertIn('40 9 * * 1-5 /bin/zsh "', cron_contents)
            self.assertIn('30 12 * * 1-5 /bin/zsh "', cron_contents)
            self.assertIn('45 15 * * 1-5 /bin/zsh "', cron_contents)

    def test_automation_setup_prefers_profile_matrix_when_manifest_exists(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env.paper"
            env_path.write_text("ALPACA_PAPER=true\nAI_INVESTING_ENABLE_LIVE=0\n")
            manifest_path = root / "profiles" / "profile_matrix.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text('{"profiles":[{"name":"aggressive","env_file":"profiles/aggressive.paper.env"}]}\n')
            script_path = root / "scripts" / "run_paper_trade.sh"
            cron_path = root / "automation" / "paper_trade.cron"
            status_path = root / "automation" / "paper_trade.status"
            runtime_config = _runtime_config(research_snapshot_path=None)
            args = argparse.Namespace(
                env_file=str(env_path),
                script_file=str(script_path),
                cron_file=str(cron_path),
                status_file=str(status_path),
                manifest=str(manifest_path),
                times="",
                hour=9,
                minute=40,
                preview_only=False,
                force=False,
            )

            cwd_before = Path.cwd()
            try:
                os.chdir(root)
                _run_automation_setup_command(runtime_config, args)
            finally:
                os.chdir(cwd_before)

            script_contents = script_path.read_text()
            self.assertIn('multi-profile-run" "--manifest"', script_contents)
            self.assertIn("--allow-failures", script_contents)
            self.assertIn("--submit", script_contents)

    def test_multi_profile_setup_writes_distinct_env_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_config = _runtime_config(research_snapshot_path=None)
            broker_config = BrokerConfig(
                api_key="paper-key",
                secret_key="paper-secret",
                paper=True,
                trading_base_url="https://paper-api.alpaca.markets",
                market_data_base_url="https://data.alpaca.markets",
            )
            args = argparse.Namespace(
                directory=str(root / "profiles"),
                manifest=str(root / "profiles" / "profile_matrix.json"),
                research_snapshot=None,
                sec_user_agent=None,
                force=False,
            )

            output = io.StringIO()
            with redirect_stdout(output):
                _run_multi_profile_setup_command(broker_config, runtime_config, args)

            manifest_text = (root / "profiles" / "profile_matrix.json").read_text()
            conservative_text = (root / "profiles" / "conservative.paper.env").read_text()
            aggressive_text = (root / "profiles" / "aggressive.paper.env").read_text()
            self.assertIn("conservative", manifest_text)
            self.assertIn("aggressive", manifest_text)
            self.assertIn("AI_INVESTING_RISK_PROFILE=conservative", conservative_text)
            self.assertIn("AI_INVESTING_RISK_PROFILE=aggressive", aggressive_text)
            self.assertIn(
                "AI_INVESTING_STATE_PATH=.ai_investing_conservative_state.json",
                conservative_text,
            )
            self.assertIn("different Alpaca paper key", output.getvalue())

    def test_multi_profile_run_skips_placeholder_profiles_and_keeps_successful_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_dir = root / "profiles"
            profile_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = profile_dir / "profile_matrix.json"
            conservative_env = profile_dir / "conservative.paper.env"
            aggressive_env = profile_dir / "aggressive.paper.env"
            conservative_env.write_text(
                "ALPACA_API_KEY=your-conservative-paper-key\n"
                "ALPACA_SECRET_KEY=your-conservative-paper-secret\n"
                "AI_INVESTING_PROFILE_NAME=Conservative\n"
                "AI_INVESTING_RISK_PROFILE=conservative\n"
                "AI_INVESTING_STATE_PATH=.ai_investing_conservative_state.json\n"
            )
            aggressive_env.write_text(
                "ALPACA_API_KEY=real-aggressive-key\n"
                "ALPACA_SECRET_KEY=real-aggressive-secret\n"
                "AI_INVESTING_PROFILE_NAME=Aggressive\n"
                "AI_INVESTING_RISK_PROFILE=aggressive\n"
                "AI_INVESTING_STATE_PATH=.ai_investing_aggressive_state.json\n"
                "AI_INVESTING_PERFORMANCE_BASELINE=100000\n"
            )
            manifest_path.write_text(
                "{\n"
                '  "profiles": [\n'
                f'    {{"name": "conservative", "env_file": "{conservative_env}"}},\n'
                f'    {{"name": "aggressive", "env_file": "{aggressive_env}"}}\n'
                "  ]\n"
                "}\n"
            )
            args = argparse.Namespace(
                manifest=str(manifest_path),
                lookback_days=900,
                feed=None,
                training_window_bars=756,
                submit=True,
                force=False,
                no_official_news=False,
                no_llm_news=False,
                official_news_lookback_days=None,
                require_official_news=False,
                require_llm_news=False,
                manual=False,
                preview_only=False,
                allow_failures=True,
            )

            output = io.StringIO()
            with patch("ai_investing.cli.AlpacaClient", _FakeAlpacaClient):
                with patch("ai_investing.cli._run_automation_trade_command") as run_trade:
                    with redirect_stdout(output):
                        _run_multi_profile_run_command(args)

            rendered = output.getvalue()
            self.assertIn("Profile skipped: missing or placeholder Alpaca credentials.", rendered)
            self.assertIn("Aggressive", rendered)
            self.assertIn("1 succeeded | 1 skipped | 0 failed", rendered)

    def test_reset_account_clears_state_and_liquidates_positions(self) -> None:
        from ai_investing.models import Position

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".ai_investing_aggressive_state.json"
            state_path.write_text('{"high_water_mark": 100000, "last_rebalance_date": "2026-05-15"}\n')
            runtime_config = _runtime_config(state_path=state_path, profile_name="Aggressive")
            client = _FakeResetClient(
                positions=[Position(symbol="SPY", qty=10.0, market_value=5000.0)]
            )
            args = argparse.Namespace(keep_state=False, allow_live=False)

            output = io.StringIO()
            with redirect_stdout(output):
                _run_reset_account_command(client, True, runtime_config, args)

            self.assertEqual(client.cancel_calls, 1)
            self.assertEqual(client.close_calls, 1)
            self.assertIn('"high_water_mark": 0.0', state_path.read_text())
            rendered = output.getvalue()
            self.assertIn("Canceled open orders: 1", rendered)
            self.assertIn("Submitted liquidation orders: 1", rendered)

    def test_automation_trade_manual_run_falls_back_to_preview_when_market_is_closed(self) -> None:
        runtime_config = _runtime_config()
        args = argparse.Namespace(
            command="automation-run",
            lookback_days=900,
            feed=None,
            research_snapshot=None,
            training_window_bars=756,
            force=False,
            no_official_news=False,
            no_llm_news=False,
            official_news_lookback_days=None,
            require_official_news=False,
            require_llm_news=False,
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
        runtime_config = _runtime_config()
        args = argparse.Namespace(
            command="automation-run",
            lookback_days=900,
            feed=None,
            research_snapshot=None,
            training_window_bars=756,
            force=False,
            no_official_news=False,
            no_llm_news=False,
            official_news_lookback_days=None,
            require_official_news=False,
            require_llm_news=False,
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
        runtime_config = _runtime_config()
        args = argparse.Namespace(
            command="automation-run",
            lookback_days=900,
            feed=None,
            research_snapshot=None,
            training_window_bars=756,
            force=False,
            no_official_news=False,
            no_llm_news=False,
            official_news_lookback_days=None,
            require_official_news=False,
            require_llm_news=False,
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

    def test_write_trade_rationale_report_persists_trade_history_entry(self) -> None:
        runtime_config = _runtime_config(
            profile_name="Aggressive",
            risk_profile="aggressive",
        )
        signal = Signal(
            as_of=date(2026, 5, 21),
            regime="risk_on",
            weights={"SPY": 0.24, "QQQ": 0.18, "NVDA": 0.11},
            diagnostics={
                "selected_count": 3,
                "selected_etf_count": 2,
                "selected_equity_count": 1,
            },
            assessments={
                "SPY": ResearchAssessment(
                    symbol="SPY",
                    total_score=0.62,
                    research_score=0.58,
                    component_scores={"quant": 0.55, "etf": 0.74, "index": 0.60},
                    asset_type="etf",
                    benchmark_index="SPX",
                ),
                "NVDA": ResearchAssessment(
                    symbol="NVDA",
                    total_score=0.78,
                    research_score=0.71,
                    component_scores={"quant": 0.81, "company": 0.69, "news": 0.66},
                    asset_type="equity",
                    sector="Technology",
                ),
            },
            official_news=OfficialNewsContext(
                as_of=date(2026, 5, 21),
                lookback_days=7,
                risk_on_score=0.55,
                cash_score=0.42,
                items=(
                    OfficialNewsItem(
                        source="fed",
                        published_on=date(2026, 5, 21),
                        title="Fed holds rates steady",
                        url="https://example.test/fed",
                        impact_scores={"risk_on": 0.55},
                        summary="Policy unchanged with balanced language.",
                    ),
                ),
                source_status={"fed": "ok"},
            ),
        )
        actions = [
            RebalanceAction(
                side="buy",
                symbol="SPY",
                notional=24000.0,
                qty=None,
                reason="increase_or_enter",
            ),
            RebalanceAction(
                side="buy",
                symbol="NVDA",
                notional=11000.0,
                qty=None,
                reason="increase_or_enter",
            ),
        ]
        result = ExecutionResult(
            actions=actions,
            responses=[{"id": "order-1"}],
            state=RuntimeState(high_water_mark=101500.0),
            submitted_actions=actions,
            skipped_messages=["Skipped QQQ because price drift exceeded limit."],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd_before = Path.cwd()
            try:
                os.chdir(root)
                report_path = _write_trade_rationale_report(
                    runtime_config=runtime_config,
                    params=StrategyParameters(top_n=4, equity_count=6),
                    signal=signal,
                    result=result,
                    submit=True,
                    clock_timestamp="2026-05-21T12:30:00-04:00",
                    execution_error=None,
                )
                resolved_report_path = (root / report_path).resolve()
            finally:
                os.chdir(cwd_before)

            self.assertTrue(resolved_report_path.exists())
            self.assertTrue(
                resolved_report_path.parent.as_posix().endswith("/logs/trade-rationales")
            )
            contents = resolved_report_path.read_text()
            self.assertIn("# Trade Rationale", contents)
            self.assertIn("- Profile: Aggressive", contents)
            self.assertIn("## Decision Summary", contents)
            self.assertIn("## Target Portfolio", contents)
            self.assertIn("## Official News Context", contents)
            self.assertIn("## Selected Research", contents)
            self.assertIn("## Submitted Basket", contents)
            self.assertIn("## Skipped Actions", contents)
            self.assertIn("SPY: 24.00%", contents)


if __name__ == "__main__":
    unittest.main()
