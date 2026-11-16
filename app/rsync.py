"""Wrapper around the rsync binary: mirror one directory into another, and verify that a mirror is complete.

Failures are raised as RsyncError with rsync's stderr attached. This module never logs or swallows them;
the caller decides what a failure means. rsync exit codes 23 and 24 (some files skipped or vanished) are a
warning, not an error.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import settings
from .config import RsyncOptions

PARTIAL_TRANSFER_CODES = (23, 24)

# Per-run --backup-dir subdirectory name; prune_backup_dir() parses it back out.
BACKUP_DIR_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

_STATS_FILES_RE = re.compile(r"Number of regular files transferred:\s*([\d,]+)")
# With --human-readable, large totals look like "10.49M bytes"; the unit letters are 1000-based.
_STATS_BYTES_RE = re.compile(r"Total transferred file size:\s*([\d,]+\.?\d*)\s*([KMGT])?\s*bytes")
_STATS_SIZE_MULTIPLIERS = {None: 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}

# --info=progress2's running overall line, e.g. "  1,234,567  43%   12.34MB/s    0:00:10 (xfr#5, to-chk=120/200)".
# Only percent and rate are parsed, so the leading byte count is matched loosely.
_PROGRESS_RE = re.compile(r"^\s*\S+\s+(\d+)%\s+([\d.]+)([KMGT]?B)/s")
_RATE_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}

# --itemize-changes lines start with an 11 character change code (">f.st.....", "*deleting") and a space.
_ITEMIZE_LINE_RE = re.compile(r"^([><ch.*][fdLDS*].{9}|\*deleting) ")

_ERROR_TAIL_CHARS = 2000


class RsyncError(RuntimeError):
    """rsync could not run, or exited with a code other than success or a partial transfer."""

    def __init__(self, message: str, returncode: Optional[int] = None):
        super().__init__(message)
        self.returncode = returncode


class StopRequest:
    """Ends the running rsync gracefully, from a signal handler or another thread.

    rsync keeps its partial file (--partial-dir), so the next run carries on where this one stopped.
    """

    def __init__(self) -> None:
        self.requested = False
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def request(self) -> None:
        with self._lock:
            self.requested = True
            proc = self._proc
        if proc is not None:
            _terminate(proc)

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            already = self.requested
        if already:
            _terminate(proc)

    def detach(self) -> None:
        with self._lock:
            self._proc = None


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        pass


def _execute(args: list, on_line: Optional[Callable[[str], None]] = None, max_duration: int = 0,
             stop: Optional[StopRequest] = None) -> tuple:
    """Run rsync and return (returncode, stdout, stderr). stdout is streamed to on_line as it arrives."""
    binary = settings.rsync_bin()
    # stderr goes to a file so a flood of errors can never fill a pipe and stall rsync.
    stderr_file = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
    try:
        try:
            proc = subprocess.Popen([binary, *args], stdout=subprocess.PIPE, stderr=stderr_file, text=True,
                                    errors="replace")
        except FileNotFoundError as exc:
            raise RsyncError(f"rsync binary not found: {binary}") from exc
        timed_out = threading.Event()
        timer: Optional[threading.Timer] = None
        if max_duration:
            def expire() -> None:
                timed_out.set()
                _terminate(proc)

            timer = threading.Timer(max_duration, expire)
            timer.daemon = True
            timer.start()
        if stop is not None:
            stop.attach(proc)
        lines = []
        try:
            for line in proc.stdout:
                lines.append(line)
                if on_line is not None:
                    on_line(line)
            returncode = proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            if stop is not None:
                stop.detach()
            proc.stdout.close()
        stderr_file.seek(0)
        stderr = stderr_file.read()
    finally:
        stderr_file.close()
    if timed_out.is_set():
        raise RsyncError(f"rsync exceeded max_duration of {max_duration}s and was stopped")
    return returncode, "".join(lines), stderr


def _failure(returncode: int, stderr: str) -> RsyncError:
    return RsyncError(f"rsync failed (exit {returncode}): {stderr.strip()[-_ERROR_TAIL_CHARS:]}", returncode=returncode)


def parse_progress_line(line: str) -> Optional[dict]:
    """{'percent', 'bytes_per_second'} from one --info=progress2 line, or None for any other line."""
    match = _PROGRESS_RE.match(line)
    if not match:
        return None
    return {"percent": int(match.group(1)), "bytes_per_second": float(match.group(2)) * _RATE_MULTIPLIERS[match.group(3)]}


def parse_stats(stdout: str) -> tuple:
    """(files transferred, bytes transferred) from rsync's --stats block."""
    files_match = _STATS_FILES_RE.search(stdout)
    bytes_match = _STATS_BYTES_RE.search(stdout)
    files = int(files_match.group(1).replace(",", "")) if files_match else 0
    total = 0
    if bytes_match:
        total = int(round(float(bytes_match.group(1).replace(",", "")) * _STATS_SIZE_MULTIPLIERS[bytes_match.group(2)]))
    return files, total


def build_args(options: RsyncOptions, backup_dir: str = "") -> list:
    """The flags shared by every sync run. Ownership is not kept by default: two hosts rarely share uid/gid maps."""
    args = ["--recursive", "--links", "--times", "--devices", "--specials"]
    if options.preserve_permissions:
        args.append("--perms")
    if options.preserve_owner:
        args.append("--owner")
    if options.preserve_group:
        args.append("--group")
    if options.numeric_ids:
        args.append("--numeric-ids")
    if options.hard_links:
        args.append("--hard-links")
    args.append("--whole-file" if options.whole_file else "--no-whole-file")
    if options.delete:
        args += ["--delete", "--delete-excluded"]
    args += ["--partial", "--partial-dir=.rsync-partial", "--sparse", "--force", "--human-readable"]
    if options.io_timeout:
        args.append(f"--timeout={options.io_timeout}")
    if backup_dir:
        args += ["--backup", f"--backup-dir={backup_dir}", "--suffix="]
    return args + list(options.extra_args)


def _endpoints(source: str, destination: str) -> list:
    return [source.rstrip("/") + "/", destination.rstrip("/") + "/"]


def sync(source: str, destination: str, options: RsyncOptions, exclude: Optional[list] = None, backup_dir: str = "",
         dry_run: bool = False, on_progress: Optional[Callable[[dict], None]] = None,
         stop: Optional[StopRequest] = None) -> dict:
    """Mirror source into destination.

    Returns {partial, stopped, files_transferred, bytes_transferred, returncode}. `stopped` is true when a stop was
    requested and rsync ended early; the transfer is then incomplete but not an error.
    """
    args = build_args(options, backup_dir)
    args += [f"--exclude={pattern}" for pattern in exclude or []]
    args.append("--stats")
    if on_progress is not None:
        args.append("--info=progress2")
    if dry_run:
        args.append("--dry-run")
    args += _endpoints(source, destination)

    def on_line(line: str) -> None:
        parsed = parse_progress_line(line)
        if parsed is not None and on_progress is not None:
            on_progress(parsed)

    returncode, stdout, stderr = _execute(args, on_line if on_progress is not None else None,
                                          options.max_duration, stop)
    stopped = bool(stop and stop.requested and returncode != 0)
    if returncode != 0 and returncode not in PARTIAL_TRANSFER_CODES and not stopped:
        raise _failure(returncode, stderr)
    files, transferred = parse_stats(stdout)
    return {"partial": returncode in PARTIAL_TRANSFER_CODES, "stopped": stopped, "files_transferred": files,
            "bytes_transferred": transferred, "returncode": returncode}


def count_diffs(source: str, destination: str, options: RsyncOptions, exclude: Optional[list] = None,
                checksum: bool = False, stop: Optional[StopRequest] = None) -> int:
    """How many items a dry run would still change; 0 means the two sides agree.

    The quick form compares size and mtime only. checksum=True reads every file on both sides (slow).
    """
    args = ["--recursive", "--links", "--times", "--dry-run", "--itemize-changes"]
    if options.io_timeout:
        args.append(f"--timeout={options.io_timeout}")
    if options.delete:
        args += ["--delete", "--delete-excluded"]
    if checksum:
        args.append("--checksum")
    args += [f"--exclude={pattern}" for pattern in exclude or []]
    args += _endpoints(source, destination)
    returncode, stdout, stderr = _execute(args, None, options.max_duration, stop)
    if returncode != 0 and returncode not in PARTIAL_TRANSFER_CODES:
        raise _failure(returncode, stderr)
    return sum(1 for line in stdout.splitlines() if _ITEMIZE_LINE_RE.match(line))


def check_path_accessible(path: str) -> bool:
    """True when path is a directory this process can list."""
    if not os.path.isdir(path):
        return False
    try:
        os.listdir(path)
    except OSError:
        return False
    return True


def free_space_bytes(path: str) -> Optional[int]:
    """Free space on the filesystem holding path, or None when it cannot be read."""
    try:
        stat = os.statvfs(path)
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize


def run_backup_dir(pair_root: str, started: float) -> str:
    """The timestamped directory for one run under a pair's backup_dir root."""
    return os.path.join(pair_root, time.strftime(BACKUP_DIR_TIMESTAMP_FORMAT, time.localtime(started)))


def prune_backup_dir(root: str, retention_days: int) -> int:
    """Delete run directories under root older than retention_days; returns how many went.

    Names that are not timestamps are left alone, and a directory that cannot be removed does not stop the rest.
    """
    if retention_days <= 0 or not os.path.isdir(root):
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            when = datetime.strptime(name, BACKUP_DIR_TIMESTAMP_FORMAT)
        except ValueError:
            continue
        if when < cutoff:
            try:
                shutil.rmtree(path)
            except OSError:
                continue
            removed += 1
    return removed
