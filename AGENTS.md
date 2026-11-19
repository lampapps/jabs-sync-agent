# JABS Sync Agent

Mirrors directories with rsync on a systemd timer; optionally reports to a JABS dashboard. Standalone-capable: everything works without a dashboard. Command line, config files and a Textual admin console. It replaces `local_sync_agent` and `nas_sync_agent`. The shared JABS rules (API, secrets, `VERSION`, tests, README layout) are in [../dashboardv2/AGENTS.md](../dashboardv2/AGENTS.md); read its "JABS-wide conventions" section first.

## Rules

- The dashboard is optional. `app/dashboard.py` is the only module that talks to it, and every method is a no-op when `JABS_DASHBOARD_URL` or `JABS_AGENT_KEY` is unset (or `dashboard.enabled: false`). A dashboard or email failure never changes a sync's result and never raises.
- `app/rsync.py` is the only module that runs the `rsync` binary. Never put a credential on a command line or in a log.
- API contract with the dashboard: `POST /api/v2/heartbeat`, `/api/v2/events` and `/api/v2/jobs` (one target per pair). Every payload carries `hostname`. See `../dashboardv2/docs/AGENTS_API_GUIDE.md`. Identity is by id: every event and jobs entry carries the job's `job_id`; a pair is a target identified by its name within the job (`target_id` = the pair's resolved name, so never rename one casually) and `job_name`/`target_label` (`job_name:pair name`) are labels only. One `job_run_id` is shared by all pairs of a run. State and lock files are keyed by `job_id`, never by a name. A job file must have a valid, unique `job_id`: the loader rejects one without (naming `job adopt`) and the template's nil-UUID placeholder.
- A stop (`stop`, `SIGTERM`, `Ctrl-C`) is not an error: report it as `stopped`, send no alert, and let the next run resume through `--partial-dir`. A stop must never leave a pair showing as running.
- Never delete or overwrite data outside a pair's destination and its `backup_dir`. A source and a destination must not contain each other (`--delete` would destroy data), and no pair of a job may write where another reads or writes (`endpoint.overlap`, checked in `config.py`).
- A pair side is a local absolute path or `remote:path` (`app/endpoint.py`); remotes are rsync daemons declared in `config.yaml`. Never put a remote's password anywhere but its `password_file` (`--password-file`), and never allow a remote on both sides of a pair. Keep the delete guards (`refuse_empty_source`, `max_delete`): a dropped mount must not wipe a destination.
- `stop_at` is a per-job deadline that stops only its own job (a linked `StopRequest`) and is reported as `stopped`; `schedule.due_jobs` skips a slot whose window has already closed.
- Scheduling is by systemd timer running `python -m app tick`; there is no cron and no daemon. Due-ness is state based (`app/schedule.py`), so it is correct at any tick interval.

## Stack and layout

- Python 3.11+, pydantic (config only), PyYAML, croniter, portalocker, requests, python-dotenv, textual (admin console only). Do not add dependencies without asking.
- `app/__main__.py` is the CLI (`python -m app <command>`); prefer a new subcommand over a shell script. Shell is only `setup.sh` and `deploy/install.sh`.
- `app/admin/` is the Textual admin console (`python -m app admin`). Every CLI subcommand must also be doable there: add it to `admin.COVERAGE` and register the action id in a tab's `ACTIONS` (`TestParity` in `tests/test_admin_support.py` fails otherwise). Keep logic in `app/ops.py` so the CLI and console share it; runs and verifies go through `python -m app ...` child processes so they behave exactly like the CLI. Textual is imported lazily so `tick` and `run` never load it.
- `app/config.py`: pydantic models for `config/config.yaml` and `config/jobs/*.yaml` (`pairs`, `rsync` options); `settings.py` holds paths and env access, read at call time.
- `app/runner.py` runs one job; `app/tick.py` is the periodic work (isolated tasks); `app/state.py` is `data/state.json`; `app/ops.py` holds what the CLI and the console share; `app/mailer.py`, `app/kuma.py`, `app/locking.py`.
- Systemd units are templates in `deploy/`, rendered by `deploy/install.sh`.
- Config examples are `config/config.example.yaml` and `config/templates/job.yaml`; keep them listing every setting, with comments (`tests/test_config.py` checks).

## Conventions

- PEP8, `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`, type hints on signatures.
- Narrow exceptions only; a broad `except Exception` needs `# pylint: disable=broad-except` and a reason. No `sys.exit()` outside `__main__`.
- Logging goes to stderr (journald); do not add log files. Level is DEBUG when `ENV_MODE=development`. Use `logging.getLogger(__name__)`.
- Secrets only via `.env`.
- The agent's version is the `MAJOR.MINOR.PATCH` in the root `VERSION` file (read by `app/settings.py`); never write it anywhere else. Every commit bumps it and starts its message with the new number (see `../dashboardv2/AGENTS.md`).

## Testing

- pytest (`requirements-dev.txt`): `.venv/bin/python -m pytest -q`. Tests use the `home` fixture so the real config, `.env` and state are never touched; the rsync tests run the real binary on temporary directories and a real `rsync --daemon` on loopback (`tests/test_remotes.py`), and are skipped when rsync is missing.
- Add or update tests with every non-trivial change.
