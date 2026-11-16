import pytest

from app import locking, runner, state
from app.config import Config, load_config, load_job
from app.rsync import StopRequest

from .conftest import requires_rsync, write_config, write_job


class Recorder:
    """Stands in for the dashboard client and records every call."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True
        return record

    def names(self):
        return [name for name, _, _ in self.calls]

    def first(self, name):
        return next(c for c in self.calls if c[0] == name)

    def all(self, name):
        return [c for c in self.calls if c[0] == name]


def put(root, files: dict) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def files(root) -> dict:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def make_job(home, tmp_path, pairs=("media",), extra="", config_text=""):
    """A job whose pairs are tmp_path/<name>/src -> tmp_path/<name>/dst."""
    write_config(home, "use_common_exclude: false\n" + config_text)
    body = 'job_name: "j"\npairs:\n' + "".join(
        f"  - name: {name}\n    source: {tmp_path / name / 'src'}\n    destination: {tmp_path / name / 'dst'}\n"
        for name in pairs) + extra
    return load_job(write_job(home, "j", body), load_config())


@pytest.fixture
def env(home, monkeypatch):
    """Notifications and Uptime Kuma pings are recorded instead of sent."""
    notices, pings = [], []
    monkeypatch.setattr(runner.mailer, "notify", lambda email, kind, subject, body: notices.append((kind, subject, body)))
    monkeypatch.setattr(runner.kuma, "ping", lambda status, message="", dry_run=False: pings.append((status, message)))
    return {"notices": notices, "pings": pings}


@requires_rsync
class TestRunJob:
    def test_a_successful_run_copies_verifies_and_reports(self, home, tmp_path, env):
        job = make_job(home, tmp_path)
        put(tmp_path / "media" / "src", {"a.txt": "a", "sub/b.txt": "b"})
        client = Recorder()
        assert runner.run_job(job, Config(), client) is True
        assert files(tmp_path / "media" / "dst") == {"a.txt": "a", "sub/b.txt": "b"}
        assert client.names()[0] == "send_sync_start" and client.names()[-1] == "send_check_result"
        done = client.first("send_sync_complete")
        assert done[1][0] == "j" and done[1][1] == "j:media" and done[2]["files_backed_up"] == 2
        assert done[2].get("success", True) is True and done[2].get("stopped") is not True
        assert client.first("send_check_result")[2]["success"] is True
        last = state.read()["jobs"]["j"]["last_run"]
        assert last["success"] is True and last["stopped"] is False and last["pairs"][0]["files"] == 2
        assert env["pings"] == [("up", "j: OK")] and [n[0] for n in env["notices"]] == ["backup_complete"]

    def test_every_pair_is_its_own_target_with_one_shared_job_run_id(self, home, tmp_path, env):
        job = make_job(home, tmp_path, pairs=("one", "two"))
        put(tmp_path / "one" / "src", {"a": "a"})
        put(tmp_path / "two" / "src", {"b": "b"})
        client = Recorder()
        assert runner.run_job(job, Config(), client)
        starts = client.all("send_sync_start")
        assert [c[1][1] for c in starts] == ["j:one", "j:two"]
        assert len({c[2]["job_run_id"] for c in starts}) == 1 and len({c[2]["run_id"] for c in starts}) == 2

    def test_a_failing_pair_does_not_stop_the_next_one(self, home, tmp_path, env):
        job = make_job(home, tmp_path, pairs=("broken", "fine"))
        put(tmp_path / "fine" / "src", {"a": "a"})        # broken/src does not exist
        client = Recorder()
        assert runner.run_job(job, Config(), client) is False
        assert files(tmp_path / "fine" / "dst") == {"a": "a"}
        errors = [c for c in client.all("send_sync_complete") if c[2].get("success", True) is False]
        assert len(errors) == 1 and "not accessible" in errors[0][2]["error_message"]
        assert [n[0] for n in env["notices"]] == ["error", "backup_complete"]
        assert env["pings"][0][0] == "down"
        last = state.read()["jobs"]["j"]["last_run"]
        assert last["success"] is False and "not accessible" in last["error"]
        assert [p["success"] for p in last["pairs"]] == [False, True]

    def test_a_dry_run_changes_nothing_and_skips_verify(self, home, tmp_path, env):
        job = make_job(home, tmp_path)
        put(tmp_path / "media" / "src", {"a.txt": "a"})
        client = Recorder()
        assert runner.run_job(job, Config(), client, dry_run=True)
        assert not (tmp_path / "media" / "dst").exists()
        assert "send_check_result" not in client.names()
        assert client.first("send_sync_start")[2]["dry_run"] is True
        assert env["pings"] == [("up", "[DRY RUN] j: OK")]

    def test_verify_can_be_switched_off(self, home, tmp_path, env):
        job = make_job(home, tmp_path, extra="verify_after_sync: false\n")
        put(tmp_path / "media" / "src", {"a.txt": "a"})
        client = Recorder()
        runner.run_job(job, Config(), client)
        assert "send_check_result" not in client.names()

    def test_a_verify_mismatch_is_reported_and_emailed_but_the_sync_stays_good(self, home, tmp_path, env, monkeypatch):
        job = make_job(home, tmp_path)
        put(tmp_path / "media" / "src", {"a.txt": "a"})
        monkeypatch.setattr(runner.rsync, "count_diffs", lambda *a, **k: 3)
        client = Recorder()
        assert runner.run_job(job, Config(), client) is True
        result = client.first("send_check_result")
        assert result[2]["success"] is False and result[2]["mismatch_count"] == 3
        assert [n[0] for n in env["notices"]] == ["backup_complete", "error"]

    def test_a_destination_without_enough_free_space_fails_the_pair(self, home, tmp_path, env):
        job = make_job(home, tmp_path, extra="rsync:\n  min_free_bytes: 999999999999999999\n")
        put(tmp_path / "media" / "src", {"a.txt": "a"})
        assert runner.run_job(job, Config(), Recorder()) is False
        assert files(tmp_path / "media" / "dst") == {}
        assert "free" in state.read()["jobs"]["j"]["last_run"]["error"]

    def test_the_backup_dir_keeps_what_a_run_overwrites_and_old_runs_are_pruned(self, home, tmp_path, env):
        kept = tmp_path / "kept"
        job = make_job(home, tmp_path, extra=f"rsync:\n  backup_dir: {kept}\n  backup_dir_retention_days: 30\n")
        put(tmp_path / "media" / "src", {"a.txt": "new content"})
        put(tmp_path / "media" / "dst", {"a.txt": "old"})
        old_run = kept / "j_media" / "19990101_000000"
        old_run.mkdir(parents=True)
        assert runner.run_job(job, Config(), Recorder())
        saved = {p.name: p.read_text() for p in (kept / "j_media").rglob("a.txt")}
        assert saved == {"a.txt": "old"} and not old_run.exists()

    def test_a_job_that_is_already_running_is_skipped(self, home, tmp_path, env):
        job = make_job(home, tmp_path)
        put(tmp_path / "media" / "src", {"a.txt": "a"})
        held = locking.acquire_lock(runner.lock_path("j"))
        try:
            client = Recorder()
            assert runner.run_job(job, Config(), client) is False
        finally:
            locking.release_lock(held)
        assert client.calls == [] and not (tmp_path / "media" / "dst").exists()

    def test_the_lock_is_released_afterwards(self, home, tmp_path, env):
        job = make_job(home, tmp_path)
        put(tmp_path / "media" / "src", {"a.txt": "a"})
        runner.run_job(job, Config(), Recorder())
        assert runner.run_job(job, Config(), Recorder()) is True

    def test_a_stop_before_the_run_finalizes_every_pair_as_stopped(self, home, tmp_path, env):
        job = make_job(home, tmp_path, pairs=("one", "two"))
        put(tmp_path / "one" / "src", {"a": "a"})
        stop = StopRequest()
        stop.request()
        client = Recorder()
        assert runner.run_job(job, Config(), client, stop=stop) is True
        done = client.all("send_sync_complete")
        assert len(done) == 2 and all(c[2]["stopped"] is True for c in done)
        assert not (tmp_path / "one" / "dst").exists()
        last = state.read()["jobs"]["j"]["last_run"]
        assert last["success"] is True and last["stopped"] is True
        assert env["notices"] == [] and env["pings"][0][0] == "up"

    def test_a_stop_during_a_pair_leaves_the_later_pairs_unstarted(self, home, tmp_path, env, monkeypatch):
        job = make_job(home, tmp_path, pairs=("one", "two"))
        put(tmp_path / "one" / "src", {"a": "a"})
        put(tmp_path / "two" / "src", {"b": "b"})
        stop = StopRequest()
        real_sync = runner.rsync.sync

        def stopping_sync(*args, **kwargs):
            stop.request()
            return {**real_sync(*args, **kwargs), "stopped": True}
        monkeypatch.setattr(runner.rsync, "sync", stopping_sync)
        client = Recorder()
        assert runner.run_job(job, Config(), client, stop=stop) is True
        assert [c[2]["stopped"] for c in client.all("send_sync_complete")] == [True, True]
        assert not (tmp_path / "two" / "dst").exists()
        assert env["notices"] == []


class TestSignals:
    def test_signals_request_a_stop_and_the_old_handlers_come_back(self):
        import os
        import signal

        before = signal.getsignal(signal.SIGTERM)
        stop = StopRequest()
        with runner.stop_on_signals(stop):
            os.kill(os.getpid(), signal.SIGTERM)
        assert stop.requested is True and signal.getsignal(signal.SIGTERM) == before


def test_lock_paths_are_safe_file_names(home):
    assert runner.lock_path("my job/../x").endswith("my_job____x.lock")
