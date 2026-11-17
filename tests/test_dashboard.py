import logging
from types import SimpleNamespace

import pytest
import requests

from app import dashboard
from app.config import Config
from app.dashboard import Client


class Posts:
    """Stands in for requests.post and records what was sent."""

    def __init__(self, status=201, error=None):
        self.status, self.error, self.calls = status, error, []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, json=json, headers=headers, timeout=timeout))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, text="")


@pytest.fixture
def posts(monkeypatch):
    fake = Posts()
    monkeypatch.setattr(requests, "post", fake)
    return fake


def client() -> Client:
    return Client("http://dash:5002/", "key", "host1")


class TestStandalone:
    def test_without_a_url_or_key_nothing_is_sent(self, posts):
        assert Client().send_event("heartbeat", "x") is False
        assert Client("http://dash", "").send_scheduler_check() is False
        assert posts.calls == []

    def test_from_env_needs_both_settings_and_the_enabled_flag(self, monkeypatch):
        cfg = Config()
        assert Client.from_env(cfg).enabled is False
        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash:5002/")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        assert Client.from_env(cfg).enabled is True and Client.from_env(cfg).url == "http://dash:5002"
        assert Client.from_env(Config(dashboard={"enabled": False})).enabled is False


class TestEvents:
    def test_every_event_carries_the_agent_identity_and_key(self, posts):
        assert client().send_event("heartbeat", "hi") is True
        call = posts.calls[0]
        assert call.url == "http://dash:5002/api/monitoring/events" and call.headers == {"X-API-Key": "key"}
        assert call.json["hostname"] == "host1" and call.json["agent_type"] == "Sync"
        assert call.json["version"] and call.json["event_type"] == "heartbeat"

    def test_a_bare_heartbeat_has_no_target_and_no_backup_type(self, posts):
        client().send_scheduler_check(2)
        body = posts.calls[0].json
        assert "target_id" not in body and "backup_type" not in body and body["stage"] == "Scheduler check"

    def test_a_pair_start_describes_the_target(self, posts):
        client().send_sync_start("job", "job:pair", "job:pair", "/a", "/b", run_id="r", job_run_id="jr", dry_run=True)
        body = posts.calls[0].json
        assert body["target_id"] == "job:pair" and body["backup_type"] == "sync" and body["source"] == "/a"
        assert body["destination"] == "/b" and body["job_run_id"] == "jr" and body["message"].startswith("[DRY RUN]")

    def test_progress_has_no_duration_so_it_never_finalizes(self, posts):
        client().send_sync_progress("job", "t", run_id="r", percent_complete=40, bytes_per_second=1000.0)
        body = posts.calls[0].json
        assert body["percent_complete"] == 40 and "duration_seconds" not in body

    def test_completion_success_failure_and_stopped(self, posts):
        c = client()
        c.send_sync_complete("job", "t", "t", 5.0, run_id="r", files_backed_up=3, bytes_backed_up=9)
        c.send_sync_complete("job", "t", "t", 5.0, run_id="r", success=False, error_message="boom")
        c.send_sync_complete("job", "t", "t", 5.0, run_id="r", stopped=True)
        ok, failed, stopped = (call.json for call in posts.calls)
        assert (ok["event_type"], ok["status"], ok["files_backed_up"]) == ("backup_complete", "success", 3)
        assert (failed["event_type"], failed["status"], failed["error_message"]) == ("error", "failed", "boom")
        assert (stopped["event_type"], stopped["status"], stopped["stage"]) == ("backup_complete", "stopped", "Stopped")

    def test_verify_results(self, posts):
        c = client()
        c.send_check_result("job", "t", "t", "r", success=True)
        c.send_check_result("job", "t", "t", "r", success=False, mismatch_count=4, error_message="4 differ")
        passed, mismatched = (call.json for call in posts.calls)
        assert passed["status"] == "success" and passed["stage"] == "Verify (quick)"
        assert mismatched["event_type"] == "error" and "4 mismatched" in mismatched["message"]

    def test_the_job_schedule_is_a_heartbeat_with_the_cron(self, posts):
        client().send_job_schedule("job", "0 2 * * *")
        body = posts.calls[0].json
        assert body["cron_schedule"] == "0 2 * * *" and body["job_name"] == "job" and "target_id" not in body


class TestJobs:
    def test_the_jobs_go_to_the_v2_endpoint_with_the_hostname(self, posts):
        jobs = [{"job_name": "j", "targets": [{"name": "p", "source": "/a", "destination": "nas2:a"}]}]
        assert client().send_jobs(jobs) is True
        call = posts.calls[0]
        assert call.url == "http://dash:5002/api/v2/jobs" and call.headers == {"X-API-Key": "key"}
        assert call.json == {"hostname": "host1", "jobs": jobs}

    def test_standalone_sends_nothing(self, posts):
        assert Client().send_jobs([]) is False and posts.calls == []

    def test_an_old_dashboard_without_the_endpoint_is_not_a_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(requests, "post", Posts(status=404))
        with caplog.at_level(logging.DEBUG):
            assert client().send_jobs([]) is False
        assert [r.levelno for r in caplog.records if "/api/v2/jobs" in r.getMessage()] == [logging.DEBUG]

    def test_every_pair_is_a_target(self):
        pairs = [SimpleNamespace(resolved_name=lambda: "backups", source="/mnt/backups", destination="nas2:jof"),
                 SimpleNamespace(resolved_name=lambda: "kpf", source="nas2:kpf", destination="/mnt/kpf")]
        assert dashboard.jobs_payload([SimpleNamespace(name="UNAS", pairs=pairs)]) == [{"job_name": "UNAS", "targets": [
            {"name": "backups", "source": "/mnt/backups", "destination": "nas2:jof"},
            {"name": "kpf", "source": "nas2:kpf", "destination": "/mnt/kpf"}]}]


class TestFailures:
    def test_a_rejection_is_logged_and_returns_false(self, monkeypatch, caplog):
        monkeypatch.setattr(requests, "post", Posts(status=403))
        with caplog.at_level(logging.WARNING):
            assert client().send_event("heartbeat", "x") is False
        assert "rejected" in caplog.text

    def test_an_unreachable_dashboard_never_raises_and_is_skipped_for_a_while(self, monkeypatch):
        fake = Posts(error=requests.exceptions.ConnectionError("down"))
        monkeypatch.setattr(requests, "post", fake)
        c = client()
        assert c.send_event("heartbeat", "x") is False
        assert c.send_event("heartbeat", "y") is False
        assert len(fake.calls) == 1  # the second call was skipped during the cooldown
