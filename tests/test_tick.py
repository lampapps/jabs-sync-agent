from datetime import datetime, timezone

import pytest

from app import config, runner, schedule, state, tick

from .conftest import write_config, write_job
from .test_runner import Recorder

UTC = timezone.utc
JOB = ('job_name: "{name}"\npairs:\n  - source: /data/in\n    destination: /data/out\n'
       'schedules:\n  - cron: "{cron}"\n    enabled: true\n')
MANUAL = "job_name: manual\npairs:\n  - source: /data/in\n    destination: /data/out\n"


def at(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def jobs(home):
    write_config(home)
    write_job(home, "daily", JOB.format(name="daily", cron="0 3 * * *"))
    write_job(home, "hourly", JOB.format(name="hourly", cron="0 * * * *"))
    write_job(home, "manual", MANUAL)
    return home


@pytest.fixture
def ran(monkeypatch):
    names = []
    monkeypatch.setattr(runner, "run_job", lambda job, cfg, client, **kw: names.append(job.name) or True)
    return names


def run_tick(client=None, now=None, cfg=None):
    tick.run_once(cfg or config.load_config(), client or Recorder(), now)


def test_the_first_tick_only_sets_baselines(jobs, ran):
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 8, 2))
    assert ran == []
    assert client.first("send_scheduler_check")[1] == (0,)


def test_a_job_runs_when_its_slot_arrives(jobs, ran):
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert sorted(ran) == ["daily", "hourly"]  # the hourly slot at 03:00 and the daily one
    run_tick(now=at(2027, 1, 15, 3, 7))
    assert sorted(ran) == ["daily", "hourly"]  # nothing new


def test_a_missed_slot_runs_once_at_the_next_tick(jobs, ran):
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 9, 31))  # the machine was off all night
    assert sorted(ran) == ["daily", "hourly"]
    run_tick(now=at(2027, 1, 15, 9, 36))
    assert sorted(ran) == ["daily", "hourly"]


def test_the_heartbeat_reports_each_schedule(jobs, ran):
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 8, 2))
    reported = {args for name, args, _ in client.calls if name == "send_job_schedule"}
    assert reported == {("daily", "0 3 * * *"), ("hourly", "0 * * * *")}  # manual-only jobs have none
    assert client.names()[0] == "send_scheduler_check"  # first, so it goes out even if a sync runs long


def test_the_heartbeat_reports_each_jobs_pairs(jobs, ran):
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 8, 2))
    (call,) = client.all("send_jobs")
    reported = {entry["job_name"]: entry["targets"] for entry in call[1][0]}
    assert sorted(reported) == ["daily", "hourly", "manual"]  # the config, not only the scheduled jobs
    assert reported["daily"] == [{"name": "in", "source": "/data/in", "destination": "/data/out"}]
    schedules = {entry["job_name"]: entry["schedules"] for entry in call[1][0]}
    assert schedules == {"daily": [{"cron": "0 3 * * *", "enabled": True}],
                         "hourly": [{"cron": "0 * * * *", "enabled": True}], "manual": []}
    assert client.names().index("send_jobs") > client.names().index("send_scheduler_check")


def test_the_due_count_is_in_the_heartbeat(jobs, ran):
    run_tick(now=at(2027, 1, 15, 2, 58))
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 3, 2))
    assert client.first("send_scheduler_check")[1] == (2,)


def test_a_crashing_job_does_not_stop_the_others(jobs, monkeypatch):
    seen = []

    def run_job(job, cfg, client, **kwargs):
        seen.append(job.name)
        if job.name == "daily":
            raise RuntimeError("boom")
        return True
    monkeypatch.setattr(runner, "run_job", run_job)
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert sorted(seen) == ["daily", "hourly"]


def test_a_broken_job_file_is_skipped_and_the_rest_still_run(jobs, ran):
    write_job(jobs, "broken", "pairs: []\n")
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert sorted(ran) == ["daily", "hourly"]


def test_a_failure_to_plan_still_sends_the_heartbeat(jobs, ran, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk gone")
    monkeypatch.setattr(config, "load_jobs", broken)
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 3, 2))
    assert client.first("send_scheduler_check")[1] == (0,) and ran == []


def test_a_stop_during_one_job_keeps_the_next_from_starting(jobs, monkeypatch):
    seen = []

    def run_job(job, cfg, client, stop=None, **kwargs):
        seen.append(job.name)
        stop.request()
        return True
    monkeypatch.setattr(runner, "run_job", run_job)
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert len(seen) == 1


def test_a_job_that_became_due_while_another_ran_is_served_next_tick(jobs, ran):
    """Planning marks slots served up front, so a long sync cannot make the next tick repeat them."""
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    count = len(ran)
    run_tick(now=at(2027, 1, 15, 3, 3))
    assert len(ran) == count


def test_a_corrupt_state_file_does_not_stop_a_tick(jobs, ran):
    (jobs / "data").mkdir(exist_ok=True)
    (jobs / "data" / "state.json").write_text("{broken")
    run_tick(now=at(2027, 1, 15, 8))
    assert state.read()["jobs"]["daily"]["served"]


def test_local_now_is_timezone_aware():
    assert schedule.local_now().tzinfo is not None
