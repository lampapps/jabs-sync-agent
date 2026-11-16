"""Typed, validated configuration: config/config.yaml and one YAML file per job in config/jobs/."""

import os
import re
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import settings

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
    whole_file: bool = True                # skip delta transfer; faster on a LAN or NFS mount
    hard_links: bool = False
    io_timeout: int = Field(300, ge=0)     # rsync --timeout: abort when no data moves for this many seconds
    max_duration: int = Field(0, ge=0)     # hard wall-clock cap for one pair in seconds, 0 = none
    min_free_bytes: int = Field(0, ge=0)   # refuse to sync into a destination with less free space, 0 = no check
    backup_dir: str = ""                   # keep overwritten and deleted files here instead of losing them
    backup_dir_retention_days: int = Field(0, ge=0)
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


class Config(_Model):
    hostname: str = Field("", pattern=HOST_PATTERN)  # reported to the dashboard; default is the system hostname
    use_common_exclude: bool = True
    verify_after_sync: bool = True
    rsync: RsyncOptions = Field(default_factory=RsyncOptions)
    email: EmailConfig = Field(default_factory=EmailConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)


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
    """One source directory mirrored into one destination directory."""

    name: str = Field("", max_length=100)   # default: the source's last path component
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    backup_dir: str = ""

    @field_validator("source", "destination", "backup_dir")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        return _absolute(value)

    @model_validator(mode="after")
    def _no_nesting(self) -> "Pair":
        source, destination = os.path.realpath(self.source), os.path.realpath(self.destination)
        if source == destination or source.startswith(destination + os.sep) or destination.startswith(source + os.sep):
            raise ValueError(f"source {self.source} and destination {self.destination} must not contain each other")
        return self

    def resolved_name(self) -> str:
        return self.name.strip() or os.path.basename(self.source.rstrip("/")) or "pair"


class JobFile(_Model):
    """One config/jobs/*.yaml; `rsync` holds only the keys this job overrides."""

    job_name: Optional[str] = Field(None, min_length=1, max_length=200)
    pairs: list[Pair] = Field(min_length=1)
    schedules: list[Schedule] = Field(default_factory=list)
    verify_after_sync: Optional[bool] = None
    rsync: dict = Field(default_factory=dict)
    exclude: list[str] = Field(default_factory=list)

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


class Job(_Model):
    """A job with the global settings merged in."""

    name: str
    path: str
    pairs: list[Pair]
    schedules: list[Schedule]
    verify_after_sync: bool
    rsync: RsyncOptions
    exclude: list[str]

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
    exclude = (_common_excludes() if cfg.use_common_exclude else []) + raw.exclude
    pairs = [pair.model_copy(update={"name": pair.resolved_name()}) for pair in raw.pairs]
    return Job(
        name=raw.job_name or path.stem, path=str(path), pairs=pairs, schedules=raw.schedules,
        verify_after_sync=cfg.verify_after_sync if raw.verify_after_sync is None else raw.verify_after_sync,
        rsync=RsyncOptions.model_validate({**cfg.rsync.model_dump(), **_set_keys(raw.rsync)}),
        exclude=exclude,
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
