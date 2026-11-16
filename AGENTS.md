# JABS Sync Agent

Mirrors directories with rsync on a systemd timer; optionally reports to a JABS dashboard. Standalone-capable: everything works without a dashboard. Command line and config files only for now (an admin console is planned). It replaces `local_sync_agent` and will replace `nas_sync_agent`. The shared JABS rules (API, secrets, `VERSION`, tests, README layout) are in [../dashboardv2/AGENTS.md](../dashboardv2/AGENTS.md); read its "JABS-wide conventions" section first.

## Rules

- The dashboard is optional. `app/dashboard.py` is the only module that talks to it, and every method is a no-op when `JABS_DASHBOARD_URL` or `JABS_AGENT_KEY` is unset (or `dashboard.enabled: false`). A dashboard or email failure never changes a sync's result and never raises.
- `app/rsync.py` is the only module that runs the `rsync` binary. Never put a credential on a command line or in a log.
- API contract with the dashboard: the v1 endpoints `/api/monitoring/events` and `/api/monitoring/target-purged`. Every payload carries `hostname`. See `../dashboardv2/docs/AGENTS_API_GUIDE.md`. Each pair is its own target (`job_name:pair name`) and keeps the same `target_id` on every run; one `job_run_id` is shared by all pairs of a run.
- A stop (`stop`, `SIGTERM`, `Ctrl-C`) is not an error: report it as `stopped`, send no alert, and let the next run resume through `--partial-dir`. A stop must never leave a pair showing as running.
- Never delete or overwrite data outside a pair's destination and its `backup_dir`. A source and a destination must not contain each other (`--delete` would destroy data).
- Scheduling is by systemd timer running `python -m app tick`; there is no cron and no daemon. Due-ness is state based (`app/schedule.py`), so it is correct at any tick interval.

## Stack and layout

- Python 3.11+, pydantic (config only), PyYAML, croniter, portalocker, requests, python-dotenv, textual (reserved for the admin console). Do not add dependencies without asking.
- `app/__main__.py` is the CLI (`python -m app <command>`); prefer a new subcommand over a shell script. Shell is only `setup.sh` and `deploy/install.sh`.
- `app/config.py`: pydantic models for `config/config.yaml` and `config/jobs/*.yaml` (`pairs`, `rsync` options); `settings.py` holds paths and env access, read at call time.
- `app/runner.py` runs one job; `app/tick.py` is the periodic work (isolated tasks); `app/state.py` is `data/state.json`; `app/ops.py` holds what the CLI (and later the console) share; `app/mailer.py`, `app/kuma.py`, `app/locking.py`.
- Systemd units are templates in `deploy/`, rendered by `deploy/install.sh`.
- Config examples are `config/config.example.yaml` and `config/templates/job.yaml`; keep them listing every setting, with comments (`tests/test_config.py` checks).

## Conventions

- PEP8, `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`, type hints on signatures.
- Narrow exceptions only; a broad `except Exception` needs `# pylint: disable=broad-except` and a reason. No `sys.exit()` outside `__main__`.
- Logging goes to stderr (journald); do not add log files. Level is DEBUG when `ENV_MODE=development`. Use `logging.getLogger(__name__)`.
- Secrets only via `.env`.
- The agent's version is the `MAJOR.MINOR.PATCH` in the root `VERSION` file (read by `app/settings.py`); never write it anywhere else. Every commit bumps it and starts its message with the new number (see `../dashboardv2/AGENTS.md`).

## Testing

- pytest (`requirements-dev.txt`): `.venv/bin/python -m pytest -q`. Tests use the `home` fixture so the real config, `.env` and state are never touched; the rsync tests run the real binary on temporary directories and are skipped when rsync is missing.
- Add or update tests with every non-trivial change.
