"""Import a local_sync_agent install's configuration (global.yaml, jobs/*.yaml, common_exclude.yaml) into this agent."""

from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from . import config, settings

# local_sync_agent kept these rsync settings flat in global.yaml and in each job; here they live under `rsync:`.
RSYNC_KEYS = ("delete", "preserve_owner", "preserve_group", "preserve_permissions", "numeric_ids", "whole_file",
              "hard_links", "io_timeout", "max_duration", "min_free_bytes", "backup_dir", "backup_dir_retention_days",
              "extra_args")
GLOBAL_KEYS = RSYNC_KEYS + ("use_common_exclude", "verify_after_sync", "email")
JOB_KEYS = RSYNC_KEYS + ("job_name", "paths", "schedules", "exclude", "verify_after_sync")
PAIR_KEYS = ("name", "source", "destination", "exclude", "backup_dir")

HEADER = "# Imported from local_sync_agent on {today}. config.example.yaml and templates/job.yaml list every setting.\n"


def _set(raw: dict) -> dict:
    return {key: value for key, value in raw.items() if value is not None}


def convert_global(raw: dict) -> tuple:
    """(config.yaml content, warnings) from a local_sync_agent global.yaml."""
    warnings = [f"global.yaml: '{key}' is not used by this agent and was dropped" for key in raw if key not in GLOBAL_KEYS]
    raw = _set(raw)
    new = {key: raw[key] for key in ("use_common_exclude", "verify_after_sync") if key in raw}
    rsync = {key: raw[key] for key in RSYNC_KEYS if key in raw}
    if rsync:
        new["rsync"] = rsync
    if raw.get("email"):
        new["email"] = raw["email"]
    return new, warnings


def convert_job(raw: dict, keep_schedules: bool) -> tuple:
    """(job file content, warnings) from a local_sync_agent job file. Schedules are switched off unless kept."""
    warnings = [f"'{key}' is not used by this agent and was dropped" for key in raw if key not in JOB_KEYS]
    raw = _set(raw)
    new = {}
    if raw.get("job_name"):
        new["job_name"] = raw["job_name"]
    pairs = []
    for old in raw.get("paths") or []:
        pairs.append({key: old[key] for key in PAIR_KEYS if old.get(key)})
        warnings += [f"pair '{old.get('name') or old.get('source')}': '{key}' was dropped" for key in old if key not in PAIR_KEYS]
    new["pairs"] = pairs
    schedules = [{"cron": s.get("cron"), "enabled": bool(s.get("enabled")) and keep_schedules}
                 for s in raw.get("schedules") or [] if isinstance(s, dict)]
    if schedules:
        new["schedules"] = schedules
    for key in ("verify_after_sync", "exclude"):
        if key in raw:
            new[key] = raw[key]
    rsync = {key: raw[key] for key in RSYNC_KEYS if key in raw}
    if rsync:
        new["rsync"] = rsync
    return new, warnings


def _dump(data: dict) -> str:
    return HEADER.format(today=date.today().isoformat()) + yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data if isinstance(data, dict) else {}


def _check(model, data: dict, label: str, errors: list) -> bool:
    try:
        model.model_validate(data)
    except ValidationError as exc:
        errors.append(f"{label}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
        return False
    return True


def _is_starter(path: Path, starter: Path) -> bool:
    """True when `path` is still the untouched starter file that setup.sh copied."""
    return starter.exists() and path.read_text(encoding="utf-8") == starter.read_text(encoding="utf-8")


def import_local_sync(source: Path, keep_schedules: bool = False, force: bool = False, dry_run: bool = False) -> dict:
    """Convert the configuration of the local_sync_agent install at `source` and write it here.

    Existing files are kept unless `force` (a config.yaml that is still the setup.sh starter counts as unused). Files
    that do not validate are not written. Returns {"written", "kept", "warnings", "errors", "notes"}.
    """
    report: dict = {"written": [], "kept": [], "warnings": [], "errors": [], "notes": []}
    old_config = Path(source).expanduser() / "config"
    if not old_config.is_dir():
        report["errors"].append(f"{old_config} not found; give the folder of the local_sync_agent install")
        return report

    plan = []  # (target path, text, label)
    old_global = old_config / "global.yaml"
    if old_global.exists():
        new, warnings = convert_global(_read(old_global))
        report["warnings"] += warnings
        if _check(config.Config, new, "config.yaml", report["errors"]):
            plan.append((settings.CONFIG_PATH, _dump(new), "config.yaml"))
    else:
        report["warnings"].append("no config/global.yaml in that install; config.yaml was not created")

    old_exclude = old_config / "common_exclude.yaml"
    if old_exclude.exists():
        plan.append((settings.COMMON_EXCLUDE_PATH, old_exclude.read_text(encoding="utf-8"), "common_exclude.yaml"))

    for path in sorted((old_config / "jobs").glob("*.yaml")):
        try:
            new, warnings = convert_job(_read(path), keep_schedules)
        except (yaml.YAMLError, OSError) as exc:
            report["errors"].append(f"jobs/{path.name}: cannot be read: {exc}")
            continue
        report["warnings"] += [f"jobs/{path.name}: {w}" for w in warnings]
        if _check(config.JobFile, new, f"jobs/{path.name}", report["errors"]):
            plan.append((settings.JOBS_DIR / path.name, _dump(new), f"jobs/{path.name}"))

    starter = settings.CONFIG_DIR / "config.example.yaml"
    for target, text, label in plan:
        unused = not target.exists() or (target == settings.CONFIG_PATH and _is_starter(target, starter))
        if not (force or unused):
            report["kept"].append(label)
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        report["written"].append(label)

    if not keep_schedules:
        report["notes"].append("Schedules were imported switched off so both agents never run the same job; enable "
                               "them here once the old agent's schedule is removed.")
    report["notes"].append("The SMTP login lives in the old install's .env; copy JABS_SMTP_USERNAME and "
                           "JABS_SMTP_PASSWORD (and the dashboard URL and key, if used) into this .env.")
    return report
