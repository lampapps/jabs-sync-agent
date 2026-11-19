"""Operations shared by the command line (and later the admin console): job overview, stop, verify, reset."""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import portalocker

from . import config, endpoint, rsync, runner, schedule, settings, state
from .config import Config, ConfigError, Job
from .rsync import RsyncError, StopRequest

logger = logging.getLogger(__name__)


def format_when(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def format_last(last: Optional[dict]) -> str:
    """'ok 2027-01-15 03:00' / 'stopped ...' / 'FAILED ...' / '-' for a job's recorded last run."""
    if not last:
        return "-"
    kind = "stopped" if last.get("stopped") else ("ok" if last.get("success") else "FAILED")
    return f"{kind} {format_when(last.get('finished_at'))}"


def job_lock_info(job_id: str) -> Optional[dict]:
    """The holder's details when the job is running right now, else None. Probes only while a lock file exists."""
    path = runner.lock_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "a+", encoding="utf-8") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:
                handle.seek(0)
                try:
                    return json.load(handle) or {}
                except ValueError:
                    return {}
            portalocker.unlock(handle)
    except OSError:
        return None
    return None


def jobs_overview(cfg: Config, now: Optional[datetime] = None) -> tuple:
    """([{job, next, last, running}], errors) for every job file; `next` is a datetime or None."""
    jobs, errors = config.load_jobs(cfg)
    data = state.read()
    now = now or schedule.local_now()
    rows = [{"job": job, "next": schedule.next_run(job, now),
             "last": data.get("jobs", {}).get(job.job_id, {}).get("last_run"), "running": job_lock_info(job.job_id)}
            for job in jobs]
    return rows, errors


def stop_job(job: Job) -> bool:
    """Ask a running job to stop gracefully; False when it is not running. rsync keeps its partial file."""
    name = job.name
    info = job_lock_info(job.job_id)
    if info is None:
        return False
    pid = info.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        raise ConfigError(f"'{name}' is running but its process id is unknown; stop it by hand")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise ConfigError(f"'{name}' runs as another user (pid {pid}); run this command as that user") from exc
    return True


def tailscale_route(host: str) -> Optional[str]:
    """'direct' or 'relay' (through a DERP server, which is much slower) for a Tailscale peer, or None when the
    tailscale client is not installed or the host does not answer."""
    binary = shutil.which("tailscale")
    if not binary:
        return None
    try:
        done = subprocess.run([binary, "ping", "--c", "1", "--timeout", "5s", "--until-direct=false", host],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_tailscale_ping(done.stdout)


def parse_tailscale_ping(output: str) -> Optional[str]:
    for line in output.splitlines():
        if line.startswith("pong from"):
            return "relay" if "via DERP" in line else "direct"
    return None


def check_remotes(cfg: Config) -> list:
    """Connect to every remote and list its module root. Returns [{name, ok, detail}]."""
    results = []
    for name, remote in sorted(cfg.remotes.items()):
        where = f"{remote.host}:{remote.port}/{remote.module}"
        if remote.password_file and not os.path.isfile(remote.password_file):
            results.append({"name": name, "ok": False, "detail": f"{where}: password file not found: "
                                                                  f"{remote.password_file}"})
            continue
        started = time.monotonic()
        try:
            entries = rsync.list_entries(f"{name}:", cfg.rsync, cfg.remotes)
        except RsyncError as exc:
            results.append({"name": name, "ok": False, "detail": f"{where}: {' '.join(str(exc).split())}"})
            continue
        detail = f"{where}: connected in {time.monotonic() - started:.1f}s, {len(entries)} item(s) at the module root"
        route = tailscale_route(remote.host)
        if route == "relay":
            detail += "; Tailscale is relaying this connection (DERP), so transfers will be slow"
        elif route == "direct":
            detail += "; Tailscale connection is direct"
        results.append({"name": name, "ok": True, "detail": detail})
    return results


def _reachable(text: str, job: Job, remotes: Optional[dict]) -> bool:
    """True when a local directory can be listed or a remote directory answers."""
    parsed = endpoint.parse(text)
    if parsed.remote is None:
        return rsync.check_path_accessible(parsed.path)
    try:
        rsync.list_entries(text, job.rsync, remotes)
    except RsyncError:
        return False
    return True


def verify_job(job: Job, deep: bool = False, stop: Optional[StopRequest] = None,
               remotes: Optional[dict] = None) -> list:
    """Compare every pair of a job without syncing. Returns [{label, status, detail}] where status is
    ok, mismatch, skipped or error."""
    results = []
    for pair in job.pairs:
        label = job.label(pair)
        if not (_reachable(pair.source, job, remotes) and _reachable(pair.destination, job, remotes)):
            results.append({"label": label, "status": "skipped", "detail": "source or destination not accessible"})
            continue
        try:
            mismatches = rsync.count_diffs(pair.source, pair.destination, job.rsync, exclude=job.pair_excludes(pair),
                                           checksum=deep, stop=stop, remotes=remotes)
        except RsyncError as exc:
            results.append({"label": label, "status": "error", "detail": str(exc)})
            continue
        if mismatches:
            results.append({"label": label, "status": "mismatch", "detail": f"{mismatches} item(s) differ"})
        else:
            results.append({"label": label, "status": "ok", "detail": "0 mismatched items"})
    return results


JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TEMPLATE = Path("templates") / "job.yaml"
_JOB_ID_LINE = re.compile(r"^job_id\s*:\s*[\"']?([^\"'#\s]*)[\"']?\s*(#.*)?$", re.MULTILINE)
_ID_COMMENT = "# Identifies this job on the dashboard and in the agent's state. Never edit or copy it."


def new_job(name: str) -> Path:
    """Create config/jobs/NAME.yaml from the template, with a fresh job id and its schedules disabled."""
    if not JOB_NAME_PATTERN.match(name):
        raise ValueError("a job name uses letters, digits, '.', '_' and '-' (up to 64 characters)")
    target = settings.JOBS_DIR / f"{name}.yaml"
    if target.exists():
        raise ValueError(f"{target.name} already exists")
    template = settings.CONFIG_DIR / TEMPLATE
    if not template.exists():
        raise ValueError(f"the template {template} is missing")
    text = template.read_text(encoding="utf-8").replace('"example"', f'"{name}"')
    text = text.replace(config.NIL_JOB_ID, str(uuid.uuid4()))
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def adopt_job(path: Path) -> Optional[str]:
    """Give a job file a job_id if it has none (or only the template's placeholder); returns the new id, or None
    when it already has one. The rest of the file, comments included, is left as it is."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    new_id = str(uuid.uuid4())
    match = _JOB_ID_LINE.search(text)
    if match:
        if match.group(1) not in ("", config.NIL_JOB_ID):
            return None
        text = text[:match.start()] + f'job_id: "{new_id}"' + text[match.end():]
    else:
        lines = text.splitlines(keepends=True)
        at = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), len(lines))
        if at and not lines[at - 1].strip():
            at -= 1   # keep a comment block that precedes the first setting together with it
        lines[at:at] = [f"{_ID_COMMENT}\n", f'job_id: "{new_id}"\n', "\n"]
        text = "".join(lines)
    path.write_text(text, encoding="utf-8")
    return new_id


def adopt_jobs(files: Optional[list] = None) -> list:
    """Run adopt_job on the given files (default: every job file); returns [(path, new id)] for those changed."""
    changed = []
    for path in [Path(f) for f in files] if files else config.job_files():
        new_id = adopt_job(path)
        if new_id:
            changed.append((path, new_id))
    return changed


def delete_job(job: Job) -> None:
    """Delete a job's file from config/jobs and forget its schedule state. Synced files are never touched.

    Refuses to delete a file outside the jobs directory, or a job that is running.
    """
    path = Path(job.path).resolve()
    if path.parent != settings.JOBS_DIR.resolve() or path.suffix not in (".yaml", ".yml"):
        raise ConfigError(f"{job.path} is not a file in {settings.JOBS_DIR}; delete it by hand")
    if job_lock_info(job.job_id) is not None:
        raise ConfigError(f"'{job.name}' is running; wait for it to finish")
    path.unlink()
    try:
        with state.transaction() as data:
            data.get("jobs", {}).pop(job.job_id, None)
    except (OSError, portalocker.exceptions.LockException) as exc:
        logger.warning("deleted %s but could not clear its schedule state: %s", path.name, exc)


def reset_state() -> int:
    """Delete lock files and the schedule state; returns how many files went. Synced data and config are never touched."""
    removed = 0
    for path in [*settings.LOCK_DIR.glob("*.lock"), *settings.STATE_PATH.parent.glob("state.*")]:
        path.unlink()
        removed += 1
    if settings.LOCK_DIR.is_dir() and not any(settings.LOCK_DIR.iterdir()):
        shutil.rmtree(settings.LOCK_DIR, ignore_errors=True)
    return removed


def writable_paths(cfg: Config) -> list:
    """Directories the agent must be able to write; deploy/install.sh turns them into ReadWritePaths."""
    jobs, _ = config.load_jobs(cfg)
    wanted = {str(settings.DATA_DIR)}
    for job in jobs:
        for pair in job.pairs:
            destination = pair.destination_endpoint()
            if not destination.remote:
                wanted.add(destination.path.rstrip("/") or "/")
            if job.pair_backup_dir(pair):
                wanted.add(job.pair_backup_dir(pair).rstrip("/") or "/")
    return sorted(wanted)
