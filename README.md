# JABS Sync Agent

A JABS (Just Another Backup Script) agent that mirrors directories with [rsync](https://rsync.samba.org/) on a schedule, verifies the mirror, emails failures, and can report to a JABS dashboard. It replaces `local_sync_agent`; the same job format will also cover syncing to a remote NAS.

- **Command line and config files only.** Everything you watch lives on the dashboard.
- **Runs standalone.** Without a dashboard it syncs, verifies and emails exactly the same. The dashboard is optional.
- **Scheduled by a systemd timer**, not cron. A timer runs `python -m app tick` every 5 minutes; the tick decides which jobs are due.
- **One way.** Each pair mirrors a source directory into a destination directory.

## Requirements

- Python 3.11 or newer with `python3-venv`
- [rsync](https://rsync.samba.org/) 3.x
- systemd, for the timer (without it, run `python -m app tick` from anything that can run a command every few minutes)
- Optional: a JABS dashboard (`dashboardv2`) to report to

## Install

```bash
./setup.sh
```

It creates the virtual environment, installs the dependencies and copies the example `config/config.yaml`, `.env` and an example job (never overwriting existing files). If `python3` is older than 3.11, choose another interpreter: `PYTHON=python3.11 ./setup.sh`. Run it as the user that will own the install, not with `sudo`.

Then:

1. Edit `.env` if you want a dashboard (`JABS_DASHBOARD_URL`, `JABS_AGENT_KEY`) or email (`JABS_SMTP_USER`, `JABS_SMTP_PASS`), and `config/config.yaml` for the defaults.
2. Add jobs in `config/jobs/*.yaml` by copying `config/templates/job.yaml`. Point each pair at real directories and enable a schedule.
3. Try a job without changing anything:

   ```bash
   .venv/bin/python -m app run JOB --dry-run
   ```

4. Install the timer:

   ```bash
   sudo deploy/install.sh
   ```

### What `deploy/install.sh` does

It installs a oneshot service and a timer (`jabs-sync-agent.service` and `.timer`), runs the config check as the service user, and starts the timer. The service is hardened (`ProtectSystem=strict`, `NoNewPrivileges`, private `/tmp`) and may write only to `data/`, each pair's destination and each `backup_dir`, which it reads from your config with `python -m app paths`. **Run the script again after adding a destination or a `backup_dir`.**

| Option | Meaning |
| --- | --- |
| `--user NAME` | Run as this user (default: the user who ran `sudo`). `root` is allowed, with a warning. |
| `--read-all` | Let a non-root user read files it does not own (`CAP_DAC_READ_SEARCH`), to sync system directories or other users' homes. |
| `--name UNIT` | Unit name (default `jabs-sync-agent`). |
| `--no-start` | Install and enable the timer without starting it. |
| `--render-only DIR` | Write the unit files into DIR and change nothing on the system. |

```bash
systemctl status jabs-sync-agent.timer
journalctl -u jabs-sync-agent -f          # logs
sudo systemctl start jabs-sync-agent      # run a tick now
```

## Configuration

`config/config.yaml` (from `config/config.example.yaml`) holds the settings shared by all jobs. A job file in `config/jobs/*.yaml` lists its source and destination pairs and can override `verify_after_sync` and any rsync option. `config/config.example.yaml` documents every global key, and `config/templates/job.yaml` lists every job setting with comments. Patterns every job skips live in `config/common_exclude.yaml`. Secrets live only in `.env`: `JABS_SMTP_USER` and `JABS_SMTP_PASS`, `JABS_DASHBOARD_URL`, `JABS_AGENT_KEY`, `UPTIME_KUMA_URL`, `RSYNC_BIN`, `ENV_MODE`.

Blank values in YAML (`backup_dir:`) mean "use the default". Check the files with `.venv/bin/python -m app config`.

## Usage

### Scheduling

Each job lists schedules as 5-field cron expressions (`schedules: [{cron: "0 2 * * *", enabled: true}]`). They are evaluated by the agent, and no cron daemon is involved.

- The agent remembers, per job and expression, the latest scheduled time it has served. A newer slot is due.
- **A missed run happens once at the next tick**, for example after the machine was off or while another sync was still running. Several missed slots still mean one run.
- A job seen for the first time only gets a baseline, so installing never starts a surprise sync.
- Several schedules of one job that are due together run it once.
- Jobs run one after another within a tick, and a job already running (a manual `run`) is skipped.

The tick every 5 minutes means a job starts up to 5 minutes after its scheduled time. For tighter timing change `OnCalendar` in `deploy/agent.timer` and run `deploy/install.sh` again.

### Commands

Run as `.venv/bin/python -m app <command>`.

| Command | Purpose |
| --- | --- |
| `config` | Validate `config.yaml` and the job files; shows whether the dashboard is in use |
| `tick` | Run what is due and report to the dashboard (the timer does this) |
| `run JOB [--dry-run]` | Run one job now; `--dry-run` still reports to the dashboard |
| `stop JOB` | Ask a running job to stop gracefully; it resumes on its next run |
| `verify [JOB] [--deep]` | Compare source and destination without syncing; `--deep` reads file content (slow) |
| `jobs` | Each job's next scheduled run and last result |
| `testmail` | Send a test email with the SMTP settings |
| `paths` | Print the directories the agent must be able to write |
| `reset --yes` | Delete lock files and the schedule state (never synced data or your config) |

### Verifying a mirror

A cheap quick verify (an rsync dry run comparing size and mtime only, no file content read) runs after every pair's sync unless `verify_after_sync` is false. `verify --deep` reads and compares file content on both sides, so it is slow and never runs automatically. A failing verify is reported and emailed but does not fail the sync that just finished.

### Backup directory

With `backup_dir` set, a file the sync would overwrite or delete on the destination is moved to `<backup_dir>/<job_pair>/<YYYYMMDD_HHMMSS>/` instead of being lost. `backup_dir_retention_days` removes those per-run directories after the given age (checked after each real sync); 0 keeps them forever. A pair can set its own `backup_dir`.

## How it works

- **A job is a list of pairs.** Each pair mirrors one source into one destination with `rsync` and is reported to the dashboard as its own job (`job_name:pair name`). A failing pair never stops the pairs after it.
- **Defaults suit a LAN or NFS mount:** whole-file copies, no ownership, a 300 second idle timeout, resumable partial files (`--partial-dir`), and `--delete` so the destination is an exact mirror. Set `delete: false` for an append-only copy.
- **Safety:** a source and a destination may not contain each other, a missing or unreadable source fails the pair before rsync starts, and `min_free_bytes` refuses to sync into a nearly full destination.
- **Stopping is not an error.** `stop JOB`, `SIGTERM` and `Ctrl-C` end the running rsync gracefully. rsync keeps its partial file, the dashboard shows the run as `stopped`, no alert is sent, and the next run carries on. Pairs that had not started are finalized as stopped.
- **No database.** The agent keeps a lock file per job and a small `data/state.json` (which schedule slots were served and each job's last result).
- Exit codes 23 and 24 from rsync (some files skipped or vanished) are a warning, not a failure.

## Dashboard integration (optional)

Set `JABS_DASHBOARD_URL` and `JABS_AGENT_KEY` in `.env` (register the agent on the dashboard host with `python -m app agent add NAME`). Leave them empty, or set `dashboard.enabled: false` in `config.yaml`, to run standalone; `python -m app config` says which mode is active.

The dashboard receives each run's start, stages, progress about every 5 seconds, completion and verify result, plus a heartbeat with each job's schedule on every tick. Every request carries the machine's **hostname** (`hostname:` in `config.yaml`, else the system name). Dashboard problems are logged and never change a sync's result.

## Email alerts

Set `email.smtp_server` and `email.to_addrs` in `config.yaml` and the login in `.env`, then send a test with `python -m app testmail`. By default a failed pair and a verify mismatch send a message (`notify_on.error`); `notify_on.backup_complete` adds one per finished pair. A stop sends nothing.

## Logs and troubleshooting

The agent logs to stderr, which systemd captures in the journal. It writes no log files.

```bash
journalctl -u jabs-sync-agent -f                  # follow live
journalctl -u jabs-sync-agent -p warning          # warnings and errors only
```

Set `ENV_MODE=development` in `.env` for DEBUG level.

| Symptom | Likely cause |
| --- | --- |
| `could not start: Could not acquire lock` | Another run of the same job is still going (a manual `run` or the previous tick) |
| `Source directory not accessible` | A mount dropped, or the path in the job is wrong |
| `Destination has only N GiB free` | `min_free_bytes` is set and the destination is nearly full |
| `Sync completed with warnings (partial transfer)` | rsync hit vanished or unreadable files mid-run (exit 23 or 24) |
| `rsync binary not found` | Install rsync, or set `RSYNC_BIN` in `.env` |
| Quick verify keeps finding mismatches | Something else writes to the destination, or `delete` differs from the sync |
| The service cannot write to a new destination | Run `sudo deploy/install.sh` again so the sandbox allows it |

## Security

No encryption or authentication is used for the transfer: source and destination are local or NFS-mounted paths on a trusted LAN. The dashboard key and SMTP login are kept in `.env`, which `setup.sh` makes owner-only and git ignores. The service runs as an unprivileged user in a read-only sandbox that can write only the directories listed by `python -m app paths`.

## Development

```bash
./setup.sh --dev
.venv/bin/python -m pytest -q
```

The tests that run rsync are skipped when rsync is not installed. See [AGENTS.md](AGENTS.md) for the layout and conventions.

## License

MIT, see [LICENSE.md](LICENSE.md).
