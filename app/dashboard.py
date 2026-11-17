"""The only place that talks to the JABS dashboard. Every method is a no-op returning False in standalone mode."""

import logging
import time
from typing import Optional

import requests

from . import settings
from .config import Config

logger = logging.getLogger(__name__)

# After a connection failure, skip the dashboard this long so a down dashboard cannot stall a sync.
COOLDOWN_S = 30

BACKUP_TYPE = "sync"
VERIFY_STAGE = "Verify (quick)"


class Client:
    def __init__(self, url: str = "", key: str = "", hostname: str = ""):
        self.url = url.rstrip("/")
        self.key = key
        self.hostname = hostname
        self._cooldown_until = 0.0

    @classmethod
    def from_env(cls, cfg: Config) -> "Client":
        """A client for the configured dashboard, or a disabled one when running standalone."""
        hostname = settings.machine_hostname(cfg.hostname)
        url, key = settings.dashboard_url(), settings.agent_key()
        if not cfg.dashboard.enabled or not (url and key):
            return cls(hostname=hostname)
        return cls(url, key, hostname)

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def describe(self) -> str:
        if self.enabled:
            return f"reporting to {self.url} as host '{self.hostname}'"
        return "standalone (no dashboard)"

    def _post(self, path: str, payload: dict) -> Optional[requests.Response]:
        """POST JSON to the dashboard; None when disabled, cooling down or unreachable."""
        if not self.enabled or time.monotonic() < self._cooldown_until:
            return None
        try:
            return requests.post(f"{self.url}{path}", json=payload, headers={"X-API-Key": self.key},
                                 timeout=settings.DASHBOARD_TIMEOUT_S)
        except requests.exceptions.RequestException as exc:
            self._cooldown_until = time.monotonic() + COOLDOWN_S
            logger.debug("dashboard request %s failed: %s", path, exc)
            return None

    def _post_ok(self, path: str, payload: dict, ok: tuple = (200, 201), quiet: tuple = ()) -> bool:
        response = self._post(path, payload)
        if response is None:
            return False
        if response.status_code in ok:
            return True
        level = logging.DEBUG if response.status_code in quiet else logging.WARNING
        logger.log(level, "dashboard rejected %s: %s %s", path, response.status_code, response.text[:200])
        return False

    # ------------------------------------------------------------ events

    def send_event(self, event_type: str, message: str, run_id=None, job_run_id=None, target_id=None, job_name=None,
                   target_label=None, stage=None, status=None, duration_seconds=None, files_backed_up=None,
                   bytes_backed_up=None, percent_complete=None, bytes_per_second=None, source=None,
                   destination=None, error_message=None, cron_schedule=None) -> bool:
        if not self.enabled:
            return False
        payload = {
            "version": settings.VERSION, "agent_type": settings.AGENT_TYPE, "hostname": self.hostname,
            "event_type": event_type, "message": message, "timestamp": int(time.time()),
        }
        optional = {
            "run_id": run_id, "job_run_id": job_run_id, "target_id": target_id, "job_name": job_name,
            "backup_type": BACKUP_TYPE if target_id else None, "target_label": target_label, "stage": stage,
            "status": status, "duration_seconds": duration_seconds, "files_backed_up": files_backed_up,
            "bytes_backed_up": bytes_backed_up, "percent_complete": percent_complete,
            "bytes_per_second": bytes_per_second, "source": source, "destination": destination,
            "error_message": error_message, "cron_schedule": cron_schedule,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return self._post_ok("/api/monitoring/events", payload)

    @staticmethod
    def _tag(message: str, dry_run: bool) -> str:
        return f"[DRY RUN] {message}" if dry_run else message

    def send_sync_start(self, job_name, target_id, target_label, source, destination, run_id=None, job_run_id=None,
                        dry_run=False) -> bool:
        return self.send_event(
            "heartbeat", self._tag(f"Starting sync for {job_name}", dry_run), run_id=run_id, job_run_id=job_run_id,
            target_id=target_id, job_name=job_name, target_label=target_label, source=source,
            destination=destination, stage="Starting sync")

    def send_sync_stage(self, job_name, target_id, target_label, stage, run_id=None, dry_run=False) -> bool:
        return self.send_event(
            "heartbeat", self._tag(f"Sync {job_name}: {stage}", dry_run), run_id=run_id, target_id=target_id,
            job_name=job_name, target_label=target_label, stage=stage)

    def send_sync_progress(self, job_name, target_id, run_id=None, percent_complete=None,
                           bytes_per_second=None) -> bool:
        """Mid-run progress; it has no duration, so the dashboard never treats it as the end of the run."""
        return self.send_event(
            "heartbeat", "Sync in progress", run_id=run_id, target_id=target_id, job_name=job_name,
            percent_complete=percent_complete, bytes_per_second=bytes_per_second)

    def send_sync_complete(self, job_name, target_id, target_label, duration_seconds, run_id=None, job_run_id=None,
                           files_backed_up=0, bytes_backed_up=0, success=True, stopped=False, error_message=None,
                           dry_run=False) -> bool:
        """The run's final event. A stop is reported as 'stopped': finished, but expected to resume, not an error."""
        if success:
            return self.send_event(
                "backup_complete", self._tag("Sync stopped, it resumes on the next run" if stopped else "Sync complete",
                                             dry_run),
                run_id=run_id, job_run_id=job_run_id, target_id=target_id, job_name=job_name,
                target_label=target_label, stage="Stopped" if stopped else "Completed",
                status="stopped" if stopped else "success", duration_seconds=duration_seconds,
                files_backed_up=files_backed_up, bytes_backed_up=bytes_backed_up)
        return self.send_event(
            "error", self._tag("Sync failed", dry_run), run_id=run_id, job_run_id=job_run_id, target_id=target_id,
            job_name=job_name, target_label=target_label, stage="Error", status="failed",
            duration_seconds=duration_seconds, error_message=error_message)

    def send_check_result(self, job_name, target_id, target_label, run_id, success, mismatch_count=None,
                          error_message=None, dry_run=False) -> bool:
        if success:
            return self.send_event(
                "heartbeat", self._tag(f"{VERIFY_STAGE} passed", dry_run), run_id=run_id, target_id=target_id,
                job_name=job_name, target_label=target_label, stage=VERIFY_STAGE, status="success")
        message = (f"{VERIFY_STAGE} found {mismatch_count} mismatched item(s)" if mismatch_count
                   else f"{VERIFY_STAGE} FAILED")
        return self.send_event(
            "error", self._tag(message, dry_run), run_id=run_id, target_id=target_id, job_name=job_name,
            target_label=target_label, stage=VERIFY_STAGE, status="failed", error_message=error_message)

    def send_scheduler_check(self, due_jobs: int = 0) -> bool:
        """Bare heartbeat from each tick: the agent is alive."""
        return self.send_event("heartbeat", f"Scheduler check: {due_jobs} job(s) due", stage="Scheduler check")

    def send_job_schedule(self, job_name: str, cron_schedule: str) -> bool:
        """Bare heartbeat carrying a job's cron schedule(s), which the dashboard uses for "next run"."""
        return self.send_event("heartbeat", f"{job_name} schedule: {cron_schedule}", job_name=job_name,
                               cron_schedule=cron_schedule)

    def send_jobs(self, jobs: list) -> bool:
        """Every configured job with each pair's source and destination, for the Agents page; needs dashboard 0.3.0+."""
        return self._post_ok("/api/v2/jobs", {"hostname": self.hostname, "jobs": jobs}, quiet=(404,))


def jobs_payload(jobs: list) -> list:
    """What /api/v2/jobs takes: each job's schedules (disabled ones too) and one target per pair as written in the file."""
    return [{"job_name": job.name, "schedules": [{"cron": s.cron, "enabled": s.enabled} for s in job.schedules],
             "targets": [{"name": pair.resolved_name(), "source": pair.source, "destination": pair.destination}
                         for pair in job.pairs]} for job in jobs]
