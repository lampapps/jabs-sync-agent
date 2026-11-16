import os
import threading
import time
from datetime import datetime, timedelta

import pytest

from app import rsync
from app.config import RsyncOptions
from app.rsync import RsyncError, StopRequest

from .conftest import requires_rsync


def tree(root, files: dict) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def listing(root) -> dict:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


class TestParsing:
    def test_progress_lines(self):
        parsed = rsync.parse_progress_line("      1,234,567  43%   12.34MB/s    0:00:10 (xfr#5, to-chk=120/200)")
        assert parsed["percent"] == 43 and parsed["bytes_per_second"] == pytest.approx(12.34 * 1024 ** 2)
        assert rsync.parse_progress_line("sending incremental file list") is None

    def test_stats(self):
        out = "Number of regular files transferred: 1,234\nTotal transferred file size: 10.49M bytes\n"
        assert rsync.parse_stats(out) == (1234, 10_490_000)
        assert rsync.parse_stats("Total transferred file size: 512 bytes") == (0, 512)
        assert rsync.parse_stats("nothing useful") == (0, 0)


class TestArgs:
    def test_defaults_suit_a_lan_mirror(self):
        args = rsync.build_args(RsyncOptions())
        for flag in ("--recursive", "--perms", "--numeric-ids", "--whole-file", "--delete", "--delete-excluded",
                     "--partial", "--partial-dir=.rsync-partial", "--timeout=300"):
            assert flag in args
        assert "--owner" not in args and "--group" not in args and "--hard-links" not in args

    def test_options_change_the_flags(self):
        args = rsync.build_args(RsyncOptions(delete=False, preserve_owner=True, preserve_group=True,
                                             whole_file=False, hard_links=True, io_timeout=0,
                                             extra_args=["--bwlimit=100"]), backup_dir="/b/run")
        assert "--delete" not in args and "--owner" in args and "--group" in args and "--no-whole-file" in args
        assert "--hard-links" in args and not any(a.startswith("--timeout") for a in args)
        assert "--backup" in args and "--backup-dir=/b/run" in args and args[-1] == "--bwlimit=100"


@requires_rsync
class TestSync:
    def test_a_mirror_copies_new_files_and_removes_deleted_ones(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a", "sub/b.txt": "b"})
        tree(dst, {"gone.txt": "x"})
        result = rsync.sync(str(src), str(dst), RsyncOptions())
        assert listing(dst) == {"a.txt": "a", "sub/b.txt": "b"}
        assert result["files_transferred"] == 2 and result["bytes_transferred"] == 2
        assert not result["partial"] and not result["stopped"]

    def test_delete_can_be_switched_off(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a"})
        tree(dst, {"keep.txt": "k"})
        rsync.sync(str(src), str(dst), RsyncOptions(delete=False))
        assert set(listing(dst)) == {"a.txt", "keep.txt"}

    def test_excludes_are_skipped_and_not_deleted_from_the_source(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a", "skip.tmp": "t", "cache/x": "x"})
        rsync.sync(str(src), str(dst), RsyncOptions(), exclude=["*.tmp", "cache/"])
        assert set(listing(dst)) == {"a.txt"} and (src / "skip.tmp").exists()

    def test_a_dry_run_changes_nothing(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a"})
        dst.mkdir()
        result = rsync.sync(str(src), str(dst), RsyncOptions(), dry_run=True)
        assert listing(dst) == {} and result["returncode"] == 0

    def test_overwritten_and_deleted_files_go_to_the_backup_dir(self, tmp_path):
        src, dst, kept = tmp_path / "src", tmp_path / "dst", tmp_path / "kept"
        tree(src, {"a.txt": "new content"})
        tree(dst, {"a.txt": "old", "gone.txt": "bye"})
        rsync.sync(str(src), str(dst), RsyncOptions(), backup_dir=str(kept))
        assert listing(kept) == {"a.txt": "old", "gone.txt": "bye"} and listing(dst) == {"a.txt": "new content"}

    def test_progress_is_reported_while_rsync_runs(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {f"f{i}.txt": "x" * 1000 for i in range(20)})
        seen = []
        rsync.sync(str(src), str(dst), RsyncOptions(), on_progress=seen.append)
        assert seen and seen[-1]["percent"] == 100

    def test_a_failure_carries_rsyncs_message(self, tmp_path):
        src, blocker = tmp_path / "src", tmp_path / "file"
        tree(src, {"a.txt": "a"})
        blocker.write_text("not a directory")
        with pytest.raises(RsyncError, match="rsync failed \\(exit \\d+\\)") as caught:
            rsync.sync(str(src), str(blocker / "dst"), RsyncOptions())
        assert caught.value.returncode not in (0, 23, 24)

    def test_a_missing_binary_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSYNC_BIN", str(tmp_path / "no-such-rsync"))
        with pytest.raises(RsyncError, match="not found"):
            rsync.sync(str(tmp_path), str(tmp_path / "d"), RsyncOptions())

    def test_max_duration_stops_a_slow_transfer(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        (src / "big.bin").write_bytes(os.urandom(2_000_000))
        options = RsyncOptions(max_duration=1, extra_args=["--bwlimit=100"])
        started = time.monotonic()
        with pytest.raises(RsyncError, match="max_duration"):
            rsync.sync(str(src), str(dst), options)
        assert time.monotonic() - started < 15

    def test_a_stop_request_ends_rsync_and_the_next_run_finishes_the_file(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        payload = os.urandom(3_000_000)
        (src / "big.bin").write_bytes(payload)
        stop = StopRequest()
        threading.Timer(1.0, stop.request).start()
        started = time.monotonic()
        result = rsync.sync(str(src), str(dst), RsyncOptions(extra_args=["--bwlimit=200"]), stop=stop)
        assert result["stopped"] is True and time.monotonic() - started < 12
        assert not (dst / "big.bin").exists()
        result = rsync.sync(str(src), str(dst), RsyncOptions())
        assert result["stopped"] is False and (dst / "big.bin").read_bytes() == payload

    def test_a_stop_requested_before_the_start_ends_immediately(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a"})
        stop = StopRequest()
        stop.request()
        result = rsync.sync(str(src), str(dst), RsyncOptions(), stop=stop)
        assert result["stopped"] is True or (dst / "a.txt").exists()


@requires_rsync
class TestVerify:
    def test_identical_trees_have_no_differences(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a", "sub/b.txt": "b"})
        rsync.sync(str(src), str(dst), RsyncOptions())
        assert rsync.count_diffs(str(src), str(dst), RsyncOptions()) == 0

    def test_missing_extra_and_changed_files_are_counted(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a", "b.txt": "bb", "c.txt": "c"})
        rsync.sync(str(src), str(dst), RsyncOptions())
        (src / "new.txt").write_text("n")          # missing from the destination
        (dst / "extra.txt").write_text("e")        # only on the destination
        (src / "b.txt").write_text("bbbb")         # different size
        assert rsync.count_diffs(str(src), str(dst), RsyncOptions()) == 3

    def test_without_delete_extra_files_are_not_differences(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "a"})
        rsync.sync(str(src), str(dst), RsyncOptions())
        (dst / "extra.txt").write_text("e")
        assert rsync.count_diffs(str(src), str(dst), RsyncOptions(delete=False)) == 0

    def test_only_a_deep_check_sees_same_size_content_changes(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        tree(src, {"a.txt": "aaaa"})
        rsync.sync(str(src), str(dst), RsyncOptions())
        stat = (dst / "a.txt").stat()
        (dst / "a.txt").write_text("bbbb")
        os.utime(dst / "a.txt", (stat.st_atime, stat.st_mtime))  # same size and mtime, different content
        assert rsync.count_diffs(str(src), str(dst), RsyncOptions()) == 0
        assert rsync.count_diffs(str(src), str(dst), RsyncOptions(), checksum=True) == 1


class TestFilesystemHelpers:
    def test_accessible_directories(self, tmp_path):
        assert rsync.check_path_accessible(str(tmp_path))
        assert not rsync.check_path_accessible(str(tmp_path / "nope"))
        (tmp_path / "file").write_text("x")
        assert not rsync.check_path_accessible(str(tmp_path / "file"))

    def test_free_space(self, tmp_path):
        assert rsync.free_space_bytes(str(tmp_path)) > 0
        assert rsync.free_space_bytes(str(tmp_path / "nope")) is None

    def test_prune_removes_only_old_timestamped_runs(self, tmp_path):
        fmt = rsync.BACKUP_DIR_TIMESTAMP_FORMAT
        old = (datetime.now() - timedelta(days=40)).strftime(fmt)
        recent = (datetime.now() - timedelta(days=2)).strftime(fmt)
        for name in (old, recent, "not-a-timestamp"):
            (tmp_path / name).mkdir()
        (tmp_path / "stray-file").write_text("x")
        assert rsync.prune_backup_dir(str(tmp_path), 30) == 1
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted([recent, "not-a-timestamp", "stray-file"])

    def test_prune_with_no_retention_keeps_everything(self, tmp_path):
        (tmp_path / "19990101_000000").mkdir()
        assert rsync.prune_backup_dir(str(tmp_path), 0) == 0 and rsync.prune_backup_dir("/no/such/dir", 30) == 0

    def test_run_backup_dir_is_a_timestamp_under_the_pair_root(self):
        path = rsync.run_backup_dir("/b/job_pair", time.mktime((2027, 1, 15, 3, 4, 5, 0, 0, -1)))
        assert path == "/b/job_pair/20270115_030405"
