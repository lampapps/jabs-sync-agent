"""Periodic work, run by the systemd timer (`python -m app tick`): heartbeat and due jobs."""

import logging
from datetime import datetime
from typing import Optional

from . import config, runner, schedule, state
from .config import Config
from .dashboard import Client
from .rsync import StopRequest

logger = logging.getLogger(__name__)


class Tick:
    def __init__(self, cfg: Config, client: Client, now: datetime):
        self.cfg, self.client, self.now = cfg, client, now
        self.jobs: list = []
        self.due: list = []
        self.stop = StopRequest()

    def load(self) -> None:
        self.jobs, errors = config.load_jobs(self.cfg)
        for message in errors:
            logger.error("skipping a job file: %s", message)
        with state.transaction() as data:
            self.due = schedule.due_jobs(self.jobs, data, self.now)

    def heartbeat(self) -> None:
        self.client.send_scheduler_check(len(self.due))
        for job in self.jobs:
            crons = job.enabled_crons()
            if crons:
                self.client.send_job_schedule(job.name, ",".join(crons))

    def run_due_jobs(self) -> None:
        with runner.stop_on_signals(self.stop):
            for job in self.due:
                if self.stop.requested:
                    logger.info("Stop requested; not starting job '%s'", job.name)
                    break
                logger.info("Job '%s' is due", job.name)
                try:
                    runner.run_job(job, self.cfg, self.client, stop=self.stop)
                except Exception:  # pylint: disable=broad-except  # one failing job must not stop the next one
                    logger.exception("Job '%s' crashed", job.name)


# Heartbeat first so a long sync does not keep the dashboard waiting.
TASKS = [("heartbeat", Tick.heartbeat), ("due jobs", Tick.run_due_jobs)]


def run_once(cfg: Config, client: Client, now: Optional[datetime] = None) -> None:
    """Plan what is due, then run every task once. A failing task is logged and does not stop the others."""
    tick = Tick(cfg, client, now or schedule.local_now())
    try:
        tick.load()
    except Exception:  # pylint: disable=broad-except  # a bad state file must not stop the heartbeat
        logger.exception("could not plan due jobs")
    for name, task in TASKS:
        try:
            task(tick)
        except Exception:  # pylint: disable=broad-except  # tasks are independent
            logger.exception("tick task '%s' failed", name)
    logger.info("tick complete: %d job(s) due", len(tick.due))
