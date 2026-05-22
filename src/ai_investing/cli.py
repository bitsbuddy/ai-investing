from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .automation import serve_automation_ui, write_automation_enabled
from .alpaca import AlpacaClient
from .backtest import (
    run_backtest,
    run_walk_forward_backtest,
    select_walk_forward_parameters,
)
from .config import (
    load_broker_config,
    load_runtime_config,
    require_broker_credentials,
)
from .execution import ExecutionResult, RuntimeState, execute_rebalance, save_state
from .models import (
    OfficialNewsContext,
    RebalanceAction,
    ResearchSnapshot,
    ResearchWeights,
    StrategyParameters,
)
from .news import build_official_news_context, summarize_official_news
from .profiles import (
    default_profile_matrix_entries,
    load_env_file,
    load_profile_matrix,
    strategy_parameters_for_risk_profile,
    write_profile_matrix,
)
from .research import ResearchOverlay, load_research_snapshot
from .strategy import ETFMomentumStrategy, align_history


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Investing CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    paper_setup = subparsers.add_parser(
        "paper-setup",
        help="Create a paper-trading env file and print the next commands to run.",
    )
    paper_setup.add_argument("--env-file", default=".env.paper")
    paper_setup.add_argument("--research-snapshot", default=None)
    paper_setup.add_argument("--sec-user-agent", default=None)
    paper_setup.add_argument("--force", action="store_true")

    multi_profile_setup = subparsers.add_parser(
        "multi-profile-setup",
        help="Create conservative, balanced, and aggressive paper-trading env files plus a matrix manifest.",
    )
    multi_profile_setup.add_argument("--directory", default="profiles")
    multi_profile_setup.add_argument("--manifest", default="profiles/profile_matrix.json")
    multi_profile_setup.add_argument("--research-snapshot", default=None)
    multi_profile_setup.add_argument("--sec-user-agent", default=None)
    multi_profile_setup.add_argument("--force", action="store_true")

    automation_setup = subparsers.add_parser(
        "automation-setup",
        help="Generate a daily runner script and cron template for unattended trading.",
    )
    automation_setup.add_argument("--env-file", default=".env.paper")
    automation_setup.add_argument("--script-file", default="scripts/run_paper_trade.sh")
    automation_setup.add_argument("--cron-file", default="automation/paper_trade.cron")
    automation_setup.add_argument("--status-file", default="automation/paper_trade.status")
    automation_setup.add_argument("--manifest", default="profiles/profile_matrix.json")
    automation_setup.add_argument(
        "--times",
        default="09:40,12:30,15:45",
        help="Comma-separated local-time schedule in HH:MM format, weekdays only.",
    )
    automation_setup.add_argument("--hour", type=int, default=9)
    automation_setup.add_argument("--minute", type=int, default=40)
    automation_setup.add_argument("--preview-only", action="store_true")
    automation_setup.add_argument("--force", action="store_true")

    automation_ui = subparsers.add_parser(
        "automation-ui",
        help="Run a local web UI to start and stop the scheduled automation.",
    )
    automation_ui.add_argument("--host", default="127.0.0.1")
    automation_ui.add_argument("--port", type=int, default=8787)
    automation_ui.add_argument("--env-file", default=".env.paper")
    automation_ui.add_argument(
        "--script-file", default="scripts/run_paper_trade.sh"
    )
    automation_ui.add_argument(
        "--control-file", default="automation/paper_trade.enabled"
    )
    automation_ui.add_argument(
        "--cron-file", default="automation/paper_trade.cron"
    )
    automation_ui.add_argument(
        "--log-file", default="logs/paper-trade.log"
    )
    automation_ui.add_argument(
        "--status-file", default="automation/paper_trade.status"
    )
    automation_ui.add_argument(
        "--manifest", default="profiles/profile_matrix.json"
    )

    automation_run = subparsers.add_parser(
        "automation-run",
        help="Execute the scheduled automation flow with market-hours aware behavior.",
    )
    automation_run.add_argument("--lookback-days", type=int, default=900)
    automation_run.add_argument("--feed", default=None)
    automation_run.add_argument("--research-snapshot", default=None)
    automation_run.add_argument("--training-window-bars", type=int, default=756)
    automation_run.add_argument("--force", action="store_true")
    automation_run.add_argument("--no-official-news", action="store_true")
    automation_run.add_argument("--no-llm-news", action="store_true")
    automation_run.add_argument("--official-news-lookback-days", type=int, default=None)
    automation_run.add_argument("--require-official-news", action="store_true")
    automation_run.add_argument("--require-llm-news", action="store_true")
    automation_run.add_argument("--manual", action="store_true")
    automation_run.add_argument("--preview-only", action="store_true")

    multi_profile_run = subparsers.add_parser(
        "multi-profile-run",
        help="Run preview or submission across multiple profile env files.",
    )
    multi_profile_run.add_argument("--manifest", default="profiles/profile_matrix.json")
    multi_profile_run.add_argument("--lookback-days", type=int, default=900)
    multi_profile_run.add_argument("--feed", default=None)
    multi_profile_run.add_argument("--training-window-bars", type=int, default=756)
    multi_profile_run.add_argument("--submit", action="store_true")
    multi_profile_run.add_argument("--force", action="store_true")
    multi_profile_run.add_argument("--no-official-news", action="store_true")
    multi_profile_run.add_argument("--no-llm-news", action="store_true")
    multi_profile_run.add_argument("--official-news-lookback-days", type=int, default=None)
    multi_profile_run.add_argument("--require-official-news", action="store_true")
    multi_profile_run.add_argument("--require-llm-news", action="store_true")
    multi_profile_run.add_argument("--manual", action="store_true")
    multi_profile_run.add_argument("--preview-only", action="store_true")
    multi_profile_run.add_argument("--allow-failures", action="store_true")

    multi_profile_report = subparsers.add_parser(
        "multi-profile-report",
        help="Compare current paper-account performance across multiple profiles.",
    )
    multi_profile_report.add_argument("--manifest", default="profiles/profile_matrix.json")

    backtest = subparsers.add_parser("backtest", help="Run a historical backtest.")
    backtest.add_argument("--start", type=_parse_date, required=True)
    backtest.add_argument("--end", type=_parse_date, default=_today_utc())
    backtest.add_argument("--feed", default=None)
    backtest.add_argument("--no-optimize", action="store_true")
    backtest.add_argument("--training-window-bars", type=int, default=756)

    signal = subparsers.add_parser("signal", help="Generate current target weights.")
    signal.add_argument("--lookback-days", type=int, default=900)
    signal.add_argument("--feed", default=None)
    signal.add_argument("--research-snapshot", default=None)
    signal.add_argument("--training-window-bars", type=int, default=756)
    signal.add_argument("--no-official-news", action="store_true")
    signal.add_argument("--no-llm-news", action="store_true")
    signal.add_argument("--official-news-lookback-days", type=int, default=None)
    signal.add_argument("--require-official-news", action="store_true")
    signal.add_argument("--require-llm-news", action="store_true")

    trade = subparsers.add_parser("trade", help="Preview or submit a rebalance.")
    trade.add_argument("--lookback-days", type=int, default=900)
    trade.add_argument("--feed", default=None)
    trade.add_argument("--research-snapshot", default=None)
    trade.add_argument("--training-window-bars", type=int, default=756)
    trade.add_argument("--submit", action="store_true")
    trade.add_argument("--force", action="store_true")
    trade.add_argument("--no-official-news", action="store_true")
    trade.add_argument("--no-llm-news", action="store_true")
    trade.add_argument("--official-news-lookback-days", type=int, default=None)
    trade.add_argument("--require-official-news", action="store_true")
    trade.add_argument("--require-llm-news", action="store_true")

    reset_account = subparsers.add_parser(
        "reset-account",
        help="Cancel open orders, liquidate positions, and clear local trading state.",
    )
    reset_account.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep the local state file instead of clearing it after liquidation.",
    )
    reset_account.add_argument(
        "--allow-live",
        action="store_true",
        help="Allow resetting a live account. Paper accounts do not need this flag.",
    )

    research = subparsers.add_parser(
        "research", help="Run multi-layer company/index/ETF analysis."
    )
    research.add_argument("--lookback-days", type=int, default=900)
    research.add_argument("--feed", default=None)
    research.add_argument("--research-snapshot", default=None)
    research.add_argument("--training-window-bars", type=int, default=756)
    research.add_argument("--no-official-news", action="store_true")
    research.add_argument("--no-llm-news", action="store_true")
    research.add_argument("--official-news-lookback-days", type=int, default=None)
    research.add_argument("--require-official-news", action="store_true")
    research.add_argument("--require-llm-news", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    broker_config = load_broker_config()
    runtime_config = load_runtime_config()

    if args.command == "paper-setup":
        _run_paper_setup_command(broker_config, runtime_config, args)
        return
    if args.command == "multi-profile-setup":
        _run_multi_profile_setup_command(broker_config, runtime_config, args)
        return
    if args.command == "automation-setup":
        _run_automation_setup_command(runtime_config, args)
        return
    if args.command == "automation-ui":
        _run_automation_ui_command(args)
        return
    if args.command == "multi-profile-run":
        _run_multi_profile_run_command(args)
        return
    if args.command == "multi-profile-report":
        _run_multi_profile_report_command(args)
        return

    if args.command == "research":
        research_snapshot_path = _resolve_research_snapshot_path(runtime_config, args)
        if _has_broker_credentials(broker_config):
            client = AlpacaClient(broker_config)
            _run_research_command(
                client, runtime_config, args, research_snapshot_path=research_snapshot_path
            )
            return
        _run_snapshot_only_research(runtime_config, research_snapshot_path, args)
        return

    require_broker_credentials(broker_config)
    client = AlpacaClient(broker_config)

    if args.command == "backtest":
        _run_backtest_command(client, runtime_config, args)
        return
    if args.command == "signal":
        _run_signal_command(client, runtime_config, args)
        return
    if args.command == "trade":
        _run_trade_command(client, broker_config.paper, runtime_config, args)
        return
    if args.command == "reset-account":
        _run_reset_account_command(client, broker_config.paper, runtime_config, args)
        return
    if args.command == "automation-run":
        _run_automation_trade_command(client, broker_config.paper, runtime_config, args)
        return
    raise ValueError(f"Unsupported command: {args.command}")


def _run_backtest_command(
    client: AlpacaClient, runtime_config, args: argparse.Namespace
) -> None:
    history = _load_history(
        client=client,
        symbols=list(
            dict.fromkeys(
                list(runtime_config.risk_on_universe)
                + list(runtime_config.equity_universe)
                + list(runtime_config.defensive_universe)
            )
        ),
        start=args.start,
        end=args.end,
        feed=args.feed or runtime_config.default_feed,
    )

    base_params = strategy_parameters_for_risk_profile(runtime_config.risk_profile)
    if args.no_optimize:
        strategy = ETFMomentumStrategy(
            risk_on_universe=runtime_config.risk_on_universe,
            equity_universe=runtime_config.equity_universe,
            defensive_universe=runtime_config.defensive_universe,
            params=base_params,
        )
        result = run_backtest(history, strategy)
        print("Selection mode: fixed parameters")
    else:
        result = run_walk_forward_backtest(
            history,
            risk_on_universe=runtime_config.risk_on_universe,
            equity_universe=runtime_config.equity_universe,
            defensive_universe=runtime_config.defensive_universe,
            base_params=base_params,
            training_window=args.training_window_bars,
        )
        print("Selection mode: walk-forward optimization")
    print("")

    print("Final Active Parameters")
    print(result.params)
    print("")
    print("Performance")
    print(f"Total return: {result.total_return:.2%}")
    print(f"CAGR: {result.cagr:.2%}")
    print(f"Annualized volatility: {result.annualized_volatility:.2%}")
    print(f"Sharpe: {result.sharpe:.2f}")
    print(f"Max drawdown: {result.max_drawdown:.2%}")
    print(f"Average turnover: {result.average_turnover:.2%}")
    print(f"Score: {result.score:.4f}")


def _run_paper_setup_command(
    broker_config,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    env_path = Path(args.env_file)
    if env_path.exists() and not args.force:
        raise FileExistsError(
            f"{env_path} already exists. Re-run with --force to overwrite it."
        )

    research_snapshot = _resolve_paper_setup_snapshot_path(runtime_config, args)
    sec_user_agent = _resolve_paper_setup_sec_user_agent(runtime_config, args)
    env_payload = _build_paper_env_payload(
        broker_config=broker_config,
        runtime_config=runtime_config,
        research_snapshot_path=research_snapshot,
        sec_user_agent=sec_user_agent,
    )
    env_path.write_text(_render_env_file(env_payload))

    print(f"Wrote paper config: {env_path}")
    print("")
    print("Paper Trading Settings")
    print("- ALPACA_PAPER=true")
    print("- AI_INVESTING_ENABLE_LIVE=0")
    print(f"- state file: {env_payload['AI_INVESTING_STATE_PATH']}")
    print(
        f"- credentials: {'present' if _has_broker_credentials(broker_config) else 'missing placeholders'}"
    )
    print("")
    print("Next Commands")
    print(f"- set -a; source {env_path}; set +a")
    if research_snapshot is not None:
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli research "
            f"--research-snapshot {research_snapshot}"
        )
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli signal "
            f"--research-snapshot {research_snapshot}"
        )
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli trade "
            f"--research-snapshot {research_snapshot}"
        )
        print(
            "- PYTHONPATH=src python3 -m ai_investing.cli trade --submit "
            f"--research-snapshot {research_snapshot}"
        )
    else:
        print("- PYTHONPATH=src python3 -m ai_investing.cli signal")
        print("- PYTHONPATH=src python3 -m ai_investing.cli trade")
        print("- PYTHONPATH=src python3 -m ai_investing.cli trade --submit")

    if not _has_broker_credentials(broker_config):
        print("")
        print(
            "Replace ALPACA_API_KEY and ALPACA_SECRET_KEY in the env file with your Alpaca paper credentials before submitting orders."
        )


def _run_multi_profile_setup_command(
    broker_config,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    profile_dir = Path(args.directory)
    manifest_path = Path(args.manifest)
    entries = default_profile_matrix_entries(profile_dir)
    for path in [manifest_path, *(entry.env_file for entry in entries)]:
        if path.exists() and not args.force:
            raise FileExistsError(
                f"{path} already exists. Re-run with --force to overwrite it."
            )

    research_snapshot = _resolve_paper_setup_snapshot_path(runtime_config, args)
    sec_user_agent = _resolve_paper_setup_sec_user_agent(runtime_config, args)
    profile_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        env_payload = _build_paper_env_payload(
            broker_config=broker_config,
            runtime_config=runtime_config,
            research_snapshot_path=research_snapshot,
            sec_user_agent=sec_user_agent,
        )
        env_payload.update(
            {
                "ALPACA_API_KEY": f"your-{entry.name}-paper-key",
                "ALPACA_SECRET_KEY": f"your-{entry.name}-paper-secret",
                "AI_INVESTING_PROFILE_NAME": entry.name.title(),
                "AI_INVESTING_RISK_PROFILE": entry.name,
                "AI_INVESTING_STATE_PATH": f".ai_investing_{entry.name}_state.json",
                "AI_INVESTING_PERFORMANCE_BASELINE": "100000",
            }
        )
        entry.env_file.parent.mkdir(parents=True, exist_ok=True)
        entry.env_file.write_text(_render_env_file(env_payload))

    write_profile_matrix(manifest_path, entries)

    print(f"Wrote profile matrix: {manifest_path}")
    for entry in entries:
        print(f"- env: {entry.env_file} ({entry.name})")
    print("")
    print("Next Steps")
    print("- Put a different Alpaca paper key and secret into each env file.")
    print(
        f"- Run previews across all profiles: PYTHONPATH=src python3 -m ai_investing.cli multi-profile-run --manifest {manifest_path}"
    )
    print(
        f"- Compare current performance: PYTHONPATH=src python3 -m ai_investing.cli multi-profile-report --manifest {manifest_path}"
    )


def _run_automation_setup_command(
    runtime_config,
    args: argparse.Namespace,
) -> None:
    repo_root = Path.cwd().resolve()
    env_path = Path(args.env_file).resolve()
    script_path = Path(args.script_file).resolve()
    cron_path = Path(args.cron_file).resolve()
    control_path = Path("automation/paper_trade.enabled").resolve()
    status_path = Path(args.status_file).resolve()
    manifest_path = Path(args.manifest).resolve()

    if not env_path.exists():
        raise FileNotFoundError(
            f"{env_path} does not exist. Create it first with `paper-setup` or manually."
        )
    schedule_times = _resolve_automation_schedule_times(args)
    for path in (script_path, cron_path):
        if path.exists() and not args.force:
            raise FileExistsError(
                f"{path} already exists. Re-run with --force to overwrite it."
            )

    script_path.parent.mkdir(parents=True, exist_ok=True)
    cron_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    write_automation_enabled(control_path, True)
    status_path.write_text(
        "run_phase=idle\nlast_result=never\nlast_message=Waiting for first scheduled run\n"
    )

    script_path.write_text(
        _render_automation_script(
            repo_root=repo_root,
            env_path=env_path,
            manifest_path=manifest_path,
            python_path=Path(sys.executable).resolve(),
            control_path=control_path,
            status_path=status_path,
            preview_only=args.preview_only,
        )
    )
    script_path.chmod(0o755)
    cron_path.write_text(
        _render_cron_file(
            script_path=script_path,
            schedule_times=schedule_times,
        )
    )

    mode = "preview-only dry run" if args.preview_only else "paper order submission"
    print(f"Wrote automation script: {script_path}")
    print(f"Wrote cron template: {cron_path}")
    print("")
    print("Automation Settings")
    print(f"- mode: {mode}")
    print(f"- env file: {env_path}")
    print(f"- control file: {control_path}")
    print(f"- status file: {status_path}")
    print(
        f"- schedule: weekdays at {', '.join(_format_schedule_time(hour, minute) for hour, minute in schedule_times)} (system local time)"
    )
    print("- log file: logs/paper-trade.log")
    print(
        f"- profile mode: {'multi-profile matrix' if manifest_path.exists() else 'single env file'}"
    )
    print("")
    print("Next Steps")
    print(f"- crontab {cron_path}")
    print("- crontab -l")
    print("- tail -f logs/paper-trade.log")
    print("- PYTHONPATH=src python3 -m ai_investing.cli automation-ui")


def _run_automation_ui_command(args: argparse.Namespace) -> None:
    host = args.host
    port = args.port
    repo_root = Path.cwd().resolve()
    python_path = Path(sys.executable).resolve()
    env_path = Path(args.env_file).resolve()
    script_path = Path(args.script_file).resolve()
    control_path = Path(args.control_file).resolve()
    cron_path = Path(args.cron_file).resolve()
    log_path = Path(args.log_file).resolve()
    status_path = Path(args.status_file).resolve()
    manifest_path = Path(args.manifest).resolve()
    if not (0 <= port <= 65535):
        raise ValueError("--port must be between 0 and 65535.")

    print(f"Automation UI: http://{host}:{port}")
    print("Press Ctrl+C to stop the UI server.")
    serve_automation_ui(
        host=host,
        port=port,
        repo_root=repo_root,
        python_path=python_path,
        control_path=control_path,
        script_path=script_path,
        env_path=env_path,
        cron_path=cron_path,
        log_path=log_path,
        state_path=status_path,
        manifest_path=manifest_path,
        profile_status_dir=repo_root / "automation" / "profiles",
        profile_log_dir=repo_root / "logs" / "profiles",
        trade_rationale_dir=repo_root / "logs" / "trade-rationales",
    )


def _run_multi_profile_run_command(args: argparse.Namespace) -> None:
    entries = load_profile_matrix(Path(args.manifest))
    failures: list[str] = []
    skipped: list[str] = []
    successes = 0

    for entry in entries:
        print("")
        env_values = load_env_file(entry.env_file)
        broker_config = load_broker_config(env_values)
        runtime_config = load_runtime_config(env_values)
        print(
            f"=== {runtime_config.profile_name} ({runtime_config.risk_profile}) | {entry.env_file} ==="
        )
        if not _has_usable_broker_credentials(broker_config):
            message = "Profile skipped: missing or placeholder Alpaca credentials."
            skipped.append(f"{runtime_config.profile_name}: {message}")
            print(message)
            continue
        try:
            require_broker_credentials(broker_config)
            client = AlpacaClient(broker_config)
            profile_args = argparse.Namespace(
                lookback_days=args.lookback_days,
                feed=args.feed,
                research_snapshot=None,
                training_window_bars=args.training_window_bars,
                submit=args.submit,
                force=args.force,
                no_official_news=args.no_official_news,
                no_llm_news=args.no_llm_news,
                official_news_lookback_days=args.official_news_lookback_days,
                require_official_news=args.require_official_news,
                require_llm_news=args.require_llm_news,
                manual=args.manual,
                preview_only=args.preview_only,
            )
            if args.submit:
                _run_automation_trade_command(
                    client, broker_config.paper, runtime_config, profile_args
                )
            else:
                _run_trade_command(client, broker_config.paper, runtime_config, profile_args)
            account = client.get_account()
            baseline = runtime_config.performance_baseline or account.equity
            total_return = (
                (account.equity / baseline) - 1.0 if baseline > 0 else 0.0
            )
            successes += 1
            print(
                f"Profile account equity: ${account.equity:,.2f} | baseline ${baseline:,.2f} | return {total_return:.2%}"
            )
        except Exception as exc:
            failures.append(f"{runtime_config.profile_name}: {exc}")
            print(f"Profile failed: {exc}")

    print("")
    print(
        f"Multi-profile summary: {successes} succeeded | {len(skipped)} skipped | {len(failures)} failed"
    )

    if failures and not args.allow_failures:
        raise RuntimeError("One or more profiles failed:\n- " + "\n- ".join(failures))
    if successes == 0 and (failures or skipped):
        details = failures or skipped
        raise RuntimeError("No profiles completed successfully:\n- " + "\n- ".join(details))


def _run_multi_profile_report_command(args: argparse.Namespace) -> None:
    entries = load_profile_matrix(Path(args.manifest))
    rows: list[dict[str, object]] = []

    for entry in entries:
        env_values = load_env_file(entry.env_file)
        broker_config = load_broker_config(env_values)
        runtime_config = load_runtime_config(env_values)
        require_broker_credentials(broker_config)
        client = AlpacaClient(broker_config)
        account = client.get_account()
        baseline = runtime_config.performance_baseline or account.equity
        total_return = (account.equity / baseline) - 1.0 if baseline > 0 else 0.0
        rows.append(
            {
                "profile_name": runtime_config.profile_name,
                "risk_profile": runtime_config.risk_profile,
                "equity": account.equity,
                "baseline": baseline,
                "total_return": total_return,
                "state_path": runtime_config.state_path,
            }
        )

    rows.sort(key=lambda row: row["total_return"], reverse=True)
    print("Profile Performance")
    for index, row in enumerate(rows, start=1):
        print(
            f"{index}. {row['profile_name']} ({row['risk_profile']}): "
            f"equity=${row['equity']:,.2f} | baseline=${row['baseline']:,.2f} | return={row['total_return']:.2%}"
        )
        print(f"   state: {row['state_path']}")


def _run_signal_command(
    client: AlpacaClient, runtime_config, args: argparse.Namespace
) -> None:
    signal, _history, params, _signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        args=args,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=_resolve_research_snapshot_path(runtime_config, args),
        training_window_bars=args.training_window_bars,
    )
    print(f"Using parameters: {params}")
    _print_signal(signal)
    _print_official_news(signal.official_news)
    _print_selected_research(signal)


def _run_trade_command(
    client: AlpacaClient,
    is_paper: bool,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    clock = client.get_clock()
    if args.submit and not clock.is_open:
        raise RuntimeError(
            f"Market is closed at {clock.timestamp}. Refusing to submit market orders."
        )

    signal, history, params, signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        args=args,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=_resolve_research_snapshot_path(runtime_config, args),
        training_window_bars=args.training_window_bars,
    )
    latest_prices = {
        symbol: closes[signal_index] for symbol, closes in history.closes.items()
    }
    result: ExecutionResult | None = None
    try:
        result = execute_rebalance(
            client=client,
            signal=signal,
            state_path=runtime_config.state_path,
            latest_prices=latest_prices,
            allow_live=runtime_config.enable_live,
            is_paper=is_paper,
            submit=args.submit,
            force=args.force,
            live_price_feed=args.feed or runtime_config.default_feed,
            max_price_drift_pct=runtime_config.max_price_drift_pct,
        )
    except Exception as exc:
        report_path = _maybe_write_trade_rationale_report(
            runtime_config=runtime_config,
            params=params,
            signal=signal,
            result=None,
            submit=args.submit,
            clock_timestamp=clock.timestamp,
            execution_error=str(exc),
        )
        if report_path is not None:
            print(f"Trade rationale report: {report_path}")
        raise
    actions = result.actions
    responses = result.responses
    state = result.state

    print(f"Using parameters: {params}")
    _print_signal(signal)
    _print_official_news(signal.official_news)
    _print_selected_research(signal)
    print("")
    if args.submit and result.submitted_actions:
        print("Submitted Basket")
        for action in result.submitted_actions:
            if action.qty is None:
                details = f"${action.notional:.2f}"
            else:
                details = f"{action.qty:.6f} shares"
            print(
                f"- {action.side.upper()} {action.symbol}: {details} ({action.reason})"
            )
    elif not actions:
        print("No rebalance required.")
    else:
        print("Planned Actions")
        for action in actions:
            if action.qty is None:
                details = f"${action.notional:.2f}"
            else:
                details = f"{action.qty:.6f} shares"
            print(
                f"- {action.side.upper()} {action.symbol}: {details} ({action.reason})"
            )

    if args.submit and actions and _actions_differ(actions, result.submitted_actions):
        print("")
        print("Remaining Actions")
        for action in actions:
            if action.qty is None:
                details = f"${action.notional:.2f}"
            else:
                details = f"{action.qty:.6f} shares"
            print(
                f"- {action.side.upper()} {action.symbol}: {details} ({action.reason})"
            )

    if result.skipped_messages:
        print("")
        print("Skipped Actions")
        for message in result.skipped_messages:
            print(f"- {message}")

    if args.submit:
        print("")
        print(f"Submitted {len(responses)} orders.")
        print(f"High-water mark: ${state.high_water_mark:.2f}")

    report_path = _maybe_write_trade_rationale_report(
        runtime_config=runtime_config,
        params=params,
        signal=signal,
        result=result,
        submit=args.submit,
        clock_timestamp=clock.timestamp,
        execution_error=None,
    )
    if report_path is not None:
        print("")
        print(f"Trade rationale report: {report_path}")


def _run_reset_account_command(
    client: AlpacaClient,
    is_paper: bool,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    if not is_paper and not args.allow_live:
        raise RuntimeError(
            "Refusing to reset a live account without --allow-live."
        )

    positions = client.get_positions()
    if positions:
        print("Current Positions")
        for position in positions:
            print(
                f"- {position.symbol}: qty={position.qty:.6f} | market_value=${position.market_value:,.2f}"
            )
    else:
        print("Current Positions")
        print("- none")

    cancel_responses = client.cancel_all_orders()
    print("")
    print(f"Canceled open orders: {len(cancel_responses)}")

    liquidation_responses: list[dict[str, object]] = []
    if positions:
        liquidation_responses = client.close_all_positions(cancel_orders=False)
    print(f"Submitted liquidation orders: {len(liquidation_responses)}")

    if args.keep_state:
        print(f"Kept local state: {runtime_config.state_path}")
    else:
        save_state(runtime_config.state_path, RuntimeState())
        print(f"Cleared local state: {runtime_config.state_path}")


def _run_automation_trade_command(
    client: AlpacaClient,
    is_paper: bool,
    runtime_config,
    args: argparse.Namespace,
) -> None:
    if args.preview_only:
        print("Automation mode: preview-only dry run.")
        _run_trade_command(
            client,
            is_paper,
            runtime_config,
            _copy_namespace_with_submit(args, submit=False),
        )
        return

    clock = client.get_clock()
    if clock.is_open:
        _run_trade_command(
            client,
            is_paper,
            runtime_config,
            _copy_namespace_with_submit(args, submit=True),
        )
        return

    if args.manual:
        print(
            f"Market is closed at {clock.timestamp}. Running a preview instead of submitting orders."
        )
        _run_trade_command(
            client,
            is_paper,
            runtime_config,
            _copy_namespace_with_submit(args, submit=False),
        )
        return

    print(f"Market is closed at {clock.timestamp}. Skipping scheduled run.")


def _run_research_command(
    client: AlpacaClient,
    runtime_config,
    args: argparse.Namespace,
    *,
    research_snapshot_path: Path | None,
) -> None:
    if research_snapshot_path is None:
        raise RuntimeError(
            "Research mode requires a snapshot file. Set AI_INVESTING_RESEARCH_SNAPSHOT_PATH or pass --research-snapshot."
        )
    signal, _history, params, _signal_index = _compute_latest_signal(
        client=client,
        runtime_config=runtime_config,
        args=args,
        lookback_days=args.lookback_days,
        feed=args.feed or runtime_config.default_feed,
        research_snapshot_path=research_snapshot_path,
        training_window_bars=args.training_window_bars,
    )
    print(f"Using parameters: {params}")
    _print_signal(signal)
    _print_official_news(signal.official_news)
    print("")
    print("Research Scorecard")
    _print_all_research(signal)


def _run_snapshot_only_research(
    runtime_config,
    research_snapshot_path: Path | None,
    args: argparse.Namespace | None = None,
) -> None:
    if research_snapshot_path is None:
        raise RuntimeError(
            "Research mode without broker credentials requires a local research snapshot."
        )
    snapshot = load_research_snapshot(research_snapshot_path)
    official_news = _load_official_news_context(
        runtime_config=runtime_config,
        args=args,
        snapshot=snapshot,
    )
    overlay = _build_research_overlay(snapshot, official_news)
    assert overlay is not None
    print("Mode: research snapshot only")
    print("Quant component: neutral 0.50")
    _print_official_news(official_news)
    print("")
    assessments = [
        overlay.assess_symbol(symbol, 0.5)
        for symbol in sorted(overlay.snapshot.assets)
    ]
    for assessment in sorted(
        assessments, key=lambda item: item.total_score, reverse=True
    ):
        print(_format_assessment_line(assessment))


def _compute_latest_signal(
    client: AlpacaClient,
    runtime_config,
    args: argparse.Namespace,
    lookback_days: int,
    feed: str,
    research_snapshot_path: Path | None = None,
    training_window_bars: int = 756,
):
    end = _today_utc()
    start = end - timedelta(days=lookback_days)
    symbols = list(
        dict.fromkeys(
            list(runtime_config.risk_on_universe)
            + list(runtime_config.equity_universe)
            + list(runtime_config.defensive_universe)
        )
    )
    history = _load_history(
        client=client,
        symbols=symbols,
        start=start,
        end=end,
        feed=feed,
    )
    signal_index = len(history.dates) - 1
    if history.dates[signal_index] >= _today_utc():
        signal_index -= 1
    snapshot = (
        load_research_snapshot(research_snapshot_path)
        if research_snapshot_path is not None
        else None
    )
    official_news = _load_official_news_context(
        runtime_config=runtime_config,
        args=args,
        snapshot=snapshot,
    )
    research_overlay = _build_research_overlay(snapshot, official_news)
    if snapshot is not None and research_overlay is not None:
        research_overlay.validate_for_date(
            _resolve_research_validation_date(history.dates[signal_index]),
            max_age_days=runtime_config.research_max_age_days,
        )
    base_params = strategy_parameters_for_risk_profile(runtime_config.risk_profile)
    best_result = select_walk_forward_parameters(
        history,
        risk_on_universe=runtime_config.risk_on_universe,
        equity_universe=runtime_config.equity_universe,
        defensive_universe=runtime_config.defensive_universe,
        base_params=base_params,
        signal_index=signal_index,
        training_window=training_window_bars,
    )
    strategy = ETFMomentumStrategy(
        risk_on_universe=runtime_config.risk_on_universe,
        equity_universe=runtime_config.equity_universe,
        defensive_universe=runtime_config.defensive_universe,
        params=best_result.params,
        research_overlay=research_overlay,
    )
    signal = replace(strategy.signal_for_index(history, signal_index), official_news=official_news)
    return signal, history, best_result.params, signal_index


def _load_history(
    *,
    client: AlpacaClient,
    symbols: list[str],
    start: date,
    end: date,
    feed: str,
):
    raw_history = client.get_daily_closes(
        symbols=symbols,
        start=start,
        end=end,
        feed=feed,
    )
    return align_history(raw_history)


def _print_signal(signal) -> None:
    print(f"As of: {signal.as_of.isoformat()}")
    print(f"Regime: {signal.regime}")
    print("Target weights:")
    for symbol, weight in sorted(signal.weights.items()):
        print(f"- {symbol}: {weight:.2%}")
    cash = 1.0 - sum(signal.weights.values())
    print(f"- CASH: {cash:.2%}")


def _print_official_news(official_news: OfficialNewsContext | None) -> None:
    if official_news is None:
        return
    print("")
    print("Official News Context")
    if official_news.risk_on_score is not None:
        print(f"- Risk assets: {official_news.risk_on_score:.2f}")
    if official_news.duration_score is not None:
        print(f"- Duration: {official_news.duration_score:.2f}")
    if official_news.cash_score is not None:
        print(f"- Cash / short duration: {official_news.cash_score:.2f}")
    if official_news.gold_score is not None:
        print(f"- Gold: {official_news.gold_score:.2f}")
    for source_name, status in sorted(official_news.source_status.items()):
        print(f"- {source_name}: {status}")
    for line in summarize_official_news(official_news, limit=4):
        print(f"- headline: {line}")


def _print_selected_research(signal) -> None:
    if not signal.assessments:
        return
    print("")
    print("Selected Research")
    selected_symbols = set(signal.weights)
    selected = [
        signal.assessments[symbol]
        for symbol in selected_symbols
        if symbol in signal.assessments
    ]
    if not selected:
        return
    for assessment in sorted(selected, key=lambda item: item.total_score, reverse=True):
        print(_format_assessment_line(assessment))


def _print_all_research(signal) -> None:
    if not signal.assessments:
        print("No research overlay loaded.")
        return
    for assessment in sorted(
        signal.assessments.values(), key=lambda item: item.total_score, reverse=True
    ):
        print(_format_assessment_line(assessment))


def _format_assessment_line(assessment) -> str:
    component_parts = [
        f"{name}={score:.2f}"
        for name, score in sorted(assessment.component_scores.items())
    ]
    research = (
        f" | research={assessment.research_score:.2f}"
        if assessment.research_score is not None
        else ""
    )
    benchmark = (
        f" | benchmark={assessment.benchmark_index}"
        if assessment.benchmark_index
        else ""
    )
    sector = (
        f" | sector={assessment.sector}"
        if getattr(assessment, "sector", None)
        else ""
    )
    return (
        f"- {assessment.symbol}: total={assessment.total_score:.2f}"
        f"{research} | {' '.join(component_parts)}{benchmark}{sector}"
    )


def _maybe_write_trade_rationale_report(
    *,
    runtime_config,
    params: StrategyParameters,
    signal,
    result: ExecutionResult | None,
    submit: bool,
    clock_timestamp: str,
    execution_error: str | None,
) -> Path | None:
    try:
        return _write_trade_rationale_report(
            runtime_config=runtime_config,
            params=params,
            signal=signal,
            result=result,
            submit=submit,
            clock_timestamp=clock_timestamp,
            execution_error=execution_error,
        )
    except Exception as exc:
        print(f"Warning: unable to write trade rationale report: {exc}")
        return None


def _write_trade_rationale_report(
    *,
    runtime_config,
    params: StrategyParameters,
    signal,
    result: ExecutionResult | None,
    submit: bool,
    clock_timestamp: str,
    execution_error: str | None,
) -> Path:
    reports_dir = Path("logs/trade-rationales")
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = _utc_now_label()
    mode = "submit" if submit else "preview"
    status = _trade_report_status(result=result, submit=submit, execution_error=execution_error)
    filename = (
        f"{generated_at.replace(':', '').replace('-', '')}"
        f"-{_slugify_profile_name(runtime_config.profile_name)}-{mode}.md"
    )
    report_path = reports_dir / filename
    report_path.write_text(
        _render_trade_rationale_report(
            runtime_config=runtime_config,
            params=params,
            signal=signal,
            result=result,
            generated_at=generated_at,
            mode=mode,
            status=status,
            clock_timestamp=clock_timestamp,
            execution_error=execution_error,
        )
    )
    return report_path


def _render_trade_rationale_report(
    *,
    runtime_config,
    params: StrategyParameters,
    signal,
    result: ExecutionResult | None,
    generated_at: str,
    mode: str,
    status: str,
    clock_timestamp: str,
    execution_error: str | None,
) -> str:
    cash_target = max(0.0, 1.0 - sum(signal.weights.values()))
    diagnostics = signal.diagnostics or {}
    selected_count = int(diagnostics.get("selected_count", len(signal.weights)))
    selected_etf_count = int(diagnostics.get("selected_etf_count", 0))
    selected_equity_count = int(diagnostics.get("selected_equity_count", 0))
    decision_summary = _build_trade_decision_summary(
        regime=signal.regime,
        selected_count=selected_count,
        selected_etf_count=selected_etf_count,
        selected_equity_count=selected_equity_count,
        cash_target=cash_target,
    )
    report_lines = [
        "# Trade Rationale",
        "",
        f"- Generated At: {generated_at}",
        f"- Profile: {runtime_config.profile_name}",
        f"- Risk Profile: {runtime_config.risk_profile}",
        f"- Run Mode: {mode}",
        f"- Run Status: {status}",
        f"- Market Clock: {clock_timestamp}",
        f"- Signal As Of: {signal.as_of.isoformat()}",
        "",
        "## Decision Summary",
        f"- {decision_summary}",
        f"- Parameters: `{params}`",
        "",
        "## Target Portfolio",
    ]
    report_lines.extend(
        f"- {symbol}: {weight:.2%}"
        for symbol, weight in sorted(signal.weights.items())
    )
    report_lines.append(f"- CASH: {cash_target:.2%}")

    if signal.official_news is not None:
        report_lines.extend(_render_report_news_section(signal.official_news))

    selected_research_lines = _selected_research_lines(signal)
    if selected_research_lines:
        report_lines.extend(["", "## Selected Research"])
        report_lines.extend(selected_research_lines)

    if result is not None and result.submitted_actions:
        report_lines.extend(["", "## Submitted Basket"])
        report_lines.extend(_format_action_line(action) for action in result.submitted_actions)
    elif result is not None and result.actions:
        report_lines.extend(["", "## Planned Actions"])
        report_lines.extend(_format_action_line(action) for action in result.actions)

    if (
        result is not None
        and result.actions
        and _actions_differ(result.actions, result.submitted_actions)
    ):
        report_lines.extend(["", "## Remaining Actions"])
        report_lines.extend(_format_action_line(action) for action in result.actions)

    if result is not None and result.skipped_messages:
        report_lines.extend(["", "## Skipped Actions"])
        report_lines.extend(f"- {message}" for message in result.skipped_messages)

    if execution_error is not None:
        report_lines.extend(["", "## Execution Error", f"- {execution_error}"])

    report_lines.append("")
    return "\n".join(report_lines)


def _build_trade_decision_summary(
    *,
    regime: str,
    selected_count: int,
    selected_etf_count: int,
    selected_equity_count: int,
    cash_target: float,
) -> str:
    if regime == "risk_on":
        return (
            f"The model stayed risk-on with {selected_count} qualifying positions "
            f"({selected_etf_count} ETFs and {selected_equity_count} equities) "
            f"and a target cash buffer of {cash_target:.2%}."
        )
    return (
        f"The model moved defensive because it did not find enough qualifying "
        f"risk-on positions. Target cash is {cash_target:.2%}."
    )


def _render_report_news_section(official_news: OfficialNewsContext) -> list[str]:
    lines = ["", "## Official News Context"]
    if official_news.risk_on_score is not None:
        lines.append(f"- Risk assets: {official_news.risk_on_score:.2f}")
    if official_news.duration_score is not None:
        lines.append(f"- Duration: {official_news.duration_score:.2f}")
    if official_news.cash_score is not None:
        lines.append(f"- Cash / short duration: {official_news.cash_score:.2f}")
    if official_news.gold_score is not None:
        lines.append(f"- Gold: {official_news.gold_score:.2f}")
    for source_name, source_status in sorted(official_news.source_status.items()):
        lines.append(f"- {source_name}: {source_status}")
    for headline in summarize_official_news(official_news, limit=4):
        lines.append(f"- Headline: {headline}")
    return lines


def _selected_research_lines(signal) -> list[str]:
    if not signal.assessments:
        return []
    selected_symbols = set(signal.weights)
    selected = [
        signal.assessments[symbol]
        for symbol in selected_symbols
        if symbol in signal.assessments
    ]
    return [
        _format_assessment_line(assessment)
        for assessment in sorted(selected, key=lambda item: item.total_score, reverse=True)
    ]


def _trade_report_status(
    *,
    result: ExecutionResult | None,
    submit: bool,
    execution_error: str | None,
) -> str:
    if execution_error is not None:
        return "failed"
    if not submit:
        return "preview"
    if result is None:
        return "unknown"
    if result.responses:
        return "submitted"
    if result.submitted_actions:
        return "submitted"
    if result.actions:
        return "planned"
    return "no_rebalance"


def _slugify_profile_name(value: str) -> str:
    cleaned = "".join(
        character.lower() if character.isalnum() else "-"
        for character in value.strip()
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "profile"


def _actions_differ(
    left: list[RebalanceAction], right: list[RebalanceAction]
) -> bool:
    if len(left) != len(right):
        return True
    return [_action_key(action) for action in left] != [
        _action_key(action) for action in right
    ]


def _action_key(action: RebalanceAction) -> tuple[str, str, float, float | None, str]:
    return (
        action.side,
        action.symbol,
        round(action.notional, 6),
        None if action.qty is None else round(action.qty, 6),
        action.reason,
    )


def _format_action_line(action: RebalanceAction) -> str:
    if action.qty is None:
        details = f"${action.notional:.2f}"
    else:
        details = f"{action.qty:.6f} shares"
    return f"- {action.side.upper()} {action.symbol}: {details} ({action.reason})"


def _build_research_overlay(
    snapshot: ResearchSnapshot | None,
    official_news: OfficialNewsContext | None,
) -> ResearchOverlay | None:
    if snapshot is None and official_news is None:
        return None
    if snapshot is None:
        snapshot = ResearchSnapshot(
            as_of=official_news.as_of,
            weights=ResearchWeights(
                quant=0.80,
                company=0.0,
                index=0.0,
                etf=0.0,
                news=0.20,
                minimum_total_score=0.35,
            ),
            assets={},
        )
    return ResearchOverlay(snapshot, official_news=official_news)


def _load_official_news_context(
    *,
    runtime_config,
    args: argparse.Namespace | None,
    snapshot: ResearchSnapshot | None,
) -> OfficialNewsContext | None:
    if args is not None and getattr(args, "no_official_news", False):
        return None
    if not runtime_config.enable_official_news:
        return None
    lookback_days = (
        getattr(args, "official_news_lookback_days", None)
        or runtime_config.official_news_lookback_days
    )
    require_success = runtime_config.require_official_news or bool(
        args is not None and getattr(args, "require_official_news", False)
    )
    enable_llm = runtime_config.enable_llm_news and not bool(
        args is not None and getattr(args, "no_llm_news", False)
    )
    require_llm = runtime_config.require_llm_news or bool(
        args is not None and getattr(args, "require_llm_news", False)
    )
    return build_official_news_context(
        as_of=_today_utc(),
        lookback_days=lookback_days,
        user_agent=runtime_config.sec_user_agent,
        assets=snapshot.assets if snapshot is not None else None,
        require_success=require_success,
        enable_llm=enable_llm,
        require_llm=require_llm,
        llm_api_key=runtime_config.llm_news_api_key,
        llm_model=runtime_config.llm_news_model,
        llm_base_url=runtime_config.llm_news_base_url,
        llm_max_items=runtime_config.llm_news_max_items,
        llm_max_chars=runtime_config.llm_news_max_chars,
    )


def _has_broker_credentials(broker_config) -> bool:
    return bool(broker_config.api_key and broker_config.secret_key)


def _has_usable_broker_credentials(broker_config) -> bool:
    return _has_broker_credentials(broker_config) and not (
        _looks_like_placeholder_credential(broker_config.api_key)
        or _looks_like_placeholder_credential(broker_config.secret_key)
    )


def _looks_like_placeholder_credential(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return (
        normalized == ""
        or normalized.startswith("your-")
        or normalized.startswith("your_")
        or "placeholder" in normalized
    )


def _resolve_research_snapshot_path(
    runtime_config, args: argparse.Namespace
) -> Path | None:
    raw_value = getattr(args, "research_snapshot", None)
    if raw_value:
        return Path(raw_value)
    return runtime_config.research_snapshot_path


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _utc_now_label() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_paper_env_payload(
    *,
    broker_config,
    runtime_config,
    research_snapshot_path: Path | None,
    sec_user_agent: str,
) -> dict[str, str]:
    return {
        "ALPACA_API_KEY": broker_config.api_key or "your-paper-key",
        "ALPACA_SECRET_KEY": broker_config.secret_key or "your-paper-secret",
        "OPENAI_API_KEY": runtime_config.llm_news_api_key,
        "ALPACA_PAPER": "true",
        "AI_INVESTING_ENABLE_LIVE": "0",
        "AI_INVESTING_STATE_PATH": ".ai_investing_paper_state.json",
        "AI_INVESTING_PROFILE_NAME": runtime_config.profile_name,
        "AI_INVESTING_RISK_PROFILE": runtime_config.risk_profile,
        "AI_INVESTING_PERFORMANCE_BASELINE": (
            ""
            if runtime_config.performance_baseline is None
            else str(runtime_config.performance_baseline)
        ),
        "AI_INVESTING_DEFAULT_FEED": runtime_config.default_feed,
        "AI_INVESTING_RISK_ON": ",".join(runtime_config.risk_on_universe),
        "AI_INVESTING_EQUITIES": ",".join(runtime_config.equity_universe),
        "AI_INVESTING_DEFENSIVE": ",".join(runtime_config.defensive_universe),
        "AI_INVESTING_RESEARCH_SNAPSHOT_PATH": (
            str(research_snapshot_path) if research_snapshot_path is not None else ""
        ),
        "AI_INVESTING_RESEARCH_MAX_AGE_DAYS": str(
            runtime_config.research_max_age_days
        ),
        "AI_INVESTING_ENABLE_OFFICIAL_NEWS": (
            "1" if runtime_config.enable_official_news else "0"
        ),
        "AI_INVESTING_ENABLE_LLM_NEWS": (
            "1" if runtime_config.enable_llm_news else "0"
        ),
        "AI_INVESTING_OFFICIAL_NEWS_LOOKBACK_DAYS": str(
            runtime_config.official_news_lookback_days
        ),
        "AI_INVESTING_REQUIRE_OFFICIAL_NEWS": (
            "1" if runtime_config.require_official_news else "0"
        ),
        "AI_INVESTING_REQUIRE_LLM_NEWS": (
            "1" if runtime_config.require_llm_news else "0"
        ),
        "AI_INVESTING_SEC_USER_AGENT": sec_user_agent,
        "AI_INVESTING_LLM_NEWS_MODEL": runtime_config.llm_news_model,
        "AI_INVESTING_OPENAI_BASE_URL": runtime_config.llm_news_base_url,
        "AI_INVESTING_LLM_NEWS_MAX_ITEMS": str(runtime_config.llm_news_max_items),
        "AI_INVESTING_LLM_NEWS_MAX_CHARS": str(runtime_config.llm_news_max_chars),
        "AI_INVESTING_MAX_PRICE_DRIFT_PCT": str(runtime_config.max_price_drift_pct),
        "AI_INVESTING_CA_BUNDLE": os.getenv("AI_INVESTING_CA_BUNDLE", ""),
        "AI_INVESTING_SSL_NO_VERIFY": os.getenv("AI_INVESTING_SSL_NO_VERIFY", ""),
    }


def _render_env_file(values: dict[str, str]) -> str:
    lines = [f"{key}={_env_quote(value)}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def _env_quote(value: str) -> str:
    if value == "":
        return ""
    if any(character.isspace() for character in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _resolve_paper_setup_snapshot_path(
    runtime_config,
    args: argparse.Namespace,
) -> Path | None:
    raw_value = getattr(args, "research_snapshot", None)
    if raw_value:
        return Path(raw_value)
    if runtime_config.research_snapshot_path is not None:
        return runtime_config.research_snapshot_path
    default_snapshot = Path("examples/research_snapshot.example.json")
    if default_snapshot.exists():
        return default_snapshot
    return None


def _resolve_paper_setup_sec_user_agent(
    runtime_config,
    args: argparse.Namespace,
) -> str:
    raw_value = getattr(args, "sec_user_agent", None)
    if raw_value:
        return raw_value
    return runtime_config.sec_user_agent


def _resolve_research_validation_date(signal_date: date) -> date:
    return max(signal_date, _today_utc())


def _copy_namespace_with_submit(
    args: argparse.Namespace,
    *,
    submit: bool,
) -> argparse.Namespace:
    values = vars(args).copy()
    values["submit"] = submit
    return argparse.Namespace(**values)


def _render_automation_script(
    *,
    repo_root: Path,
    env_path: Path,
    manifest_path: Path,
    python_path: Path,
    control_path: Path,
    status_path: Path,
    preview_only: bool,
) -> str:
    multi_profile_preview_line = (
        '  automation_args+=("--preview-only")\n' if preview_only else ""
    )
    multi_profile_submit_line = (
        "" if preview_only else '  automation_args+=("--submit")\n'
    )
    single_profile_preview_line = (
        '  automation_args+=("--preview-only")\n' if preview_only else ""
    )
    return f"""#!/bin/zsh
set -euo pipefail

status_file="{status_path}"
run_kind="scheduled"
if [ -f "{manifest_path}" ]; then
  automation_args=("multi-profile-run" "--manifest" "{manifest_path}" "--allow-failures")
{multi_profile_submit_line}{multi_profile_preview_line}else
  automation_args=("automation-run")
{single_profile_preview_line}fi
if [ "${{AI_INVESTING_FORCE_RUN:-0}}" = "1" ]; then
  automation_args+=("--manual")
fi
if [ "${{AI_INVESTING_FORCE_RUN:-0}}" = "1" ]; then
  run_kind="manual"
fi

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

run_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

trap 'code=$?; run_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; write_status failed failed "$run_kind run failed" "$run_started_at" "$run_finished_at" "$code"; echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) $run_kind run failed (exit $code) ==="; exit $code' ERR

cd "{repo_root}"
mkdir -p "{repo_root / 'logs'}"
exec >> "{repo_root / 'logs' / 'paper-trade.log'}" 2>&1

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) $run_kind run start ==="
write_status running pending "$run_kind run starting" "$run_started_at" "" ""
if [ "${{AI_INVESTING_FORCE_RUN:-0}}" != "1" ] && ( [ ! -f "{control_path}" ] || ! grep -q '^enabled=1$' "{control_path}" ); then
  run_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_status idle skipped "Automation disabled; skipped scheduled run" "$run_started_at" "$run_finished_at" "0"
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) automation disabled; skipping run ==="
  exit 0
fi
set -a
source "{env_path}"
set +a
PYTHONPATH=src "{python_path}" -m ai_investing.cli "${{automation_args[@]}}"
run_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_status idle success "$run_kind run completed successfully" "$run_started_at" "$run_finished_at" "0"
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) $run_kind run end ==="
"""


def _render_cron_file(
    *,
    script_path: Path,
    schedule_times: list[tuple[int, int]],
) -> str:
    entries = "".join(
        f"{minute} {hour} * * 1-5 /bin/zsh \"{script_path}\"\n"
        for hour, minute in schedule_times
    )
    return (
        "# Load this schedule with: crontab automation/paper_trade.cron\n"
        "# Weekdays only. Time uses the machine's local timezone.\n"
        f"{entries}"
    )


def _resolve_automation_schedule_times(
    args: argparse.Namespace,
) -> list[tuple[int, int]]:
    raw_times = (getattr(args, "times", "") or "").strip()
    if raw_times:
        return _parse_schedule_times(raw_times)
    return [_validate_schedule_time(args.hour, args.minute)]


def _parse_schedule_times(raw_times: str) -> list[tuple[int, int]]:
    schedule_times: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for raw_item in raw_times.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                f"Invalid schedule time {item!r}. Expected HH:MM format."
            )
        hour_text, minute_text = item.split(":", 1)
        try:
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid schedule time {item!r}. Expected HH:MM format."
            ) from exc
        schedule_time = _validate_schedule_time(hour, minute)
        if schedule_time in seen:
            continue
        schedule_times.append(schedule_time)
        seen.add(schedule_time)
    if not schedule_times:
        raise ValueError("At least one automation schedule time is required.")
    return sorted(schedule_times)


def _validate_schedule_time(hour: int, minute: int) -> tuple[int, int]:
    if not (0 <= hour <= 23):
        raise ValueError("Automation schedule hour must be between 0 and 23.")
    if not (0 <= minute <= 59):
        raise ValueError("Automation schedule minute must be between 0 and 59.")
    return hour, minute


def _format_schedule_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


if __name__ == "__main__":
    main()
