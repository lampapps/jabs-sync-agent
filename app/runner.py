"""Run one sync job: lock, mirror each pair, verify, and report to the dashboard, email and Uptime Kuma."""

import contextlib
import logging
import os
import signal
import threading
import time
import uuid
from typing import Iterator, Optional

from . import kuma, locking, mailer, rsync, settings, state
from .config import Config, Job, Pair
from .dashboard import Client
from .rsync import RsyncError, StopRequest

PROGRESS_POST_INTERVAL_S = 5
GIB = 1024 ** 3

logger = logging.getLogger(__name__)


class _PairLogger(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"{self.extra['label']} - {msg}", kwargs


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def lock_path(job_name: str) -> str:
    return str(settings.LOCK_DIR / f"{safe_name(job_name)}.lock")


@contextlib.contextmanager
def stop_on_signals(stop: StopRequest) -> Iterator[None]:
    """While active, SIGTERM and SIGINT end the running rsync gracefully instead of killing the agent."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.signal(sig, lambda *_: stop.request()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_job(job: Job, cfg: Config, client: Client, dry_run: bool = False, stop: Optional[StopRequest] = None) -> bool:
    """Run every pair of one job; True unless a pair failed. A stop is not a failure.

    Monitoring, email and Uptime Kuma never change the outcome.
    """
    stop = stop or StopRequest()
    try:
        lock_file = locking.acquire_lock(lock_path(job.name))
    except RuntimeError as exc:
        logger.error("%s - could not start: %s", job.name, exc)
        return False
    start = time.time()
    try:
        result = _run_pairs(job, cfg, client, dry_run, stop, start)
    finally:
        locking.release_lock(lock_file)

    state.record_run(job.name, {**result, "dry_run": dry_run})
    outcome = "stopped" if result["stopped"] else ("OK" if result["success"] else "FAILED")
    kuma.ping("up" if result["success"] else "down", f"{'[DRY RUN] ' if dry_run else ''}{job.name}: {outcome}",
              dry_run=dry_run)
    return result["success"]


def _run_pairs(job: Job, cfg: Config, client: Client, dry_run: bool, stop: StopRequest, start: float) -> dict:
    job_run_id = str(uuid.uuid4())
    outcomes = []
    for pair in job.pairs:
        if stop.requested:
            outcomes.append(_skip_stopped(job, pair, client, dry_run, job_run_id))
        else:
            outcomes.append(_sync_pair(job, pair, cfg, client, dry_run, stop, job_run_id))
    failed = [o for o in outcomes if not o["success"]]
    return {
        "success": not failed, "stopped": any(o["stopped"] for o in outcomes), "finished_at": time.time(),
        "duration": time.time() - start, "error": failed[0]["error"] if failed else None, "pairs": outcomes,
    }


def _skip_stopped(job: Job, pair: Pair, client: Client, dry_run: bool, job_run_id: str) -> dict:
    """A pair that never started because a stop came first; it is finalized so the dashboard does not show it running."""
    label, run_id = job.label(pair), str(uuid.uuid4())
    client.send_sync_start(job.name, label, label, pair.source, pair.destination, run_id=run_id,
                           job_run_id=job_run_id, dry_run=dry_run)
    client.send_sync_complete(job.name, label, label, 0.0, run_id=run_id, job_run_id=job_run_id, stopped=True,
                              dry_run=dry_run)
    logger.info("%s - not started: the run was stopped", label)
    return {"name": pair.resolved_name(), "success": True, "stopped": True, "error": None, "files": 0, "bytes": 0}


def _sync_pair(job: Job, pair: Pair, cfg: Config, client: Client, dry_run: bool, stop: StopRequest,
               job_run_id: str) -> dict:
    """Mirror one pair and verify it. A failure here never stops the pairs after it."""
    # target_id and target_label stay the same on every run of a pair so the dashboard groups its runs together.
    label = job.label(pair)
    log = _PairLogger(logger, {"label": label})
    run_id, start = str(uuid.uuid4()), time.time()
    tag = "[DRY RUN] " if dry_run else ""
    outcome = {"name": pair.resolved_name(), "success": False, "stopped": False, "error": None, "files": 0, "bytes": 0}

    def fail(message: str) -> dict:
        duration = time.time() - start
        log.error("%s%s", tag, message)
        client.send_sync_complete(job.name, label, label, duration, run_id=run_id, job_run_id=job_run_id,
                                  success=False, error_message=message, dry_run=dry_run)
        mailer.notify(cfg.email, "error", f"JABS sync FAILED: {label}",
                      f"{tag}Sync of {label} failed after {duration:.1f}s: {message}")
        return {**outcome, "error": message}

    client.send_sync_start(job.name, label, label, pair.source, pair.destination, run_id=run_id,
                           job_run_id=job_run_id, dry_run=dry_run)
    log.info("###### %sStarting sync: %s (%s) ######", tag, label, settings.machine_hostname(cfg.hostname))
    log.info("  Source : %s", pair.source)
    log.info("  Dest   : %s", pair.destination)

    if not rsync.check_path_accessible(pair.source):
        return fail(f"Source directory not accessible: {pair.source}")
    if not dry_run:
        try:
            os.makedirs(pair.destination, exist_ok=True)
        except OSError as exc:
            return fail(f"Destination cannot be created: {pair.destination} ({exc})")
    elif not rsync.check_path_accessible(pair.destination):
        log.warning("Destination does not exist yet (dry run, not creating it): %s", pair.destination)

    if job.rsync.min_free_bytes and not dry_run:
        free = rsync.free_space_bytes(pair.destination)
        if free is not None and free < job.rsync.min_free_bytes:
            return fail(f"Destination has only {free / GIB:.1f} GiB free "
                        f"(minimum {job.rsync.min_free_bytes / GIB:.1f} GiB): {pair.destination}")

    backup_root = job.pair_backup_dir(pair)
    pair_backup_root = os.path.join(backup_root, safe_name(label)) if backup_root else ""
    run_backup_dir = rsync.run_backup_dir(pair_backup_root, start) if pair_backup_root else ""
    if run_backup_dir and not dry_run:
        try:
            os.makedirs(run_backup_dir, exist_ok=True)
        except OSError as exc:
            return fail(f"backup_dir not accessible: {run_backup_dir} ({exc})")

    client.send_sync_stage(job.name, label, label, "Running sync", run_id=run_id, dry_run=dry_run)
    progress = {"last_post": 0.0, "last_decile": -1}

    def on_progress(parsed: dict) -> None:
        percent, rate = parsed["percent"], parsed["bytes_per_second"]
        if percent // 10 > progress["last_decile"]:
            progress["last_decile"] = percent // 10
            log.info("Progress: %d%% (%.1f MB/s)", percent, rate / 1024 ** 2)
        now = time.time()
        if now - progress["last_post"] >= PROGRESS_POST_INTERVAL_S:
            progress["last_post"] = now
            client.send_sync_progress(job.name, label, run_id=run_id, percent_complete=percent,
                                      bytes_per_second=rate)

    try:
        result = rsync.sync(pair.source, pair.destination, job.rsync, exclude=job.pair_excludes(pair),
                            backup_dir=run_backup_dir, dry_run=dry_run, on_progress=on_progress, stop=stop)
    except RsyncError as exc:
        return fail(f"Sync failed: {exc}")

    duration = time.time() - start
    outcome.update(files=result["files_transferred"], bytes=result["bytes_transferred"])
    if result["stopped"]:
        log.info("%sSync stopped after %.1fs; it resumes on the next run", tag, duration)
        client.send_sync_complete(job.name, label, label, duration, run_id=run_id, job_run_id=job_run_id,
                                  files_backed_up=result["files_transferred"],
                                  bytes_backed_up=result["bytes_transferred"], stopped=True, dry_run=dry_run)
        return {**outcome, "success": True, "stopped": True}

    if result["partial"]:
        log.warning("%sSync completed with warnings (partial transfer, exit %s)", tag, result["returncode"])
    else:
        log.info("%sSync complete: files=%s bytes=%s duration=%.1fs", tag, result["files_transferred"],
                 result["bytes_transferred"], duration)
    client.send_sync_complete(job.name, label, label, duration, run_id=run_id, job_run_id=job_run_id,
                              files_backed_up=result["files_transferred"],
                              bytes_backed_up=result["bytes_transferred"], dry_run=dry_run)
    mailer.notify(cfg.email, "backup_complete", f"JABS sync complete: {label}",
                  f"{tag}Sync of {label} completed in {duration:.1f}s ({result['files_transferred']} files transferred).")
    outcome["success"] = True

    if job.verify_after_sync and not dry_run:
        _quick_verify(job, pair, cfg, client, log, run_id, stop)
    if pair_backup_root and not dry_run:
        try:
            removed = rsync.prune_backup_dir(pair_backup_root, job.rsync.backup_dir_retention_days)
            if removed:
                log.info("Pruned %d old backup_dir run(s) older than %dd", removed,
                         job.rsync.backup_dir_retention_days)
        except OSError as exc:
            log.warning("backup_dir retention pruning failed: %s", exc)
    return outcome


def _quick_verify(job: Job, pair: Pair, cfg: Config, client: Client, log, run_id: str, stop: StopRequest) -> None:
    """Size and mtime comparison after a sync. A failing verify is reported, but the sync that finished stays good."""
    label = job.label(pair)
    try:
        log.info("Running quick verify (size/mtime only)")
        client.send_sync_stage(job.name, label, label, "Verifying (quick)", run_id=run_id)
        mismatches = rsync.count_diffs(pair.source, pair.destination, job.rsync, exclude=job.pair_excludes(pair),
                                       stop=stop)
    except RsyncError as exc:
        if stop.requested:
            log.info("Quick verify interrupted by the stop request")
            return
        log.error("Quick verify FAILED to run: %s", exc)
        client.send_check_result(job.name, label, label, run_id, success=False, error_message=str(exc))
        return
    if mismatches == 0:
        log.info("Quick verify passed")
        client.send_check_result(job.name, label, label, run_id, success=True)
        return
    log.warning("Quick verify found %d mismatched item(s)", mismatches)
    client.send_check_result(job.name, label, label, run_id, success=False, mismatch_count=mismatches,
                             error_message=f"{mismatches} item(s) differ between source and destination")
    mailer.notify(cfg.email, "error", f"JABS sync quick verify found mismatches: {label}",
                  f"Quick verify after sync of {label} found {mismatches} mismatched item(s).\n"
                  f"Source: {pair.source}\nDestination: {pair.destination}")
