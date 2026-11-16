"""Command line: python -m app <command>."""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from . import config, mailer, migrate, ops, runner, settings, tick
from .config import Config, ConfigError
from .dashboard import Client
from .rsync import StopRequest

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def _cmd_config(_args: argparse.Namespace, cfg: Config, client: Client) -> int:
    jobs, errors = config.load_jobs(cfg)
    print(f"{settings.CONFIG_PATH}: OK")
    print(f"dashboard: {client.describe()}")
    print(f"{settings.JOBS_DIR}: {len(jobs)} job(s)")
    for job in jobs:
        crons = ", ".join(job.enabled_crons()) or "no schedule enabled"
        print(f"  {job.name}: {len(job.pairs)} pair(s), {crons}")
    for message in errors:
        print(message, file=sys.stderr)
    return 1 if errors else 0


def _cmd_tick(_args: argparse.Namespace, cfg: Config, client: Client) -> int:
    tick.run_once(cfg, client)
    return 0


def _cmd_run(args: argparse.Namespace, cfg: Config, client: Client) -> int:
    job = config.find_job(args.job, cfg)
    stop = StopRequest()
    with runner.stop_on_signals(stop):
        ok = runner.run_job(job, cfg, client, dry_run=args.dry_run, stop=stop)
    return 0 if ok else 1


def _cmd_stop(args: argparse.Namespace, _cfg: Config, _client: Client) -> int:
    if not ops.stop_job(args.job):
        print(f"'{args.job}' is not running", file=sys.stderr)
        return 1
    print(f"asked '{args.job}' to stop; rsync keeps its partial file and the job resumes on its next run")
    return 0


def _cmd_verify(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    if args.job:
        jobs = [config.find_job(args.job, cfg)]
    else:
        jobs, errors = config.load_jobs(cfg)
        for message in errors:
            print(message, file=sys.stderr)
    if not jobs:
        print(f"no jobs in {settings.JOBS_DIR}", file=sys.stderr)
        return 1
    kind = "deep (reads file content, slow)" if args.deep else "quick (size/mtime only)"
    ok = True
    for job in jobs:
        for result in ops.verify_job(job, deep=args.deep, remotes=cfg.remotes):
            print(f"{result['status'].upper():<9}{result['label']} [{kind}]  {result['detail']}")
            ok = ok and result["status"] == "ok"
    return 0 if ok else 1


def _cmd_jobs(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    rows, errors = ops.jobs_overview(cfg)
    for row in rows:
        nxt = row["next"]
        running = "\trunning" if row["running"] is not None else ""
        print(f"{row['job'].name}\tnext {nxt.strftime('%Y-%m-%d %H:%M') if nxt else '-'}"
              f"\tlast {ops.format_last(row['last'])}{running}")
    for message in errors:
        print(message, file=sys.stderr)
    return 1 if errors else 0


def _cmd_testmail(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    if not mailer.is_configured(cfg.email):
        print("email is not configured: set email.smtp_server and email.to_addrs in config.yaml", file=sys.stderr)
        return 1
    body = (f"This is a test message from the JABS sync agent on {settings.machine_hostname(cfg.hostname)}.\n"
            f"Server: {cfg.email.smtp_server}:{cfg.email.smtp_port}\n")
    if not mailer.send(cfg.email, "JABS sync agent test message", body):
        print("sending failed; see the error above", file=sys.stderr)
        return 1
    print(f"test message sent to {', '.join(cfg.email.to_addrs)}")
    return 0


def _cmd_paths(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    print("\n".join(ops.writable_paths(cfg)))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    source = Path(args.dir) if args.dir else settings.BASE_DIR.parent / "local_sync_agent"
    report = migrate.import_local_sync(source, keep_schedules=args.keep_schedules, force=args.force,
                                       dry_run=args.dry_run)
    for label in report["written"]:
        print(f"{'would write' if args.dry_run else 'wrote'} {label}")
    for label in report["kept"]:
        print(f"kept the existing {label} (use --force to replace it)")
    for message in report["warnings"]:
        print(f"warning: {message}", file=sys.stderr)
    for message in report["errors"]:
        print(f"error: {message}", file=sys.stderr)
    if not report["errors"]:
        for note in report["notes"]:
            print(note)
    return 1 if report["errors"] else 0


def _cmd_reset(args: argparse.Namespace, _cfg: Config, _client: Client) -> int:
    if not args.yes:
        print("reset deletes lock files and the schedule state; re-run with --yes to confirm", file=sys.stderr)
        return 2
    removed = ops.reset_state()
    print(f"reset: removed {removed} file(s); synced data and your config are untouched")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="JABS sync agent (rsync)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=func)
        return command

    add("config", _cmd_config, "validate config.yaml and the job files")
    add("admin", None, "open the interactive administration console").add_argument(
        "--unit", help="systemd unit name of this install, if it cannot be detected (e.g. jabs-sync-agent)")
    add("tick", _cmd_tick, "run the periodic work once (driven by the systemd timer)")
    run = add("run", _cmd_run, "run one sync job now")
    run.add_argument("job", help="job name (config/jobs/NAME.yaml) or a path to a job file")
    run.add_argument("--dry-run", action="store_true", help="pass --dry-run to rsync (still reported to the dashboard)")
    stop = add("stop", _cmd_stop, "stop a running job gracefully; it resumes on its next run")
    stop.add_argument("job", help="job name")
    verify = add("verify", _cmd_verify, "compare source and destination without syncing (all jobs unless one is named)")
    verify.add_argument("job", nargs="?", help="job name or path to a job file")
    verify.add_argument("--deep", action="store_true", help="read and compare file content (slow)")
    add("jobs", _cmd_jobs, "list jobs with their next and last run")
    add("testmail", _cmd_testmail, "send a test message with the email settings")
    add("paths", _cmd_paths, "print the directories the agent must be able to write")
    add("reset", _cmd_reset, "delete lock files and schedule state (never synced data)").add_argument(
        "--yes", action="store_true", help="confirm")
    imp = add("import-local-sync", None, "convert a local_sync_agent install's configuration into this one")
    imp.add_argument("dir", nargs="?", help="folder of the local_sync_agent install (default: ../local_sync_agent)")
    imp.add_argument("--keep-schedules", action="store_true", help="keep the enabled schedules (default: import them off)")
    imp.add_argument("--force", action="store_true", help="replace files that already exist here")
    imp.add_argument("--dry-run", action="store_true", help="show what would be written and change nothing")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    settings.load_env()
    level = logging.DEBUG if settings.env_mode() == "development" else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger().setLevel(level)
    if args.command == "admin":  # must start even when config.yaml is broken: that is when it is needed
        from . import admin

        return admin.run(args.unit)
    if args.command == "import-local-sync":  # writes config.yaml, so it cannot need a valid one
        return _cmd_import(args)
    try:
        cfg = config.load_config()
        client = Client.from_env(cfg)
        return args.func(args, cfg, client)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
