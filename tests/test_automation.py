from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_investing.automation import (
    load_automation_status,
    trigger_manual_run,
    write_automation_enabled,
)


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


if __name__ == "__main__":
    unittest.main()
