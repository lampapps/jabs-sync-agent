"""Pair endpoints: a local absolute path, or `remote:path` inside the module of a named rsync daemon."""

import os
import re
from typing import NamedTuple, Optional

_REMOTE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(.*)$")


class Endpoint(NamedTuple):
    remote: Optional[str]  # name from `remotes:` in config.yaml, None for a local path
    path: str              # absolute local path, or the path inside the module without a leading slash

    def __str__(self) -> str:
        return f"{self.remote}:{self.path}" if self.remote else self.path


def parse(text: str) -> Endpoint:
    """Endpoint of a pair's source or destination; ValueError when it is neither form."""
    if text.startswith("/"):
        return Endpoint(None, text)
    match = _REMOTE.match(text)
    if match:
        path = match.group(2).strip("/")
        if ".." in path.split("/"):
            raise ValueError("a remote path must not contain '..'")
        return Endpoint(match.group(1), path)
    raise ValueError("must be an absolute path, or remote:path for a remote from `remotes:` in config.yaml")


def overlap(a: Endpoint, b: Endpoint) -> bool:
    """True when the two endpoints are the same folder or one contains the other (on the same machine)."""
    if a.remote != b.remote:
        return False
    if a.remote is None:
        first, second = os.path.realpath(a.path), os.path.realpath(b.path)
    else:
        first, second = a.path, b.path
    if first == second:
        return True
    return first == "" or second == "" or first.startswith(second + "/") or second.startswith(first + "/")
