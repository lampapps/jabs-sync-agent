# JABS Sync Agent

A JABS (Just Another Backup Script) agent that mirrors directories with [rsync](https://rsync.samba.org/) on a schedule, verifies the mirror, emails failures, and can report to a JABS dashboard. It replaces `local_sync_agent`; the same job format will also cover syncing to a remote NAS.

- **Command line, config files and an admin console.** There is no GUI on the agent; everything you watch lives on the dashboard. `python -m app admin` is a full-screen console for administering it.
- **Runs standalone.** Without a dashboard it syncs, verifies and emails exactly the same. The dashboard is optional.
- **Scheduled by a systemd timer**, not cron. A timer runs `python -m app tick` every 5 minutes; the tick decides which jobs are due.
- **One way.** Each pair mirrors a source directory into a destination directory.

## Requirements

- Python 3.11 or newer with `python3-venv`
- [rsync](https://rsync.samba.org/) 3.x
- systemd, for the timer (without it, run `python -m app tick` from anything that can run a command every few minutes)
- Optional: a JABS dashboard (`dashboardv2`) to report to

## Install

Do these steps as the user that will own the install, in the install directory.

1. **Install.**

   ```bash
   ./setup.sh
   ```

   It creates the virtual environment, installs the dependencies and copies the example `config/config.yaml`, `.env` and an example job (never overwriting existing files). If `python3` is older than 3.11, choose another interpreter: `PYTHON=python3.11 ./setup.sh`. Do not run it with `sudo`.

2. **Open the admin console.** The next steps use it; the [Admin console](#admin-console) section lists every tab.

   ```bash
   .venv/bin/python -m app admin
   ```

3. **Configure** (tab `3` Files). Select `.env` and press `e` to edit it in your system editor: set the dashboard URL and key and the SMTP login if you want them (`JABS_DASHBOARD_URL`, `JABS_AGENT_KEY`, `JABS_SMTP_USERNAME`, `JABS_SMTP_PASSWORD`). Do the same for `config.yaml` (rsync defaults, email). The files document themselves, so leave what you do not need. The console checks each file when you close the editor and offers to revert a mistake.

4. **Add your jobs** (tab `1` Jobs). Press `n` to create one from the template and open it in the editor, or `e` to edit the example job. Point each pair at real directories and enable a schedule.

5. **Test email** (tab `4` Diagnostics): press `m` to send a test message. With a dashboard configured, `h` sends one heartbeat to test the link.

6. **Try a job** (tab `1` Jobs): select it and press `d` for a dry run. rsync's output streams in a window and nothing is changed.

7. **Install the timer** (quit the console first, or use another terminal):

   ```bash
   sudo deploy/install.sh
   ```

8. **Check it.** In the console, tab `5` Service shows the service and timer and `l` follows the journal; tab `4` Diagnostics `t` runs a tick now.

Every step can also be done on the command line (`.venv/bin/python -m app config`, `run JOB --dry-run`; see [Commands](#commands)).

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

`config/config.yaml` (from `config/config.example.yaml`) holds the settings shared by all jobs. A job file in `config/jobs/*.yaml` lists its source and destination pairs and can override `verify_after_sync` and any rsync option. `config/config.example.yaml` documents every global key, and `config/templates/job.yaml` lists every job setting with comments. Patterns every job skips live in `config/common_exclude.yaml`. Secrets live only in `.env`: `JABS_SMTP_USERNAME` and `JABS_SMTP_PASSWORD`, `JABS_DASHBOARD_URL`, `JABS_AGENT_KEY`, `UPTIME_KUMA_URL`, `RSYNC_BIN`, `ENV_MODE`.

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
| `admin [--unit NAME]` | Open the interactive [admin console](#admin-console) |
| `tick` | Run what is due and report to the dashboard (the timer does this) |
| `run JOB [--dry-run]` | Run one job now; `--dry-run` still reports to the dashboard |
| `stop JOB` | Ask a running job to stop gracefully; it resumes on its next run |
| `verify [JOB] [--deep]` | Compare source and destination without syncing; `--deep` reads file content (slow) |
| `jobs` | Each job's next scheduled run and last result |
| `import-local-sync [DIR] [--keep-schedules] [--force] [--dry-run]` | Convert a `local_sync_agent` install's configuration into this one (see [Migrating from local_sync_agent](#migrating-from-local_sync_agent)) |
| `testmail` | Send a test email with the SMTP settings |
| `paths` | Print the directories the agent must be able to write |
| `reset --yes` | Delete lock files and the schedule state (never synced data or your config) |

### Admin console

`.venv/bin/python -m app admin` opens a full-screen console for the person administering the agent: everything the commands above do. Switch tabs with `1` to `6` or the mouse; the footer lists the keys for the current tab; `r` refreshes and `q` quits. It starts even when `config.yaml` is broken, so you can fix it there.

| Tab | What you can do |
| --- | --- |
| 1 Jobs | Each job's pairs, schedule, next run, last result and whether it is running now. **Dry run** (`d`) or **Sync now** (`s`) runs the job in a window that streams rsync's output, with a Stop button. **Stop** (`t`) ends a job running anywhere on this machine, gracefully. Edit a job file (`e`), create one from the template (`n`) or delete one (`x`: you type its name to confirm; synced files stay where they are) |
| 2 Verify | Every pair with its paths. Quick verify (`v`), deep verify (`d`, asks first) or verify all jobs (`a`) without syncing |
| 3 Files | Check `config.yaml`, `.env`, `common_exclude.yaml` and every job file, and edit any of them in your system editor (`$VISUAL`, `$EDITOR`, else the system default such as nano). A file is validated when you leave the editor: edit again, revert or keep. The agent reads its files at the start of every run, so a valid edit applies at the next tick; if the service needs a directory it was not given (a new destination or `backup_dir`), it offers to reinstall the units. **Import from local_sync_agent** (`i`) converts an old install's configuration |
| 4 Diagnostics | Run the tick now, send a test mail, test the dashboard link (sends one heartbeat), show the schedule state, list the writable directories, reset the schedule state (typed confirmation) |
| 5 Service | State of the service and timer; start (runs a tick now), stop, enable or disable the timer, reinstall the units with the options already in use (all through `sudo`), follow the journal |
| 6 Docs | Read the install's Markdown files; links between documents work and `b` goes back |

A run started from the console is the same `python -m app run JOB` the timer starts, so it takes the same job lock (a job that is already running is refused), reports to the dashboard and emails on failure. A dry run is still reported to the dashboard, marked `[DRY RUN]`. Closing the console stops a run it started.

Run it as the user that owns the install; it refuses to start as root when the install belongs to someone else, because root-owned state files would stop the service from writing its own data. Units are found by their working directory; if that fails, pass `--unit jabs-sync-agent`. Administrative actions are recorded in the journal as `jabs-admin`. It needs the `textual` package, installed by `./setup.sh`.

### Migrating from local_sync_agent

`import-local-sync` reads the old install's `config/global.yaml`, `config/jobs/*.yaml` and `config/common_exclude.yaml` and writes them in this agent's format: the flat rsync settings move under `rsync:`, `paths` become `pairs`, and everything else carries over. The folder defaults to `../local_sync_agent`.

```bash
.venv/bin/python -m app import-local-sync --dry-run     # preview, nothing is written
.venv/bin/python -m app import-local-sync               # write the files
```

- Files that already exist here are kept unless you pass `--force`; a `config.yaml` that is still the starter `setup.sh` copied counts as unused. A converted file that does not validate is reported and not written.
- Schedules are imported **switched off** so the old and new agents never run the same job. Add `--keep-schedules` to import them as they were.
- Settings this agent does not use are listed as warnings. The SMTP login is not in the YAML: copy `JABS_SMTP_USERNAME` and `JABS_SMTP_PASSWORD` (and the dashboard URL and key) from the old `.env`.

To cut over: import, run each job with `run JOB --dry-run`, then enable its schedule here, remove the old agent's cron entry and keep the old install until a scheduled run has succeeded.

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

Set `JABS_DASHBOARD_URL` and `JABS_AGENT_KEY` in `.env` (register the agent on the dashboard host with `.venv/bin/python -m app agent add NAME`). Leave them empty, or set `dashboard.enabled: false` in `config.yaml`, to run standalone; `.venv/bin/python -m app config` says which mode is active.

The dashboard receives each run's start, stages, progress about every 5 seconds, completion and verify result, plus a heartbeat with each job's schedule on every tick. Every request carries the machine's **hostname** (`hostname:` in `config.yaml`, else the system name). Dashboard problems are logged and never change a sync's result.

## Email alerts

Set `email.smtp_server` and `email.to_addrs` in `config.yaml` and the login in `.env`, then send a test with `.venv/bin/python -m app testmail`. By default a failed pair and a verify mismatch send a message (`notify_on.error`); `notify_on.backup_complete` adds one per finished pair. A stop sends nothing.

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
