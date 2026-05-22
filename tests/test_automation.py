from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_investing.automation import (
    load_automation_status,
    load_profile_control_statuses,
    load_trade_rationale_reports,
    read_trade_rationale_report,
    save_profile_settings,
    trigger_profile_manual_run,
    trigger_manual_run,
    write_automation_enabled,
)
from ai_investing.profiles import write_profile_matrix, ProfileMatrixEntry


class AutomationTests(unittest.TestCase):
    def test_load_automation_status_includes_schedule_state_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_path = root / "paper_trade.enabled"
            script_path = root / "run_paper_trade.sh"
            env_path = root / ".env.paper"
            cron_path = root / "paper_trade.cron"
            log_path = root / "paper-trade.log"
            state_path = root / "paper_trade.status"

            write_automation_enabled(control_path, True)
            script_path.write_text("#!/bin/zsh\n")
            env_path.write_text("ALPACA_PAPER=true\n")
            cron_path.write_text("40 9 * * 1-5 /bin/zsh run_paper_trade.sh\n")
            log_path.write_text("line 1\nline 2\n")
            state_path.write_text(
                "run_phase=idle\n"
                "last_result=success\n"
                "last_message=Scheduled run completed successfully\n"
                "last_started_at=2026-05-17T13:40:00Z\n"
                "last_finished_at=2026-05-17T13:41:00Z\n"
                "last_exit_code=0\n"
            )

            status = load_automation_status(
                control_path=control_path,
                script_path=script_path,
                env_path=env_path,
                cron_path=cron_path,
                log_path=log_path,
                state_path=state_path,
            )

            self.assertTrue(status.enabled)
            self.assertEqual(status.run_phase, "idle")
            self.assertEqual(status.last_result, "success")
            self.assertEqual(status.schedule, "40 9 * * 1-5 /bin/zsh run_paper_trade.sh")
            self.assertEqual(status.recent_log_lines, ("line 1", "line 2"))

    def test_trigger_manual_run_queues_and_spawns_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script_path = root / "run_paper_trade.sh"
            state_path = root / "paper_trade.status"
            script_path.write_text("#!/bin/zsh\n")
            state_path.write_text(
                "run_phase=idle\n"
                "last_result=never\n"
                "last_message=Waiting for first scheduled run\n"
            )

            with patch("ai_investing.automation.subprocess.Popen") as popen:
                ok, message = trigger_manual_run(
                    script_path=script_path,
                    state_path=state_path,
                )

            self.assertTrue(ok)
            self.assertEqual(message, "Manual run started.")
            popen.assert_called_once()
            state_contents = state_path.read_text()
            self.assertIn("run_phase=queued", state_contents)
            self.assertIn("last_result=pending", state_contents)
            self.assertIn("last_message=Manual run requested from UI", state_contents)

    def test_load_profile_control_statuses_reads_manifest_and_env_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_dir = root / "profiles"
            manifest_path = profile_dir / "profile_matrix.json"
            env_path = profile_dir / "balanced.paper.env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(
                "AI_INVESTING_PROFILE_NAME=Balanced\n"
                "AI_INVESTING_RISK_PROFILE=balanced\n"
                "ALPACA_API_KEY=testkey1234\n"
                "ALPACA_SECRET_KEY=testsecret5678\n"
                "AI_INVESTING_STATE_PATH=.ai_investing_balanced_state.json\n"
                "AI_INVESTING_PERFORMANCE_BASELINE=100000\n"
            )
            write_profile_matrix(
                manifest_path,
                [ProfileMatrixEntry(name="balanced", env_file=env_path)],
            )
            status_dir = root / "automation" / "profiles"
            log_dir = root / "logs" / "profiles"
            status_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "balanced.status").write_text(
                "run_phase=idle\nlast_result=success\nlast_message=done\n"
            )
            (log_dir / "balanced.log").write_text("line 1\nline 2\n")

            statuses = load_profile_control_statuses(
                manifest_path=manifest_path,
                profile_status_dir=status_dir,
                profile_log_dir=log_dir,
            )

            self.assertEqual(len(statuses), 1)
            self.assertEqual(statuses[0].profile_name, "Balanced")
            self.assertEqual(statuses[0].risk_profile, "balanced")
            self.assertTrue(statuses[0].has_api_key)
            self.assertTrue(statuses[0].has_secret_key)
            self.assertEqual(statuses[0].recent_log_lines, ("line 1", "line 2"))

    def test_save_profile_settings_updates_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / "balanced.paper.env"
            env_path.write_text(
                "AI_INVESTING_PROFILE_NAME=Balanced\n"
                "AI_INVESTING_RISK_PROFILE=balanced\n"
                "ALPACA_API_KEY=oldkey\n"
                "ALPACA_SECRET_KEY=oldsecret\n"
                "AI_INVESTING_PERFORMANCE_BASELINE=100000\n"
            )

            save_profile_settings(
                env_path=env_path,
                profile_name="Aggressive Lab",
                risk_profile="aggressive",
                alpaca_api_key="newkey",
                alpaca_secret_key="newsecret",
                performance_baseline="125000",
            )

            contents = env_path.read_text()
            self.assertIn("AI_INVESTING_PROFILE_NAME=\"Aggressive Lab\"", contents)
            self.assertIn("AI_INVESTING_RISK_PROFILE=aggressive", contents)
            self.assertIn("ALPACA_API_KEY=newkey", contents)
            self.assertIn("ALPACA_SECRET_KEY=newsecret", contents)
            self.assertIn("AI_INVESTING_PERFORMANCE_BASELINE=125000", contents)

    def test_trigger_profile_manual_run_queues_and_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / "balanced.paper.env"
            env_path.write_text("ALPACA_API_KEY=test\nALPACA_SECRET_KEY=test\n")
            status_path = root / "balanced.status"
            log_path = root / "balanced.log"

            with patch("ai_investing.automation.subprocess.Popen") as popen:
                ok, message = trigger_profile_manual_run(
                    repo_root=root,
                    python_path=Path("/usr/bin/python3"),
                    env_path=env_path,
                    status_path=status_path,
                    log_path=log_path,
                    profile_name="Balanced",
                )

            self.assertTrue(ok)
            self.assertEqual(message, "Balanced run started.")
            popen.assert_called_once()
            self.assertIn("run_phase=queued", status_path.read_text())

    def test_load_trade_rationale_reports_parses_and_sorts_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "logs" / "trade-rationales"
            reports_dir.mkdir(parents=True, exist_ok=True)
            older = reports_dir / "20260521T100000Z-balanced-preview.md"
            newer = reports_dir / "20260521T110000Z-aggressive-submit.md"
            older.write_text(
                "# Trade Rationale\n\n"
                "- Generated At: 2026-05-21T10:00:00Z\n"
                "- Profile: Balanced\n"
                "- Risk Profile: balanced\n"
                "- Run Mode: preview\n"
                "- Run Status: preview\n"
                "- Signal As Of: 2026-05-20\n"
            )
            newer.write_text(
                "# Trade Rationale\n\n"
                "- Generated At: 2026-05-21T11:00:00Z\n"
                "- Profile: Aggressive\n"
                "- Risk Profile: aggressive\n"
                "- Run Mode: submit\n"
                "- Run Status: submitted\n"
                "- Signal As Of: 2026-05-21\n"
                "\n## Target Portfolio\n- SPY: 20.00%\n"
            )

            reports = load_trade_rationale_reports(reports_dir)

            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[0].filename, newer.name)
            self.assertEqual(reports[0].profile_name, "Aggressive")
            self.assertEqual(reports[1].profile_name, "Balanced")
            self.assertIn("## Target Portfolio", read_trade_rationale_report(reports[0].path))


if __name__ == "__main__":
    unittest.main()
