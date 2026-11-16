import subprocess
import sys
from pathlib import Path

import pytest

from app import __main__ as cli
from app import ops, runner, state
from app.config import ConfigError

from .conftest import requires_rsync, write_config, write_job
from .test_runner import put

JOB = ('job_name: "{name}"\npairs:\n  - source: {src}\n    destination: {dst}\n'
       'schedules:\n  - cron: "{cron}"\n    enabled: true\n')


def run(*argv):
    return cli.main(list(argv))


@pytest.fixture
def media(home, tmp_path):
    write_config(home, "use_common_exclude: false\n")
    write_job(home, "media", JOB.format(name="media", src=tmp_path / "src", dst=tmp_path / "dst", cron="0 2 * * *"))
    write_job(home, "manual", f'pairs:\n  - source: {tmp_path / "m_src"}\n    destination: {tmp_path / "m_dst"}\n')
    return tmp_path


class TestConfigAndJobs:
    def test_config_lists_jobs_pairs_and_schedules(self, media, capsys):
        assert run("config") == 0
        out = capsys.readouterr().out
        assert "OK" in out and "standalone" in out and "media: 1 pair(s), 0 2 * * *" in out
        assert "manual: 1 pair(s), no schedule enabled" in out

    def test_config_fails_on_a_broken_job_and_says_which(self, media, home, capsys):
        write_job(home, "bad", "pairs: []\n")
        assert run("config") == 1
        assert "bad.yaml" in capsys.readouterr().err

    def test_a_missing_config_is_a_clean_error(self, home, capsys):
        assert run("config") == 1
        assert "config.example.yaml" in capsys.readouterr().err

    def test_jobs_shows_next_and_last_run(self, media, capsys):
        state.record_run("media", {"success": True, "finished_at": 1_800_000_000})
        state.record_run("manual", {"success": True, "stopped": True, "finished_at": 1_800_000_000})
        assert run("jobs") == 0
        lines = {line.split("\t")[0]: line for line in capsys.readouterr().out.splitlines()}
        assert "next 20" in lines["media"] and "last ok" in lines["media"]
        assert "next -" in lines["manual"] and "last stopped" in lines["manual"]


@requires_rsync
class TestRunAndVerify:
    def test_run_mirrors_and_exits_zero(self, media):
        put(media / "src", {"a.txt": "a"})
        assert run("run", "media") == 0
        assert (media / "dst" / "a.txt").read_text() == "a"

    def test_run_exits_nonzero_when_a_pair_fails(self, media):
        assert run("run", "media") == 1

    def test_a_dry_run_changes_nothing(self, media):
        put(media / "src", {"a.txt": "a"})
        assert run("run", "media", "--dry-run") == 0
        assert not (media / "dst").exists()

    def test_an_unknown_job_is_a_clean_error(self, media, capsys):
        assert run("run", "nope") == 1
        assert "not found" in capsys.readouterr().err

    def test_verify_reports_ok_mismatch_and_skipped(self, media, capsys):
        put(media / "src", {"a.txt": "a"})
        run("run", "media")
        capsys.readouterr()
        assert run("verify", "media") == 0
        assert "OK" in capsys.readouterr().out
        put(media / "src", {"new.txt": "n"})
        assert run("verify", "media") == 1
        assert "MISMATCH" in capsys.readouterr().out
        assert run("verify") == 1
        out = capsys.readouterr().out
        assert "SKIPPED" in out and "manual:" in out  # the manual job's folders do not exist

    def test_a_deep_verify_is_labelled(self, media, capsys):
        put(media / "src", {"a.txt": "a"})
        run("run", "media")
        capsys.readouterr()
        assert run("verify", "media", "--deep") == 0
        assert "deep" in capsys.readouterr().out


class TestOtherCommands:
    def test_stop_says_when_nothing_is_running(self, media, capsys):
        assert run("stop", "media") == 1
        assert "not running" in capsys.readouterr().err

    def test_paths_lists_the_data_dir_and_destinations(self, media, capsys, home):
        assert run("paths") == 0
        listed = capsys.readouterr().out.split()
        assert str(home / "data") in listed and str(media / "dst") in listed

    def test_reset_needs_confirmation(self, media, capsys):
        state.record_run("media", {"success": True, "finished_at": 1})
        assert run("reset") == 2
        assert state.read()
        assert run("reset", "--yes") == 0
        assert state.read() == {}

    def test_testmail_needs_email_settings(self, media, capsys):
        assert run("testmail") == 1
        assert "not configured" in capsys.readouterr().err

    def test_testmail_sends_with_the_configured_server(self, media, home, monkeypatch):
        write_config(home, "email:\n  smtp_server: smtp.example.com\n  to_addrs: [a@example.com]\n")
        sent = []
        monkeypatch.setattr(cli.mailer, "send", lambda email, subject, body: sent.append(subject) or True)
        assert run("testmail") == 0 and sent == ["JABS sync agent test message"]


class TestOps:
    def test_format_last(self):
        assert ops.format_last(None) == "-"
        assert ops.format_last({"success": False, "finished_at": 0}).startswith("FAILED")
        assert ops.format_last({"success": True, "stopped": True, "finished_at": 0}).startswith("stopped")

    def test_writable_paths_include_backup_dirs(self, home, tmp_path):
        write_config(home, f"rsync:\n  backup_dir: {tmp_path / 'kept'}\n")
        write_job(home, "m", f"pairs:\n  - source: {tmp_path / 's'}\n    destination: {tmp_path / 'd'}\n")
        from app.config import load_config
        assert ops.writable_paths(load_config()) == sorted([str(home / "data"), str(tmp_path / "d"),
                                                            str(tmp_path / "kept")])

    def test_stop_job_signals_the_process_holding_the_lock(self, home):
        lock = runner.lock_path("held")
        code = ("import sys, time; sys.path.insert(0, %r); from app import locking; "
                "held = locking.acquire_lock(%r); print('locked', flush=True); time.sleep(60)") % (
                    str(Path(runner.__file__).parent.parent), lock)
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "locked"
            assert ops.job_lock_info("held")["pid"] == child.pid
            assert ops.stop_job("held") is True
            assert child.wait(timeout=10) != 0
        finally:
            child.kill()
            child.wait()
        assert ops.stop_job("held") is False

    def test_stop_job_refuses_when_the_holder_is_unknown(self, home, monkeypatch):
        monkeypatch.setattr(ops, "job_lock_info", lambda name: {})
        with pytest.raises(ConfigError, match="process id"):
            ops.stop_job("x")
