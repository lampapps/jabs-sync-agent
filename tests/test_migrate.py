import yaml

from app import __main__ as cli
from app import config, migrate, settings

from .conftest import write_config

OLD_GLOBAL = """
use_common_exclude: true
delete: false
preserve_permissions: true
io_timeout: 120
min_free_bytes: 0
verify_after_sync: true
mystery_setting: 1
email:
  smtp_server: smtp.example.com
  smtp_port: 587
  to_addrs: [a@example.com]
  use_tls: true
  notify_on:
    error: {enabled: true}
    backup_complete: {enabled: false}
"""

OLD_JOB = """
job_name: "media"
paths:
  - name: "Music"
    source: "/data/Music"
    destination: "/nas/music"
    backup_dir: "/nas/deleted/music"
  - source: "/data/Videos"
    destination: "/nas/videos"
    exclude: ["*.iso"]
schedules:
  - cron: "0 0 * * *"
    enabled: true
exclude: ["Downloads/"]
delete: true
backup_dir_retention_days: 30
"""


def old_install(tmp_path, global_text=OLD_GLOBAL, jobs=None):
    root = tmp_path / "old"
    (root / "config" / "jobs").mkdir(parents=True, exist_ok=True)
    if global_text is not None:
        (root / "config" / "global.yaml").write_text(global_text)
    for name, text in (jobs if jobs is not None else {"media": OLD_JOB}).items():
        (root / "config" / "jobs" / f"{name}.yaml").write_text(text)
    return root


def loaded(path):
    return yaml.safe_load(path.read_text())


class TestConversion:
    def test_global_settings_move_under_rsync_and_unknown_keys_are_reported(self):
        new, warnings = migrate.convert_global(yaml.safe_load(OLD_GLOBAL))
        assert new["rsync"] == {"delete": False, "preserve_permissions": True, "io_timeout": 120, "min_free_bytes": 0}
        assert new["verify_after_sync"] is True and new["email"]["smtp_server"] == "smtp.example.com"
        assert any("mystery_setting" in w for w in warnings)

    def test_a_job_becomes_pairs_with_job_level_rsync_overrides(self):
        new, warnings = migrate.convert_job(yaml.safe_load(OLD_JOB), keep_schedules=False)
        assert warnings == []
        assert new["pairs"][0] == {"name": "Music", "source": "/data/Music", "destination": "/nas/music",
                                   "backup_dir": "/nas/deleted/music"}
        assert new["pairs"][1]["exclude"] == ["*.iso"] and "name" not in new["pairs"][1]
        assert new["exclude"] == ["Downloads/"]
        assert new["rsync"] == {"delete": True, "backup_dir_retention_days": 30}

    def test_schedules_come_in_off_unless_kept(self):
        raw = yaml.safe_load(OLD_JOB)
        assert migrate.convert_job(raw, False)[0]["schedules"] == [{"cron": "0 0 * * *", "enabled": False}]
        assert migrate.convert_job(raw, True)[0]["schedules"] == [{"cron": "0 0 * * *", "enabled": True}]

    def test_unknown_job_and_pair_keys_are_reported(self):
        raw = {"paths": [{"source": "/a", "destination": "/b", "mode": "x"}], "surprise": 1}
        _, warnings = migrate.convert_job(raw, False)
        assert any("surprise" in w for w in warnings) and any("'mode'" in w for w in warnings)


class TestImport:
    def test_a_full_import_writes_files_this_agent_can_load(self, home, tmp_path):
        report = migrate.import_local_sync(old_install(tmp_path))
        assert report["errors"] == [] and sorted(report["written"]) == ["config.yaml", "jobs/media.yaml"]
        cfg = config.load_config()
        (job,) = config.load_jobs(cfg)[0]
        assert cfg.rsync.delete is False and cfg.email.smtp_server == "smtp.example.com"
        assert [job.label(p) for p in job.pairs] == ["media:Music", "media:Videos"]
        assert job.rsync.delete is True and job.rsync.io_timeout == 120 and not job.enabled_crons()

    def test_keeping_schedules_enables_them(self, home, tmp_path):
        migrate.import_local_sync(old_install(tmp_path), keep_schedules=True)
        (job,) = config.load_jobs(config.load_config())[0]
        assert job.enabled_crons() == ["0 0 * * *"]

    def test_a_dry_run_writes_nothing(self, home, tmp_path):
        report = migrate.import_local_sync(old_install(tmp_path), dry_run=True)
        assert sorted(report["written"]) == ["config.yaml", "jobs/media.yaml"]
        assert not settings.CONFIG_PATH.exists() and not list(settings.JOBS_DIR.glob("*.yaml"))

    def test_existing_files_are_kept_unless_forced(self, home, tmp_path):
        write_config(home, "verify_after_sync: false\n")
        (settings.JOBS_DIR / "media.yaml").write_text("mine: true\n")
        report = migrate.import_local_sync(old_install(tmp_path))
        assert sorted(report["kept"]) == ["config.yaml", "jobs/media.yaml"] and report["written"] == []
        assert settings.CONFIG_PATH.read_text() == "verify_after_sync: false\n"
        report = migrate.import_local_sync(old_install(tmp_path), force=True)
        assert sorted(report["written"]) == ["config.yaml", "jobs/media.yaml"]
        assert loaded(settings.JOBS_DIR / "media.yaml")["pairs"]

    def test_a_config_that_is_still_the_setup_starter_is_replaced(self, home, tmp_path):
        starter = "# starter\nverify_after_sync: true\n"
        (settings.CONFIG_DIR / "config.example.yaml").write_text(starter)
        write_config(home, starter)
        report = migrate.import_local_sync(old_install(tmp_path))
        assert "config.yaml" in report["written"] and loaded(settings.CONFIG_PATH)["rsync"]["delete"] is False

    def test_invalid_converted_files_are_reported_and_not_written(self, home, tmp_path):
        bad_job = "job_name: bad\npaths:\n  - source: relative\n    destination: /b\n"
        report = migrate.import_local_sync(old_install(tmp_path, jobs={"bad": bad_job, "media": OLD_JOB}))
        assert any("jobs/bad.yaml" in e and "absolute" in e for e in report["errors"])
        assert report["written"].count("jobs/media.yaml") == 1 and not (settings.JOBS_DIR / "bad.yaml").exists()

    def test_the_common_exclude_list_is_copied(self, home, tmp_path):
        root = old_install(tmp_path)
        (root / "config" / "common_exclude.yaml").write_text('- "*.tmp"\n')
        report = migrate.import_local_sync(root)
        assert "common_exclude.yaml" in report["written"] and settings.COMMON_EXCLUDE_PATH.read_text() == '- "*.tmp"\n'

    def test_a_folder_without_config_is_an_error(self, home, tmp_path):
        report = migrate.import_local_sync(tmp_path / "nothing-here")
        assert report["errors"] and "not found" in report["errors"][0] and report["written"] == []

    def test_a_missing_global_file_still_imports_the_jobs(self, home, tmp_path):
        report = migrate.import_local_sync(old_install(tmp_path, global_text=None))
        assert report["written"] == ["jobs/media.yaml"] and any("global.yaml" in w for w in report["warnings"])

    def test_the_notes_mention_schedules_and_the_env_file(self, home, tmp_path):
        notes = " ".join(migrate.import_local_sync(old_install(tmp_path))["notes"])
        assert "switched off" in notes and "JABS_SMTP_USERNAME" in notes


class TestCommand:
    def test_the_command_reports_what_it_did(self, home, tmp_path, capsys):
        assert cli.main(["import-local-sync", str(old_install(tmp_path)), "--dry-run"]) == 0
        out = capsys.readouterr()
        assert "would write config.yaml" in out.out and "would write jobs/media.yaml" in out.out
        assert "mystery_setting" in out.err

    def test_it_works_before_a_config_exists(self, home, tmp_path):
        assert not settings.CONFIG_PATH.exists()
        assert cli.main(["import-local-sync", str(old_install(tmp_path))]) == 0
        assert settings.CONFIG_PATH.exists()

    def test_errors_give_a_failing_exit_status(self, home, tmp_path, capsys):
        assert cli.main(["import-local-sync", str(tmp_path / "missing")]) == 1
        assert "error:" in capsys.readouterr().err
