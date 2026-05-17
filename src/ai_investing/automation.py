from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class AutomationStatus:
    enabled: bool
    control_file_exists: bool
    script_exists: bool
    env_exists: bool
    cron_exists: bool
    log_exists: bool
    state_path_exists: bool
    run_phase: str
    last_result: str | None
    last_message: str | None
    last_started_at: str | None
    last_finished_at: str | None
    last_exit_code: str | None
    schedule: str | None
    recent_log_lines: tuple[str, ...]


def read_automation_enabled(control_path: Path) -> bool:
    if not control_path.exists():
        return False
    contents = control_path.read_text().strip().lower()
    return "enabled=1" in contents


def write_automation_enabled(control_path: Path, enabled: bool) -> None:
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text("enabled=1\n" if enabled else "enabled=0\n")


def read_automation_state(state_path: Path) -> dict[str, str]:
    if not state_path.exists():
        return {}
    fields: dict[str, str] = {}
    for line in state_path.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def write_automation_state(
    state_path: Path,
    *,
    run_phase: str,
    last_result: str,
    last_message: str,
    last_started_at: str = "",
    last_finished_at: str = "",
    last_exit_code: str = "",
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        "\n".join(
            [
                f"run_phase={run_phase}",
                f"last_result={last_result}",
                f"last_message={last_message}",
                f"last_started_at={last_started_at}",
                f"last_finished_at={last_finished_at}",
                f"last_exit_code={last_exit_code}",
            ]
        )
        + "\n"
    )


def trigger_manual_run(*, script_path: Path, state_path: Path) -> tuple[bool, str]:
    if not script_path.exists():
        return False, "Runner script is missing."

    current_state = read_automation_state(state_path)
    if current_state.get("run_phase") == "running":
        return False, "Automation is already running."

    now = _utc_now_label()
    write_automation_state(
        state_path,
        run_phase="queued",
        last_result="pending",
        last_message="Manual run requested from UI",
        last_started_at=now,
    )
    env = os.environ.copy()
    env["AI_INVESTING_FORCE_RUN"] = "1"
    subprocess.Popen(
        ["/bin/zsh", str(script_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    return True, "Manual run started."


def read_cron_schedule(cron_path: Path) -> str | None:
    if not cron_path.exists():
        return None
    for line in cron_path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def read_recent_log_lines(log_path: Path, *, limit: int = 12) -> tuple[str, ...]:
    if not log_path.exists():
        return ()
    return tuple(log_path.read_text().splitlines()[-limit:])


def load_automation_status(
    *,
    control_path: Path,
    script_path: Path,
    env_path: Path,
    cron_path: Path,
    log_path: Path,
    state_path: Path,
) -> AutomationStatus:
    state = read_automation_state(state_path)
    return AutomationStatus(
        enabled=read_automation_enabled(control_path),
        control_file_exists=control_path.exists(),
        script_exists=script_path.exists(),
        env_exists=env_path.exists(),
        cron_exists=cron_path.exists(),
        log_exists=log_path.exists(),
        state_path_exists=state_path.exists(),
        run_phase=state.get("run_phase", "idle"),
        last_result=state.get("last_result"),
        last_message=state.get("last_message"),
        last_started_at=state.get("last_started_at"),
        last_finished_at=state.get("last_finished_at"),
        last_exit_code=state.get("last_exit_code"),
        schedule=read_cron_schedule(cron_path),
        recent_log_lines=read_recent_log_lines(log_path),
    )


def serve_automation_ui(
    *,
    host: str,
    port: int,
    control_path: Path,
    script_path: Path,
    env_path: Path,
    cron_path: Path,
    log_path: Path,
    state_path: Path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/":
                self.send_error(404)
                return
            status = load_automation_status(
                control_path=control_path,
                script_path=script_path,
                env_path=env_path,
                cron_path=cron_path,
                log_path=log_path,
                state_path=state_path,
            )
            body = _render_ui_html(
                status=status,
                control_path=control_path,
                script_path=script_path,
                env_path=env_path,
                cron_path=cron_path,
                log_path=log_path,
                state_path=state_path,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/enable":
                write_automation_enabled(control_path, True)
            elif self.path == "/disable":
                write_automation_enabled(control_path, False)
            elif self.path == "/run-now":
                trigger_manual_run(script_path=script_path, state_path=state_path)
            else:
                self.send_error(404)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _render_ui_html(
    *,
    status: AutomationStatus,
    control_path: Path,
    script_path: Path,
    env_path: Path,
    cron_path: Path,
    log_path: Path,
    state_path: Path,
) -> str:
    state_label = "Enabled" if status.enabled else "Disabled"
    state_class = "enabled" if status.enabled else "disabled"
    summary = _status_summary(status)
    last_updated = _last_updated_label(status)
    run_now_disabled = 'disabled aria-disabled="true"' if status.run_phase == "running" else ""
    recent_logs = (
        "\n".join(escape(line) for line in status.recent_log_lines)
        if status.recent_log_lines
        else "No automation log output yet."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Investing Automation</title>
  <meta http-equiv="refresh" content="15">
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: #fffdf7;
      --ink: #1f2a2c;
      --muted: #5c6b70;
      --line: #d8cfbe;
      --enabled: #1f7a4d;
      --disabled: #9b2c2c;
      --accent: #184e77;
      --soft: #f3ede0;
      --shadow: 0 20px 50px rgba(31, 42, 44, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(24, 78, 119, 0.08), transparent 30%),
        linear-gradient(160deg, #f7f2e7 0%, #ebe2d0 100%);
      min-height: 100vh;
    }}
    .shell {{
      max-width: 900px;
      margin: 48px auto;
      padding: 24px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 28px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 2.2rem;
      line-height: 1.1;
    }}
    p {{
      margin: 0;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.5;
    }}
    .status {{
      margin-top: 24px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 16px 18px;
      border-radius: 18px;
      background: #f8f4ea;
      border: 1px solid var(--line);
    }}
    .dot {{
      width: 14px;
      height: 14px;
      border-radius: 999px;
      background: var(--disabled);
      box-shadow: 0 0 0 6px rgba(155, 44, 44, 0.08);
    }}
    .status.enabled .dot {{
      background: var(--enabled);
      box-shadow: 0 0 0 6px rgba(31, 122, 77, 0.08);
    }}
    .status strong {{
      font-size: 1.1rem;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      margin-top: 24px;
      flex-wrap: wrap;
    }}
    form {{ margin: 0; }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font-size: 0.98rem;
      cursor: pointer;
      font-family: inherit;
    }}
    .start {{
      background: var(--enabled);
      color: white;
    }}
    .stop {{
      background: var(--disabled);
      color: white;
    }}
    .run-now {{
      background: var(--accent);
      color: white;
    }}
    button[disabled] {{
      cursor: not-allowed;
      opacity: 0.55;
    }}
    .meta {{
      margin-top: 28px;
      display: grid;
      gap: 12px;
    }}
    .panels {{
      margin-top: 28px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}
    .panel {{
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
    }}
    .panel h2 {{
      margin: 0 0 8px;
      font-size: 1.1rem;
    }}
    .panel p {{
      color: var(--ink);
    }}
    .logbox {{
      margin-top: 10px;
      padding: 14px;
      border-radius: 14px;
      background: #faf6ee;
      border: 1px solid var(--line);
      font-family: "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 0.86rem;
      line-height: 1.5;
      color: var(--ink);
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 320px;
      overflow: auto;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 0;
      border-bottom: 1px solid rgba(216, 207, 190, 0.7);
    }}
    .row:last-child {{ border-bottom: 0; }}
    .label {{
      color: var(--muted);
      min-width: 180px;
    }}
    code {{
      font-size: 0.94rem;
      color: var(--accent);
      word-break: break-all;
    }}
    @media (max-width: 720px) {{
      .shell {{ margin: 20px auto; padding: 14px; }}
      .card {{ padding: 20px; border-radius: 18px; }}
      .panels {{ grid-template-columns: 1fr; }}
      .row {{ flex-direction: column; }}
      .label {{ min-width: 0; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <h1>Automation Control</h1>
      <p>Start or stop the scheduled paper-trading automation without editing cron files by hand.</p>

      <div class="status {state_class}">
        <div class="dot"></div>
        <div>
          <strong>{state_label}</strong>
          <p>The scheduled runner will {"place paper trades" if status.enabled else "skip trading until re-enabled"}.</p>
        </div>
      </div>

      <div class="panels">
        <div class="panel">
          <h2>High-Level Progress</h2>
          <p><strong>{escape(summary)}</strong></p>
          <p>Last updated: {escape(last_updated)}</p>
        </div>
        <div class="panel">
          <h2>Runner State</h2>
          <p>Phase: <strong>{escape(status.run_phase)}</strong></p>
          <p>Last result: <strong>{escape(status.last_result or "unknown")}</strong></p>
          <p>Last message: <strong>{escape(status.last_message or "none")}</strong></p>
        </div>
      </div>

      <div class="actions">
        <form method="post" action="/enable">
          <button class="start" type="submit">Start Automation</button>
        </form>
        <form method="post" action="/disable">
          <button class="stop" type="submit">Stop Automation</button>
        </form>
        <form method="post" action="/run-now">
          <button class="run-now" type="submit" {run_now_disabled}>Run Now</button>
        </form>
      </div>

      <div class="meta">
        <div class="row">
          <div class="label">Control File</div>
          <code>{escape(str(control_path))}</code>
        </div>
        <div class="row">
          <div class="label">Runner Script</div>
          <code>{escape(str(script_path))}</code>
        </div>
        <div class="row">
          <div class="label">Env File</div>
          <code>{escape(str(env_path))}</code>
        </div>
        <div class="row">
          <div class="label">Cron File</div>
          <code>{escape(str(cron_path))}</code>
        </div>
        <div class="row">
          <div class="label">Status File</div>
          <code>{escape(str(state_path))}</code>
        </div>
        <div class="row">
          <div class="label">Log File</div>
          <code>{escape(str(log_path))}</code>
        </div>
        <div class="row">
          <div class="label">Cron Schedule</div>
          <code>{escape(status.schedule or "not installed in template")}</code>
        </div>
        <div class="row">
          <div class="label">Control File Exists</div>
          <strong>{'yes' if status.control_file_exists else 'no'}</strong>
        </div>
        <div class="row">
          <div class="label">Runner Script Exists</div>
          <strong>{'yes' if status.script_exists else 'no'}</strong>
        </div>
        <div class="row">
          <div class="label">Env File Exists</div>
          <strong>{'yes' if status.env_exists else 'no'}</strong>
        </div>
        <div class="row">
          <div class="label">Status File Exists</div>
          <strong>{'yes' if status.state_path_exists else 'no'}</strong>
        </div>
        <div class="row">
          <div class="label">Log File Exists</div>
          <strong>{'yes' if status.log_exists else 'no'}</strong>
        </div>
      </div>

      <div class="panel" style="margin-top: 28px;">
        <h2>Recent Log Output</h2>
        <div class="logbox">{recent_logs}</div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def _status_summary(status: AutomationStatus) -> str:
    if status.run_phase == "queued":
        return "Manual run requested and waiting to start."
    if status.run_phase == "running":
        return "Automation is running now."
    if status.last_result == "pending":
        return "Automation run has been triggered and is starting."
    if status.last_result == "success":
        return "Last scheduled run completed successfully."
    if status.last_result == "failed":
        return "Last scheduled run failed."
    if status.last_result == "skipped":
        return "Last scheduled run was skipped."
    if status.enabled:
        return "Automation is enabled and waiting for the next scheduled run."
    return "Automation is disabled."


def _last_updated_label(status: AutomationStatus) -> str:
    candidate = status.last_finished_at or status.last_started_at
    if candidate is None:
        return "never"
    try:
        moment = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return candidate
    return moment.astimezone(ZoneInfo("America/Toronto")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def _utc_now_label() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
