"""Remotes (rsync daemons), endpoints, the safety guards and stop_at.

The daemon tests start a real `rsync --daemon` on a free loopback port with a throwaway module, so they exercise
the same code path as a NAS reached over Tailscale.
"""

import os
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import endpoint, ops, rsync, runner, schedule, state
from app.__main__ import main
from app.config import Config, ConfigError, Remote, RsyncOptions, find_job, load_config, load_job
from app.rsync import RsyncError, StopRequest

from .conftest import requires_rsync, write_config, write_job
from .test_runner import Recorder, env, files, put  # noqa: F401  (env is a fixture)

UTC = timezone.utc


class TestEndpoint:
    def test_an_absolute_path_is_local(self):
        assert endpoint.parse("/mnt/nas1/a") == endpoint.Endpoint(None, "/mnt/nas1/a")

    def test_name_colon_path_is_remote(self):
        assert endpoint.parse("nas2:photos/2024/") == endpoint.Endpoint("nas2", "photos/2024")
        assert endpoint.parse("nas2:/photos") == endpoint.Endpoint("nas2", "photos")
        assert endpoint.parse("nas2:") == endpoint.Endpoint("nas2", "")

    def test_hidden_folders_keep_their_dot(self):
        assert endpoint.parse("nas2:.config/x").path == ".config/x"

    @pytest.mark.parametrize("text", ["relative/path", "nas2:../etc", "nas2:a/../b", "9nas:x", ""])
    def test_bad_endpoints_are_rejected(self, text):
        with pytest.raises(ValueError):
            endpoint.parse(text)

    def test_overlap_needs_the_same_side(self):
        local = endpoint.parse
        assert endpoint.overlap(local("/a"), local("/a/b"))
        assert endpoint.overlap(local("/a/b"), local("/a"))
        assert not endpoint.overlap(local("/a"), local("/ab"))
        assert endpoint.overlap(local("nas:x"), local("nas:x/y"))
        assert not endpoint.overlap(local("nas:x"), local("nas:y"))
        assert not endpoint.overlap(local("nas:x"), local("/x"))
        assert not endpoint.overlap(local("nas:x"), local("other:x"))
        assert endpoint.overlap(local("nas:"), local("nas:anything"))


def remote_config(extra: str = "") -> str:
    return ("use_common_exclude: false\nremotes:\n  nas2:\n    host: 127.0.0.1\n    module: backup\n"
            "    user: jabs\n    password_file: /nonexistent/secret\n" + extra)


def job_body(*pairs: str, extra: str = "") -> str:
    return 'job_name: "j"\npairs:\n' + "".join(
        f"  - source: {src}\n    destination: {dst}\n" for src, dst in pairs) + extra


class TestConfig:
    def test_a_pair_can_use_a_remote_on_one_side(self, home):
        write_config(home, remote_config())
        job = load_job(write_job(home, "j", job_body(("/mnt/a", "nas2:a"), ("nas2:b", "/mnt/b"))), load_config())
        assert [p.resolved_name() for p in job.pairs] == ["a", "b"]
        assert job.pairs[0].uses_remote() and job.pairs[1].uses_remote()

    def test_two_remotes_in_one_pair_are_rejected(self, home):
        write_config(home, remote_config())
        with pytest.raises(ConfigError, match="two remotes"):
            load_job(write_job(home, "j", job_body(("nas2:a", "nas2:b"))), load_config())

    def test_an_unknown_remote_names_the_defined_ones(self, home):
        write_config(home, remote_config())
        with pytest.raises(ConfigError, match="'other'.*defined: nas2"):
            load_job(write_job(home, "j", job_body(("/mnt/a", "other:a"))), load_config())

    def test_a_remote_needs_an_absolute_password_file_and_a_valid_name(self, home):
        write_config(home, "remotes:\n  nas2:\n    host: h\n    module: m\n    password_file: relative\n")
        with pytest.raises(ConfigError, match="absolute"):
            load_config()
        write_config(home, "remotes:\n  2bad:\n    host: h\n    module: m\n")
        with pytest.raises(ConfigError, match="must start with a letter"):
            load_config()

    def test_backup_dir_is_refused_for_a_remote_destination(self, home):
        write_config(home, remote_config())
        body = job_body(("/mnt/a", "nas2:a"), extra='rsync:\n  backup_dir: /mnt/keep\n')
        with pytest.raises(ConfigError, match="remote"):
            load_job(write_job(home, "j", body), load_config())
        body = 'pairs:\n  - source: /mnt/a\n    destination: nas2:a\n    backup_dir: /mnt/keep\n'
        with pytest.raises(ConfigError, match="backup_dir"):
            load_job(write_job(home, "j", body), load_config())

    def test_a_pair_that_feeds_another_pair_is_rejected(self, home):
        write_config(home, remote_config())
        with pytest.raises(ConfigError, match="overlap"):
            load_job(write_job(home, "j", job_body(("/mnt/x", "nas2:a"), ("nas2:a", "/mnt/y"))), load_config())
        with pytest.raises(ConfigError, match="overlap"):
            load_job(write_job(home, "j", job_body(("/mnt/a", "nas2:x"), ("/mnt/b", "nas2:x/y"))), load_config())

    def test_the_mirrored_pair_from_the_nas_example_is_valid(self, home):
        write_config(home, remote_config())
        body = job_body(("/mnt/nas1/dirA", "nas2:dirA"), ("nas2:dirB", "/mnt/nas1/dirB"), extra='stop_at: "07:00"\n')
        job = load_job(write_job(home, "j", body), load_config())
        assert job.stop_at == "07:00"

    @pytest.mark.parametrize("value", ["7:00", "24:00", "07:60", "soon"])
    def test_stop_at_must_be_a_clock_time(self, home, value):
        write_config(home)
        with pytest.raises(ConfigError, match="stop_at"):
            load_job(write_job(home, "j", job_body(("/a", "/b"), extra=f'stop_at: "{value}"\n')), load_config())

    def test_whole_file_is_automatic_unless_set(self):
        assert "--whole-file" in rsync.build_args(RsyncOptions())
        assert "--no-whole-file" in rsync.build_args(RsyncOptions(), remote=True)
        assert "--whole-file" in rsync.build_args(RsyncOptions(whole_file=True), remote=True)
        assert "--no-whole-file" in rsync.build_args(RsyncOptions(whole_file=False))

    def test_new_options_become_flags(self):
        args = rsync.build_args(RsyncOptions(bwlimit=500, compress=True, max_delete=7))
        assert "--bwlimit=500" in args and "--compress" in args and "--max-delete=7" in args
        args = rsync.build_args(RsyncOptions(delete=False, max_delete=7))
        assert not any(a.startswith("--max-delete") for a in args)
        assert not any(a.startswith("--bwlimit") for a in rsync.build_args(RsyncOptions()))


class TestTargets:
    remotes = {"nas2": Remote(host="nas2.ts.net", module="backup", user="jabs", password_file="/p", port=874)}

    def test_a_remote_becomes_an_rsync_url(self):
        assert rsync.target("nas2:photos/2024", self.remotes)[0] == "rsync://jabs@nas2.ts.net:874/backup/photos/2024/"
        assert rsync.target("nas2:", self.remotes)[0] == "rsync://jabs@nas2.ts.net:874/backup/"
        assert rsync.target("/mnt/a/", self.remotes) == ("/mnt/a/", None)

    def test_ipv6_hosts_are_bracketed_and_no_user_is_allowed(self):
        remote = Remote(host="fd7a::1", module="m")
        assert rsync.remote_url(remote, "x") == "rsync://[fd7a::1]:873/m/x/"

    def test_the_password_is_only_ever_a_file_argument(self):
        args = rsync.remote_args(self.remotes["nas2"], RsyncOptions(contimeout=3))
        assert args == ["--contimeout=3", "--password-file=/p"]
        assert rsync.remote_args(None, RsyncOptions()) == []

    def test_an_undefined_remote_is_an_error(self):
        with pytest.raises(RsyncError, match="not defined"):
            rsync.target("ghost:x", self.remotes)


class TestDeadline:
    def test_the_next_occurrence_is_today_or_tomorrow(self):
        now = datetime(2027, 1, 15, 3, 0).astimezone()
        assert schedule.stop_deadline("07:00", now) == datetime(2027, 1, 15, 7, 0).astimezone()
        assert schedule.stop_deadline("03:00", now) == datetime(2027, 1, 16, 3, 0).astimezone()
        assert schedule.stop_deadline("02:59", now) == datetime(2027, 1, 16, 2, 59).astimezone()

    def job(self, stop_at):
        return SimpleNamespace(name="j", enabled_crons=lambda: ["0 2 * * *"], stop_at=stop_at)

    def test_a_slot_whose_window_has_closed_is_skipped(self):
        local = lambda *a: datetime(*a).astimezone()  # noqa: E731
        data: dict = {}
        job = self.job("07:00")
        schedule.due_jobs([job], data, local(2027, 1, 14, 12))      # baseline
        # the 02:00 slot is served at 10:00, after the 07:00 stop: skipped, and not retried later
        assert schedule.due_jobs([job], data, local(2027, 1, 15, 10)) == []
        assert schedule.due_jobs([job], data, local(2027, 1, 15, 10, 5)) == []

    def test_a_slot_inside_its_window_still_runs(self):
        local = lambda *a: datetime(*a).astimezone()  # noqa: E731
        data: dict = {}
        job = self.job("07:00")
        schedule.due_jobs([job], data, local(2027, 1, 14, 12))
        assert schedule.due_jobs([job], data, local(2027, 1, 15, 2, 5)) == [job]

    def test_jobs_without_stop_at_are_unchanged(self):
        local = lambda *a: datetime(*a).astimezone()  # noqa: E731
        data: dict = {}
        job = self.job(None)
        schedule.due_jobs([job], data, local(2027, 1, 14, 12))
        assert schedule.due_jobs([job], data, local(2027, 1, 15, 23)) == [job]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def daemon(tmp_path):
    """A real rsync daemon on loopback with one writable, password protected module `backup`."""
    root = tmp_path / "module"
    root.mkdir()
    secrets, password = tmp_path / "rsyncd.secrets", tmp_path / "client.secret"
    secrets.write_text("jabs:pw\n", encoding="utf-8")
    password.write_text("pw\n", encoding="utf-8")
    os.chmod(secrets, 0o600)
    os.chmod(password, 0o600)
    port = free_port()
    conf = tmp_path / "rsyncd.conf"
    conf.write_text(
        f"port = {port}\naddress = 127.0.0.1\npid file = {tmp_path / 'rsyncd.pid'}\nuse chroot = no\n"
        f"log file = {tmp_path / 'rsyncd.log'}\n[backup]\n  path = {root}\n  read only = no\n"
        f"  auth users = jabs\n  secrets file = {secrets}\n", encoding="utf-8")
    proc = subprocess.Popen(["rsync", "--daemon", "--no-detach", f"--config={conf}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.skip("could not start a loopback rsync daemon")
        remote = Remote(host="127.0.0.1", module="backup", user="jabs", password_file=str(password), port=port)
        yield SimpleNamespace(root=root, remote=remote, remotes={"nas2": remote}, port=port, password=password)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@requires_rsync
class TestAgainstADaemon:
    def test_local_to_remote(self, daemon, tmp_path):
        put(tmp_path / "src", {"a.txt": "a", "sub/b.txt": "b"})
        result = rsync.sync(str(tmp_path / "src"), "nas2:dirA", RsyncOptions(), remotes=daemon.remotes)
        assert result["files_transferred"] == 2
        assert files(daemon.root / "dirA") == {"a.txt": "a", "sub/b.txt": "b"}

    def test_remote_to_local_and_delete_mirrors(self, daemon, tmp_path):
        put(daemon.root / "dirB", {"x": "1", "y": "2"})
        put(tmp_path / "dst", {"stale": "old"})
        rsync.sync("nas2:dirB", str(tmp_path / "dst"), RsyncOptions(), remotes=daemon.remotes)
        assert files(tmp_path / "dst") == {"x": "1", "y": "2"}

    def test_a_wrong_password_is_a_clear_error(self, daemon, tmp_path):
        daemon.password.write_text("nope\n", encoding="utf-8")
        put(tmp_path / "src", {"a": "a"})
        with pytest.raises(RsyncError, match="check the module name, user and password"):
            rsync.sync(str(tmp_path / "src"), "nas2:x", RsyncOptions(), remotes=daemon.remotes)

    def test_a_closed_port_is_a_clear_error(self, daemon, tmp_path):
        dead = {"nas2": daemon.remote.model_copy(update={"port": free_port()})}
        with pytest.raises(RsyncError, match="could not connect"):
            rsync.list_entries("nas2:", RsyncOptions(contimeout=2), dead)

    def test_list_entries_reads_a_remote_and_a_local_directory(self, daemon, tmp_path):
        put(daemon.root / "d", {"one": "1", "two/three": "3"})
        assert sorted(rsync.list_entries("nas2:d", RsyncOptions(), daemon.remotes)) == ["one", "two"]
        (tmp_path / "local").mkdir()
        assert rsync.list_entries(str(tmp_path / "local"), RsyncOptions()) == []

    def test_count_diffs_works_through_a_remote(self, daemon, tmp_path):
        put(tmp_path / "src", {"a": "a"})
        assert rsync.count_diffs(str(tmp_path / "src"), "nas2:d", RsyncOptions(), remotes=daemon.remotes) > 0
        rsync.sync(str(tmp_path / "src"), "nas2:d", RsyncOptions(), remotes=daemon.remotes)
        assert rsync.count_diffs(str(tmp_path / "src"), "nas2:d", RsyncOptions(), remotes=daemon.remotes) == 0

    def test_a_job_mirrors_both_ways_in_order(self, home, daemon, tmp_path, env):  # noqa: F811
        write_config(home, f"use_common_exclude: false\nremotes:\n  nas2:\n    host: 127.0.0.1\n    module: backup\n"
                           f"    user: jabs\n    password_file: {daemon.password}\n    port: {daemon.port}\n")
        put(tmp_path / "nas1" / "dirA", {"a": "A"})
        put(daemon.root / "dirB", {"b": "B"})
        body = job_body((str(tmp_path / "nas1" / "dirA"), "nas2:dirA"), ("nas2:dirB", str(tmp_path / "nas1" / "dirB")))
        job = load_job(write_job(home, "j", body), load_config())
        client = Recorder()
        assert runner.run_job(job, load_config(), client) is True
        assert files(daemon.root / "dirA") == {"a": "A"}
        assert files(tmp_path / "nas1" / "dirB") == {"b": "B"}
        assert [c[1][1] for c in client.all("send_sync_start")] == ["j:dirA", "j:dirB"]
        assert [c[2]["success"] for c in client.all("send_check_result")] == [True, True]

    def test_a_missing_password_file_fails_the_pair_before_rsync_runs(self, home, daemon, tmp_path, env):  # noqa: F811
        write_config(home, remote_config())
        put(tmp_path / "src", {"a": "a"})
        job = load_job(write_job(home, "j", job_body((str(tmp_path / "src"), "nas2:x"))), load_config())
        client = Recorder()
        assert runner.run_job(job, load_config(), client) is False
        assert "Password file for remote 'nas2' not found" in state.read()["jobs"]["j"]["last_run"]["error"]

    def test_an_empty_remote_source_is_refused(self, home, daemon, tmp_path, env):  # noqa: F811
        write_config(home, f"use_common_exclude: false\nremotes:\n  nas2:\n    host: 127.0.0.1\n    module: backup\n"
                           f"    user: jabs\n    password_file: {daemon.password}\n    port: {daemon.port}\n")
        (daemon.root / "dirB").mkdir()
        put(tmp_path / "dst", {"precious": "data"})
        job = load_job(write_job(home, "j", job_body(("nas2:dirB", str(tmp_path / "dst")))), load_config())
        assert runner.run_job(job, load_config(), Recorder()) is False
        assert files(tmp_path / "dst") == {"precious": "data"}


def make_pair_job(home, tmp_path, rsync_lines="", extra=""):
    write_config(home, "use_common_exclude: false\n")
    body = (f'job_name: "j"\npairs:\n  - name: p\n    source: {tmp_path / "src"}\n'
            f"    destination: {tmp_path / 'dst'}\n{extra}")
    if rsync_lines:
        body += "rsync:\n" + rsync_lines
    return load_job(write_job(home, "j", body), load_config())


@requires_rsync
class TestGuards:
    def test_an_empty_source_is_not_mirrored_over_a_full_destination(self, home, tmp_path, env):  # noqa: F811
        (tmp_path / "src").mkdir()
        put(tmp_path / "dst", {"precious": "data"})
        job = make_pair_job(home, tmp_path)
        client = Recorder()
        assert runner.run_job(job, Config(), client) is False
        assert files(tmp_path / "dst") == {"precious": "data"}
        assert "Source is empty" in state.read()["jobs"]["j"]["last_run"]["error"]
        assert [n[0] for n in env["notices"]] == ["error"]

    def test_an_empty_source_is_allowed_when_asked_for(self, home, tmp_path, env):  # noqa: F811
        (tmp_path / "src").mkdir()
        put(tmp_path / "dst", {"old": "x"})
        job = make_pair_job(home, tmp_path, "  refuse_empty_source: false\n")
        assert runner.run_job(job, Config(), Recorder()) is True
        assert files(tmp_path / "dst") == {}

    def test_an_empty_source_is_fine_without_delete(self, home, tmp_path, env):  # noqa: F811
        (tmp_path / "src").mkdir()
        job = make_pair_job(home, tmp_path, "  delete: false\n")
        assert runner.run_job(job, Config(), Recorder()) is True

    def test_max_delete_stops_a_mass_deletion(self, home, tmp_path, env):  # noqa: F811
        put(tmp_path / "src", {"keep": "k"})
        put(tmp_path / "dst", {f"old{i}": "x" for i in range(5)})
        job = make_pair_job(home, tmp_path, "  max_delete: 2\n")
        client = Recorder()
        assert runner.run_job(job, Config(), client) is False
        assert "max_delete" in state.read()["jobs"]["j"]["last_run"]["error"]
        assert len([p for p in files(tmp_path / "dst") if p.startswith("old")]) >= 3

    def test_max_delete_allows_a_small_deletion(self, home, tmp_path, env):  # noqa: F811
        put(tmp_path / "src", {"keep": "k"})
        put(tmp_path / "dst", {"old1": "x"})
        job = make_pair_job(home, tmp_path, "  max_delete: 2\n")
        assert runner.run_job(job, Config(), Recorder()) is True
        assert files(tmp_path / "dst") == {"keep": "k"}


@requires_rsync
class TestStopAt:
    def test_the_deadline_stops_the_run_and_the_next_run_resumes(self, home, tmp_path, env, monkeypatch):  # noqa: F811
        big = "x" * (3 * 1024 * 1024)
        put(tmp_path / "src", {"big.bin": big})
        job = make_pair_job(home, tmp_path, "  bwlimit: 200\n", extra="")
        job = job.model_copy(update={"stop_at": "07:00"})
        now = schedule.local_now()
        monkeypatch.setattr(runner.schedule, "stop_deadline", lambda *_: now + timedelta(seconds=1.5))
        client = Recorder()
        started = time.time()
        assert runner.run_job(job, Config(), client) is True
        assert time.time() - started < 15
        assert client.first("send_sync_complete")[2]["stopped"] is True
        assert state.read()["jobs"]["j"]["last_run"]["stopped"] is True
        assert env["notices"] == []

        monkeypatch.undo()
        finishing = job.model_copy(update={"rsync": job.rsync.model_copy(update={"bwlimit": 0})})
        assert runner.run_job(finishing, Config(), Recorder()) is True
        assert files(tmp_path / "dst") == {"big.bin": big}

    def test_a_deadline_only_stops_its_own_job(self, home, tmp_path, env, monkeypatch):  # noqa: F811
        put(tmp_path / "src", {"a": "a"})
        job = make_pair_job(home, tmp_path)
        job = job.model_copy(update={"stop_at": "07:00"})
        outer = StopRequest()
        assert runner.run_job(job, Config(), Recorder(), stop=outer) is True
        assert outer.requested is False

    def test_a_signal_still_reaches_a_job_with_stop_at(self, home, tmp_path, env):  # noqa: F811
        put(tmp_path / "src", {"a": "a"})
        job = make_pair_job(home, tmp_path).model_copy(update={"stop_at": "07:00"})
        outer = StopRequest()
        outer.request()
        client = Recorder()
        assert runner.run_job(job, Config(), client, stop=outer) is True
        assert client.first("send_sync_complete")[2]["stopped"] is True
        assert not (tmp_path / "dst").exists()


class TestRemotesCommand:
    def test_parse_tailscale_ping(self):
        assert ops.parse_tailscale_ping("pong from nas2 (100.64.0.2) via 203.0.113.5:41641 in 21ms\n") == "direct"
        assert ops.parse_tailscale_ping("pong from nas2 (100.64.0.2) via DERP(fra) in 48ms\n") == "relay"
        assert ops.parse_tailscale_ping("ping timed out\n") is None

    def test_no_remotes_is_reported(self, home, capsys):
        write_config(home)
        assert main(["remotes"]) == 1
        assert "no remotes" in capsys.readouterr().err

    def test_a_missing_password_file_is_reported_without_connecting(self, home, capsys, monkeypatch):
        monkeypatch.setattr(ops, "tailscale_route", lambda host: None)
        write_config(home, remote_config())
        assert main(["remotes"]) == 1
        out = capsys.readouterr().out
        assert "FAILED" in out and "password file not found" in out

    @requires_rsync
    def test_a_working_and_a_broken_remote(self, home, daemon, capsys, monkeypatch):
        monkeypatch.setattr(ops, "tailscale_route", lambda host: "relay")
        write_config(home, f"remotes:\n  good:\n    host: 127.0.0.1\n    module: backup\n    user: jabs\n"
                           f"    password_file: {daemon.password}\n    port: {daemon.port}\n"
                           f"  dead:\n    host: 127.0.0.1\n    module: backup\n    port: {free_port()}\n"
                           "rsync:\n  contimeout: 2\n")
        assert main(["remotes"]) == 1
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("FAILED") and "dead" in lines[0] and "could not connect" in lines[0]
        assert lines[1].startswith("OK") and "good" in lines[1] and "relaying" in lines[1]
        assert daemon.password.read_text() not in "".join(lines)
