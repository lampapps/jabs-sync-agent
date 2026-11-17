"""Typed, validated configuration: config/config.yaml and one YAML file per job in config/jobs/."""

import os
import re
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import endpoint, settings

PathLike = Union[str, Path]

HOST_PATTERN = r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})?$"
EMAIL_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")


class ConfigError(Exception):
    """Raised for a missing or invalid configuration file, with a readable message."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_default(cls, data: Any) -> Any:
        # A key left empty in YAML loads as None; treat it as not set.
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


def _absolute(value: str) -> str:
    if value and not os.path.isabs(value):
        raise ValueError("must be an absolute path")
    return value


class RsyncOptions(_Model):
    delete: bool = True                    # exact mirror: files removed from the source are removed from the destination
    preserve_owner: bool = False
    preserve_group: bool = False
    preserve_permissions: bool = True
    numeric_ids: bool = True
    whole_file: Optional[bool] = None      # copy whole files; None = auto (yes between local paths, no with a remote)
    hard_links: bool = False
    io_timeout: int = Field(300, ge=0)     # rsync --timeout: abort when no data moves for this many seconds
    max_duration: int = Field(0, ge=0)     # hard wall-clock cap for one pair in seconds, 0 = none
    min_free_bytes: int = Field(0, ge=0)   # refuse to sync into a destination with less free space, 0 = no check
    backup_dir: str = ""                   # keep overwritten and deleted files here instead of losing them
    backup_dir_retention_days: int = Field(0, ge=0)
    bwlimit: int = Field(0, ge=0)          # KiB/s cap on rsync's transfer rate, 0 = unlimited
    compress: bool = False                 # compress in transit; helps on a slow link, costs CPU
    contimeout: int = Field(10, ge=1)      # seconds to wait for a remote rsync daemon to accept the connection
    max_delete: int = Field(0, ge=0)       # fail the pair rather than delete more files than this, 0 = no limit
    refuse_empty_source: bool = True       # with delete on, a source with no files fails the pair (a dropped mount looks empty)
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("backup_dir")
    @classmethod
    def _backup_dir_absolute(cls, value: str) -> str:
        return _absolute(value)


class NotifyEvent(_Model):
    enabled: bool = False


class NotifyOn(_Model):
    error: NotifyEvent = Field(default_factory=lambda: NotifyEvent(enabled=True))
    backup_complete: NotifyEvent = Field(default_factory=NotifyEvent)


class EmailConfig(_Model):
    smtp_server: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    use_tls: bool = True
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    notify_on: NotifyOn = Field(default_factory=NotifyOn)

    @field_validator("to_addrs")
    @classmethod
    def _check_recipients(cls, value: list) -> list:
        for item in value:
            if not EMAIL_ADDRESS.match(item):
                raise ValueError(f"'{item}' is not an email address")
        return value

    @field_validator("from_addr")
    @classmethod
    def _check_sender(cls, value: str) -> str:
        if value and not EMAIL_ADDRESS.match(value):
            raise ValueError(f"'{value}' is not an email address")
        return value


class DashboardConfig(_Model):
    enabled: bool = True   # false forces standalone mode even when JABS_DASHBOARD_URL is set


class Remote(_Model):
    """An rsync daemon (module) a pair can read from or write to as `name:path`."""

    host: str = Field(min_length=1)
    module: str = Field(min_length=1)
    user: str = ""
    password_file: str = ""      # absolute path of a chmod 600 file holding only the module's password
    port: int = Field(873, ge=1, le=65535)

    @field_validator("password_file")
    @classmethod
    def _password_file_absolute(cls, value: str) -> str:
        return _absolute(value)


class Config(_Model):
    hostname: str = Field("", pattern=HOST_PATTERN)  # reported to the dashboard; default is the system hostname
    use_common_exclude: bool = True
    verify_after_sync: bool = True
    rsync: RsyncOptions = Field(default_factory=RsyncOptions)
    remotes: dict[str, Remote] = Field(default_factory=dict)
    email: EmailConfig = Field(default_factory=EmailConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)

    @field_validator("remotes")
    @classmethod
    def _remote_names(cls, value: dict) -> dict:
        for name in value:
            if not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", name):
                raise ValueError(f"remote name '{name}' must start with a letter and use letters, digits, - and _")
        return value


class Schedule(_Model):
    cron: str
    enabled: bool = False

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("must be a valid 5-field cron expression")
        return value


class Pair(_Model):
    """One source directory mirrored into one destination directory (each local, or `remote:path`)."""

    name: str = Field("", max_length=100)   # default: the source's last path component
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    backup_dir: str = ""

    @field_validator("source", "destination")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        endpoint.parse(value)
        return value

    @field_validator("backup_dir")
    @classmethod
    def _backup_dir_absolute(cls, value: str) -> str:
        return _absolute(value)

    @model_validator(mode="after")
    def _compatible_endpoints(self) -> "Pair":
        source, destination = endpoint.parse(self.source), endpoint.parse(self.destination)
        if source.remote and destination.remote:
            raise ValueError("rsync cannot copy between two remotes; one side must be a local path")
        if endpoint.overlap(source, destination):
            raise ValueError(f"source {self.source} and destination {self.destination} must not contain each other")
        if self.backup_dir and destination.remote:
            raise ValueError("backup_dir is not supported when the destination is a remote")
        return self

    def source_endpoint(self) -> endpoint.Endpoint:
        return endpoint.parse(self.source)

    def destination_endpoint(self) -> endpoint.Endpoint:
        return endpoint.parse(self.destination)

    def uses_remote(self) -> bool:
        return bool(self.source_endpoint().remote or self.destination_endpoint().remote)

    def resolved_name(self) -> str:
        source = self.source_endpoint()
        return self.name.strip() or os.path.basename(source.path.rstrip("/")) or source.remote or "pair"


class JobFile(_Model):
    """One config/jobs/*.yaml; `rsync` holds only the keys this job overrides."""

    job_name: Optional[str] = Field(None, min_length=1, max_length=200)
    pairs: list[Pair] = Field(min_length=1)
    schedules: list[Schedule] = Field(default_factory=list)
    verify_after_sync: Optional[bool] = None
    rsync: dict = Field(default_factory=dict)
    exclude: list[str] = Field(default_factory=list)
    use_common_exclude: Optional[bool] = None   # None = the global setting
    stop_at: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # HH:MM local time

    @field_validator("rsync")
    @classmethod
    def _check_rsync(cls, value: dict) -> dict:
        RsyncOptions.model_validate(value)
        return value

    @model_validator(mode="after")
    def _unique_pair_names(self) -> "JobFile":
        names = [pair.resolved_name() for pair in self.pairs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"pair names must be unique within a job (repeated: {', '.join(duplicates)}); "
                             "set `name` on each pair")
        return self

    @model_validator(mode="after")
    def _pairs_do_not_feed_each_other(self) -> "JobFile":
        """With delete on, one pair writing where another reads or writes would delete data and can loop."""
        for i, first in enumerate(self.pairs):
            for second in self.pairs[i + 1:]:
                for a, b in ((first.destination, second.source), (second.destination, first.source),
                             (first.destination, second.destination)):
                    if endpoint.overlap(endpoint.parse(a), endpoint.parse(b)):
                        raise ValueError(
                            f"pairs '{first.resolved_name()}' and '{second.resolved_name()}' overlap: {a} and {b} are "
                            "the same folder or one contains the other")
        return self


class Job(_Model):
    """A job with the global settings merged in."""

    name: str
    path: str
    pairs: list[Pair]
    schedules: list[Schedule]
    verify_after_sync: bool
    rsync: RsyncOptions
    exclude: list[str]
    stop_at: Optional[str] = None

    def enabled_crons(self) -> list:
        return [s.cron for s in self.schedules if s.enabled]

    def label(self, pair: Pair) -> str:
        """The dashboard's name for a pair; it stays the same on every run so runs group together."""
        return f"{self.name}:{pair.resolved_name()}"

    def pair_excludes(self, pair: Pair) -> list:
        return self.exclude + pair.exclude

    def pair_backup_dir(self, pair: Pair) -> str:
        return pair.backup_dir or self.rsync.backup_dir


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(top level)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc


def _read_mapping(path: Path) -> dict:
    data = _read_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(path: Optional[PathLike] = None) -> Config:
    """Load and validate config/config.yaml."""
    path = Path(path or settings.CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"{path} not found; copy config/config.example.yaml to config/config.yaml")
    try:
        return Config.model_validate(_read_mapping(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def job_files() -> list:
    return sorted(settings.JOBS_DIR.glob("*.yaml")) if settings.JOBS_DIR.is_dir() else []


def _common_excludes() -> list:
    if not settings.COMMON_EXCLUDE_PATH.exists():
        return []
    data = _read_yaml(settings.COMMON_EXCLUDE_PATH)
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ConfigError(f"{settings.COMMON_EXCLUDE_PATH}: must be a list of patterns")
    return data


def _set_keys(overrides: dict) -> dict:
    return {key: value for key, value in overrides.items() if value is not None}


def load_job(path: PathLike, cfg: Config) -> Job:
    """Load one job file and merge the global settings into it."""
    path = Path(path)
    try:
        raw = JobFile.model_validate(_read_mapping(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc
    use_common = cfg.use_common_exclude if raw.use_common_exclude is None else raw.use_common_exclude
    exclude = (_common_excludes() if use_common else []) + raw.exclude
    pairs = [pair.model_copy(update={"name": pair.resolved_name()}) for pair in raw.pairs]
    rsync = RsyncOptions.model_validate({**cfg.rsync.model_dump(), **_set_keys(raw.rsync)})
    for pair in pairs:
        for text in (pair.source, pair.destination):
            remote = endpoint.parse(text).remote
            if remote and remote not in cfg.remotes:
                known = ", ".join(sorted(cfg.remotes)) or "none"
                raise ConfigError(f"{path}: pair '{pair.name}' uses the remote '{remote}', which is not in `remotes:` "
                                  f"in config.yaml (defined: {known})")
        if rsync.backup_dir and not pair.backup_dir and pair.destination_endpoint().remote:
            raise ConfigError(f"{path}: pair '{pair.name}' writes to a remote, so the job's backup_dir cannot apply to it; "
                              "it is not supported for remote destinations")
    return Job(
        name=raw.job_name or path.stem, path=str(path), pairs=pairs, schedules=raw.schedules,
        verify_after_sync=cfg.verify_after_sync if raw.verify_after_sync is None else raw.verify_after_sync,
        rsync=rsync, exclude=exclude, stop_at=raw.stop_at,
    )


def find_job(ref: str, cfg: Config) -> Job:
    """Resolve a job given as a name (config/jobs/NAME.yaml), a job_name, or a path to a YAML file."""
    candidate = Path(ref)
    if ref.endswith((".yaml", ".yml")) and candidate.exists():
        return load_job(candidate, cfg)
    by_file = settings.JOBS_DIR / f"{ref}.yaml"
    if by_file.exists():
        return load_job(by_file, cfg)
    jobs, _ = load_jobs(cfg)
    for job in jobs:
        if job.name == ref:
            return job
    raise ConfigError(f"job '{ref}' not found (looked for {by_file})")


def load_jobs(cfg: Config) -> tuple:
    """Every job file as (jobs, errors); one broken file never hides the others."""
    jobs, errors = [], []
    for path in job_files():
        try:
            jobs.append(load_job(path, cfg))
        except ConfigError as exc:
            errors.append(str(exc))
    names = [job.name for job in jobs]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"job name '{name}' is used by more than one file in {settings.JOBS_DIR}")
    return jobs, errors
