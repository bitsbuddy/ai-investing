from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import subprocess
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from .config import load_runtime_config
from .profiles import load_env_file, load_profile_matrix, write_env_file


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


@dataclass(frozen=True)
class ProfileControlStatus:
    key: str
    profile_name: str
    risk_profile: str
    env_path: Path
    state_path: Path
    status_path: Path
    log_path: Path
    has_api_key: bool
    has_secret_key: bool
    api_key_preview: str
    performance_baseline: str
    run_phase: str
    last_result: str | None
    last_message: str | None
    last_started_at: str | None
    last_finished_at: str | None
    last_exit_code: str | None
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
    entries: list[str] = []
    for line in cron_path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            entries.append(stripped)
    if not entries:
        return None
    return "\n".join(entries)


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


def load_profile_control_statuses(
    *,
    manifest_path: Path,
    profile_status_dir: Path,
    profile_log_dir: Path,
) -> tuple[ProfileControlStatus, ...]:
    if not manifest_path.exists():
        return ()

    statuses: list[ProfileControlStatus] = []
    for entry in load_profile_matrix(manifest_path):
        env_values = load_env_file(entry.env_file) if entry.env_file.exists() else {}
        runtime_config = load_runtime_config(env_values)
        status_path = profile_status_dir / f"{entry.name}.status"
        log_path = profile_log_dir / f"{entry.name}.log"
        state = read_automation_state(status_path)
        api_key = env_values.get("ALPACA_API_KEY", "")
        secret_key = env_values.get("ALPACA_SECRET_KEY", "")
        statuses.append(
            ProfileControlStatus(
                key=entry.name,
                profile_name=(env_values.get("AI_INVESTING_PROFILE_NAME") or runtime_config.profile_name),
                risk_profile=(
                    env_values.get("AI_INVESTING_RISK_PROFILE")
                    or runtime_config.risk_profile
                ),
                env_path=entry.env_file,
                state_path=runtime_config.state_path,
                status_path=status_path,
                log_path=log_path,
                has_api_key=bool(api_key),
                has_secret_key=bool(secret_key),
                api_key_preview=_mask_key(api_key),
                performance_baseline=env_values.get(
                    "AI_INVESTING_PERFORMANCE_BASELINE", ""
                ),
                run_phase=state.get("run_phase", "idle"),
                last_result=state.get("last_result"),
                last_message=state.get("last_message"),
                last_started_at=state.get("last_started_at"),
                last_finished_at=state.get("last_finished_at"),
                last_exit_code=state.get("last_exit_code"),
                recent_log_lines=read_recent_log_lines(log_path, limit=8),
            )
        )
    return tuple(statuses)


def save_profile_settings(
    *,
    env_path: Path,
    profile_name: str,
    risk_profile: str,
    alpaca_api_key: str,
    alpaca_secret_key: str,
    performance_baseline: str,
) -> None:
    values = load_env_file(env_path)
    values["AI_INVESTING_PROFILE_NAME"] = profile_name.strip() or risk_profile.title()
    values["AI_INVESTING_RISK_PROFILE"] = risk_profile.strip().lower() or "balanced"
    values["ALPACA_API_KEY"] = alpaca_api_key.strip()
    values["ALPACA_SECRET_KEY"] = alpaca_secret_key.strip()
    values["AI_INVESTING_PERFORMANCE_BASELINE"] = performance_baseline.strip()
    write_env_file(env_path, values)


def trigger_profile_manual_run(
    *,
    repo_root: Path,
    python_path: Path,
    env_path: Path,
    status_path: Path,
    log_path: Path,
    profile_name: str,
) -> tuple[bool, str]:
    if not env_path.exists():
        return False, "Profile env file is missing."
    current_state = read_automation_state(status_path)
    if current_state.get("run_phase") == "running":
        return False, f"{profile_name} is already running."

    now = _utc_now_label()
    write_automation_state(
        status_path,
        run_phase="queued",
        last_result="pending",
        last_message=f"{profile_name} run requested from UI",
        last_started_at=now,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    shell_command = _render_profile_run_shell_command(
        repo_root=repo_root,
        python_path=python_path,
        env_path=env_path,
        status_path=status_path,
        log_path=log_path,
        profile_name=profile_name,
    )
    subprocess.Popen(
        ["/bin/zsh", "-lc", shell_command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    return True, f"{profile_name} run started."


def serve_automation_ui(
    *,
    host: str,
    port: int,
    repo_root: Path,
    python_path: Path,
    control_path: Path,
    script_path: Path,
    env_path: Path,
    cron_path: Path,
    log_path: Path,
    state_path: Path,
    manifest_path: Path,
    profile_status_dir: Path,
    profile_log_dir: Path,
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
            profiles = load_profile_control_statuses(
                manifest_path=manifest_path,
                profile_status_dir=profile_status_dir,
                profile_log_dir=profile_log_dir,
            )
            body = _render_ui_html(
                status=status,
                profiles=profiles,
                control_path=control_path,
                script_path=script_path,
                env_path=env_path,
                cron_path=cron_path,
                log_path=log_path,
                state_path=state_path,
                manifest_path=manifest_path,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            fields = self._read_form_fields()
            if self.path == "/enable":
                write_automation_enabled(control_path, True)
            elif self.path == "/disable":
                write_automation_enabled(control_path, False)
            elif self.path == "/run-now":
                trigger_manual_run(script_path=script_path, state_path=state_path)
            elif self.path == "/profile-save":
                key = fields.get("profile_key", "")
                profile = _find_profile_status(
                    key=key,
                    manifest_path=manifest_path,
                    profile_status_dir=profile_status_dir,
                    profile_log_dir=profile_log_dir,
                )
                if profile is None:
                    self.send_error(404)
                    return
                save_profile_settings(
                    env_path=profile.env_path,
                    profile_name=fields.get("profile_name", ""),
                    risk_profile=fields.get("risk_profile", profile.risk_profile),
                    alpaca_api_key=fields.get("alpaca_api_key", ""),
                    alpaca_secret_key=fields.get("alpaca_secret_key", ""),
                    performance_baseline=fields.get("performance_baseline", ""),
                )
            elif self.path == "/profile-run":
                key = fields.get("profile_key", "")
                profile = _find_profile_status(
                    key=key,
                    manifest_path=manifest_path,
                    profile_status_dir=profile_status_dir,
                    profile_log_dir=profile_log_dir,
                )
                if profile is None:
                    self.send_error(404)
                    return
                trigger_profile_manual_run(
                    repo_root=repo_root,
                    python_path=python_path,
                    env_path=profile.env_path,
                    status_path=profile.status_path,
                    log_path=profile.log_path,
                    profile_name=profile.profile_name,
                )
            elif self.path == "/profiles-run-all":
                for profile in load_profile_control_statuses(
                    manifest_path=manifest_path,
                    profile_status_dir=profile_status_dir,
                    profile_log_dir=profile_log_dir,
                ):
                    trigger_profile_manual_run(
                        repo_root=repo_root,
                        python_path=python_path,
                        env_path=profile.env_path,
                        status_path=profile.status_path,
                        log_path=profile.log_path,
                        profile_name=profile.profile_name,
                    )
            else:
                self.send_error(404)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def _read_form_fields(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            parsed = parse_qs(raw, keep_blank_values=True)
            return {key: values[0] for key, values in parsed.items() if values}

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
    profiles: tuple[ProfileControlStatus, ...],
    control_path: Path,
    script_path: Path,
    env_path: Path,
    cron_path: Path,
    log_path: Path,
    state_path: Path,
    manifest_path: Path,
) -> str:
    state_label = "Enabled" if status.enabled else "Disabled"
    state_class = "enabled" if status.enabled else "disabled"
    summary = _status_summary(status)
    last_updated = _last_updated_label(status)
    run_now_disabled = 'disabled aria-disabled="true"' if status.run_phase == "running" else ""
    profile_section = _render_profile_controls_section(
        profiles=profiles,
        manifest_path=manifest_path,
    )
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
      max-width: 1240px;
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
    .stack-form {{
      display: grid;
      gap: 10px;
    }}
    .stack-form label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 0.96rem;
      font-family: inherit;
      background: white;
      color: var(--ink);
    }}
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
    .save {{
      background: #6f4e37;
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
    .profile-toolbar {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .profile-toolbar h2 {{
      margin: 0 0 8px;
      font-size: 1.4rem;
    }}
    .profile-summary-grid {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
    }}
    .summary-chip {{
      background: #faf6ee;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
    }}
    .summary-chip span {{
      display: block;
      color: var(--muted);
      font-size: 0.82rem;
      margin-bottom: 4px;
    }}
    .summary-chip strong {{
      font-size: 1.2rem;
    }}
    .profiles-grid {{
      margin-top: 22px;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 18px;
      align-items: start;
    }}
    .profile-card {{
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      display: grid;
      gap: 14px;
    }}
    .profile-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .profile-title-row {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .profile-title-row h3 {{
      margin: 0;
      font-size: 1.12rem;
    }}
    .profile-subtle {{
      margin-top: 6px;
      font-size: 0.88rem;
      color: var(--muted);
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 0.82rem;
      background: #e9dfcf;
      color: var(--ink);
    }}
    .status-pill {{
      display: inline-block;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 0.8rem;
      font-weight: 700;
      white-space: nowrap;
    }}
    .status-ready {{
      background: rgba(31, 122, 77, 0.12);
      color: var(--enabled);
    }}
    .status-running {{
      background: rgba(24, 78, 119, 0.12);
      color: var(--accent);
    }}
    .status-failed {{
      background: rgba(155, 44, 44, 0.12);
      color: var(--disabled);
    }}
    .status-setup {{
      background: rgba(111, 78, 55, 0.12);
      color: #6f4e37;
    }}
    .profile-kpis {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .kpi {{
      background: #faf6ee;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
    }}
    .kpi span {{
      display: block;
      color: var(--muted);
      font-size: 0.8rem;
      margin-bottom: 4px;
    }}
    .kpi strong {{
      font-size: 0.96rem;
      color: var(--ink);
      word-break: break-word;
    }}
    .profile-message {{
      color: var(--ink);
      min-height: 3em;
    }}
    .profile-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .accordion {{
      background: #faf6ee;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 0 14px 14px;
    }}
    .accordion summary {{
      list-style: none;
      cursor: pointer;
      padding: 14px 0;
      font-weight: 700;
      color: var(--ink);
    }}
    .accordion summary::-webkit-details-marker {{
      display: none;
    }}
    .accordion[open] summary {{
      margin-bottom: 8px;
    }}
    .accordion-body {{
      display: grid;
      gap: 12px;
    }}
    .mini-meta {{
      display: grid;
      gap: 0;
    }}
    .mini-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px solid rgba(216, 207, 190, 0.7);
    }}
    .mini-row:last-child {{
      border-bottom: 0;
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
    .compact-log {{
      max-height: 180px;
    }}
    @media (max-width: 1080px) {{
      .profiles-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .profile-summary-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 720px) {{
      .shell {{ margin: 20px auto; padding: 14px; }}
      .card {{ padding: 20px; border-radius: 18px; }}
      .panels {{ grid-template-columns: 1fr; }}
      .profile-summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .profiles-grid {{ grid-template-columns: 1fr; }}
      .profile-top {{ flex-direction: column; }}
      .profile-kpis {{ grid-template-columns: 1fr; }}
      .mini-row {{ flex-direction: column; }}
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
      {profile_section}
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


def _render_profile_controls_section(
    *,
    profiles: tuple[ProfileControlStatus, ...],
    manifest_path: Path,
) -> str:
    if not profiles:
        return (
            '<div class="panel" style="margin-top: 28px;">'
            "<h2>Profile Controls</h2>"
            f"<p>No profile matrix found at <code>{escape(str(manifest_path))}</code>. "
            "Create one with <code>multi-profile-setup</code> to manage multiple risk profiles here.</p>"
            "</div>"
        )

    running_profiles = sum(
        1 for profile in profiles if profile.run_phase in {"queued", "running"}
    )
    setup_profiles = sum(
        1 for profile in profiles if not (profile.has_api_key and profile.has_secret_key)
    )
    failed_profiles = sum(1 for profile in profiles if profile.last_result == "failed")
    ready_profiles = sum(
        1
        for profile in profiles
        if profile.has_api_key
        and profile.has_secret_key
        and profile.run_phase not in {"queued", "running"}
        and profile.last_result != "failed"
    )

    cards = []
    for profile in profiles:
        recent_logs = (
            "\n".join(escape(line) for line in profile.recent_log_lines)
            if profile.recent_log_lines
            else "No profile log output yet."
        )
        status_label, status_class = _profile_status_badge(profile)
        cards.append(
            f"""
      <div class="profile-card">
        <div class="profile-top">
          <div>
            <div class="profile-title-row">
              <h3>{escape(profile.profile_name)}</h3>
              <span class="badge">{escape(profile.risk_profile)}</span>
            </div>
            <div class="profile-subtle">{escape(str(profile.env_path))}</div>
          </div>
          <span class="status-pill {status_class}">{escape(status_label)}</span>
        </div>
        <div class="profile-kpis">
          <div class="kpi">
            <span>Run Phase</span>
            <strong>{escape(profile.run_phase)}</strong>
          </div>
          <div class="kpi">
            <span>Last Result</span>
            <strong>{escape(profile.last_result or "unknown")}</strong>
          </div>
          <div class="kpi">
            <span>Alpaca Key</span>
            <strong>{escape(profile.api_key_preview or "missing")}</strong>
          </div>
          <div class="kpi">
            <span>Baseline</span>
            <strong>{escape(profile.performance_baseline or "not set")}</strong>
          </div>
        </div>
        <p class="profile-message"><strong>Last message:</strong> {escape(profile.last_message or "none")}</p>
        <div class="profile-actions">
          <form method="post" action="/profile-run">
            <input type="hidden" name="profile_key" value="{escape(profile.key)}">
            <button class="run-now" type="submit" {'disabled aria-disabled="true"' if profile.run_phase == 'running' else ''}>Run {escape(profile.profile_name)}</button>
          </form>
        </div>
        <details class="accordion">
          <summary>Edit Settings</summary>
          <div class="accordion-body">
            <form class="stack-form" method="post" action="/profile-save">
              <input type="hidden" name="profile_key" value="{escape(profile.key)}">
              <label>Profile Name
                <input type="text" name="profile_name" value="{escape(profile.profile_name)}">
              </label>
              <label>Risk Profile
                <select name="risk_profile">
                  {_render_risk_profile_options(profile.risk_profile)}
                </select>
              </label>
              <label>Alpaca API Key
                <input type="text" name="alpaca_api_key" value="{escape(_read_env_value(profile.env_path, 'ALPACA_API_KEY'))}">
              </label>
              <label>Alpaca Secret Key
                <input type="password" name="alpaca_secret_key" value="{escape(_read_env_value(profile.env_path, 'ALPACA_SECRET_KEY'))}">
              </label>
              <label>Performance Baseline
                <input type="text" name="performance_baseline" value="{escape(profile.performance_baseline)}">
              </label>
              <div class="profile-actions">
                <button class="save" type="submit">Save Settings</button>
              </div>
            </form>
          </div>
        </details>
        <details class="accordion">
          <summary>Logs and Paths</summary>
          <div class="accordion-body">
            <div class="mini-meta">
              <div class="mini-row">
                <div class="label">Portfolio State</div>
                <code>{escape(str(profile.state_path))}</code>
              </div>
              <div class="mini-row">
                <div class="label">UI Run Status</div>
                <code>{escape(str(profile.status_path))}</code>
              </div>
              <div class="mini-row">
                <div class="label">Log File</div>
                <code>{escape(str(profile.log_path))}</code>
              </div>
              <div class="mini-row">
                <div class="label">Secret Key</div>
                <strong>{'present' if profile.has_secret_key else 'missing'}</strong>
              </div>
            </div>
            <div class="logbox compact-log">{recent_logs}</div>
          </div>
        </details>
      </div>
"""
        )

    return f"""
      <div class="panel" style="margin-top: 28px;">
        <div class="profile-toolbar">
          <div>
            <h2>Multi-Profile Controls</h2>
            <p>Compare profiles side by side, run them independently, and only open settings when you need them.</p>
            <p>Manifest: <code>{escape(str(manifest_path))}</code></p>
          </div>
          <div class="profile-actions">
            <form method="post" action="/profiles-run-all">
              <button class="run-now" type="submit">Run All Profiles</button>
            </form>
          </div>
        </div>
        <div class="profile-summary-grid">
          <div class="summary-chip">
            <span>Total Profiles</span>
            <strong>{len(profiles)}</strong>
          </div>
          <div class="summary-chip">
            <span>Ready</span>
            <strong>{ready_profiles}</strong>
          </div>
          <div class="summary-chip">
            <span>Running</span>
            <strong>{running_profiles}</strong>
          </div>
          <div class="summary-chip">
            <span>Need Setup</span>
            <strong>{setup_profiles}</strong>
          </div>
          <div class="summary-chip">
            <span>Failed Last Run</span>
            <strong>{failed_profiles}</strong>
          </div>
        </div>
        <div class="profiles-grid">
          {''.join(cards)}
        </div>
      </div>
"""


def _profile_status_badge(profile: ProfileControlStatus) -> tuple[str, str]:
    if profile.run_phase in {"queued", "running"}:
        return "running", "status-running"
    if not (profile.has_api_key and profile.has_secret_key):
        return "needs setup", "status-setup"
    if profile.last_result == "failed":
        return "failed", "status-failed"
    return "ready", "status-ready"


def _render_risk_profile_options(current_value: str) -> str:
    options = []
    for value in ("conservative", "balanced", "aggressive"):
        selected = " selected" if value == current_value else ""
        options.append(
            f'<option value="{value}"{selected}>{value.title()}</option>'
        )
    return "".join(options)


def _find_profile_status(
    *,
    key: str,
    manifest_path: Path,
    profile_status_dir: Path,
    profile_log_dir: Path,
) -> ProfileControlStatus | None:
    for profile in load_profile_control_statuses(
        manifest_path=manifest_path,
        profile_status_dir=profile_status_dir,
        profile_log_dir=profile_log_dir,
    ):
        if profile.key == key:
            return profile
    return None


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


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _read_env_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    return load_env_file(env_path).get(key, "")


def _render_profile_run_shell_command(
    *,
    repo_root: Path,
    python_path: Path,
    env_path: Path,
    status_path: Path,
    log_path: Path,
    profile_name: str,
) -> str:
    quoted_repo = shlex.quote(str(repo_root))
    quoted_python = shlex.quote(str(python_path))
    quoted_env = shlex.quote(str(env_path))
    quoted_status = shlex.quote(str(status_path))
    quoted_log = shlex.quote(str(log_path))
    quoted_profile = shlex.quote(profile_name)
    return f"""
set -euo pipefail
status_file={quoted_status}
log_file={quoted_log}
profile_name={quoted_profile}
write_status() {{
  cat > "$status_file" <<EOF
run_phase=$1
last_result=$2
last_message=$3
last_started_at=$4
last_finished_at=$5
last_exit_code=$6
EOF
}}
mkdir -p "$(dirname "$status_file")" "$(dirname "$log_file")"
run_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_status running pending "$profile_name run starting" "$run_started_at" "" ""
exec >> "$log_file" 2>&1
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) profile run start: $profile_name ==="
cd {quoted_repo}
set -a
source {quoted_env}
set +a
if PYTHONPATH=src {quoted_python} -m ai_investing.cli automation-run --manual; then
  run_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_status idle success "$profile_name run completed successfully" "$run_started_at" "$run_finished_at" "0"
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) profile run end: $profile_name ==="
else
  code=$?
  run_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_status failed failed "$profile_name run failed" "$run_started_at" "$run_finished_at" "$code"
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) profile run failed: $profile_name (exit $code) ==="
  exit $code
fi
"""
