"""Operations shared by the command line (and later the admin console): job overview, stop, verify, reset."""

import json
import logging
import os
import shutil
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

import portalocker

from . import config, rsync, runner, schedule, settings, state
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


def job_lock_info(name: str) -> Optional[dict]:
    """The holder's details when the job is running right now, else None. Probes only while a lock file exists."""
    path = runner.lock_path(name)
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
             "last": data.get("jobs", {}).get(job.name, {}).get("last_run"), "running": job_lock_info(job.name)}
            for job in jobs]
    return rows, errors


def stop_job(name: str) -> bool:
    """Ask a running job to stop gracefully; False when it is not running. rsync keeps its partial file."""
    info = job_lock_info(name)
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


def verify_job(job: Job, deep: bool = False, stop: Optional[StopRequest] = None) -> list:
    """Compare every pair of a job without syncing. Returns [{label, status, detail}] where status is
    ok, mismatch, skipped or error."""
    results = []
    for pair in job.pairs:
        label = job.label(pair)
        if not (rsync.check_path_accessible(pair.source) and rsync.check_path_accessible(pair.destination)):
            results.append({"label": label, "status": "skipped", "detail": "source or destination not accessible"})
            continue
        try:
            mismatches = rsync.count_diffs(pair.source, pair.destination, job.rsync, exclude=job.pair_excludes(pair),
                                           checksum=deep, stop=stop)
        except RsyncError as exc:
            results.append({"label": label, "status": "error", "detail": str(exc)})
            continue
        if mismatches:
            results.append({"label": label, "status": "mismatch", "detail": f"{mismatches} item(s) differ"})
        else:
            results.append({"label": label, "status": "ok", "detail": "0 mismatched items"})
    return results


def delete_job(job: Job) -> None:
    """Delete a job's file from config/jobs and forget its schedule state. Synced files are never touched.

    Refuses to delete a file outside the jobs directory, or a job that is running.
    """
    path = Path(job.path).resolve()
    if path.parent != settings.JOBS_DIR.resolve() or path.suffix not in (".yaml", ".yml"):
        raise ConfigError(f"{job.path} is not a file in {settings.JOBS_DIR}; delete it by hand")
    if job_lock_info(job.name) is not None:
        raise ConfigError(f"'{job.name}' is running; wait for it to finish")
    path.unlink()
    try:
        with state.transaction() as data:
            data.get("jobs", {}).pop(job.name, None)
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
            wanted.add(pair.destination.rstrip("/") or "/")
            if job.pair_backup_dir(pair):
                wanted.add(job.pair_backup_dir(pair).rstrip("/") or "/")
    return sorted(wanted)
