import re
from pathlib import Path

import pytest
import yaml

from app import config
from app.config import ConfigError

from .conftest import write_config, write_job, jid


def pairs_job(*pairs: str, extra: str = "") -> str:
    body = "pairs:\n" + "".join(pairs)
    return body + extra


def pair(source: str, destination: str, extra: str = "") -> str:
    return f"  - source: {source}\n    destination: {destination}\n{extra}"


class TestGlobal:
    def test_an_empty_config_uses_defaults(self, home):
        write_config(home)
        cfg = config.load_config()
        assert cfg.rsync.delete is True and cfg.rsync.io_timeout == 300 and cfg.verify_after_sync is True
        assert cfg.email.notify_on.error.enabled is True and cfg.email.notify_on.backup_complete.enabled is False
        assert cfg.dashboard.enabled is True

    def test_blank_yaml_keys_use_defaults(self, home):
        write_config(home, "rsync:\n  backup_dir:\nemail:\n  to_addrs:\n")
        cfg = config.load_config()
        assert cfg.rsync.backup_dir == "" and cfg.email.to_addrs == []

    def test_a_missing_file_says_what_to_do(self, home):
        with pytest.raises(ConfigError, match="config.example.yaml"):
            config.load_config()

    def test_unknown_keys_are_rejected(self, home):
        write_config(home, "rsycn: {}\n")
        with pytest.raises(ConfigError, match="rsycn"):
            config.load_config()

    @pytest.mark.parametrize("text, message", [
        ("rsync:\n  backup_dir: relative/dir\n", "absolute"),
        ("rsync:\n  io_timeout: -1\n", "io_timeout"),
        ("email:\n  to_addrs: [nobody]\n", "not an email address"),
        ("hostname: bad host\n", "hostname"),
    ])
    def test_invalid_values_name_the_key(self, home, text, message):
        write_config(home, text)
        with pytest.raises(ConfigError, match=message):
            config.load_config()


class TestJobs:
    def test_a_job_merges_the_global_settings(self, home):
        write_config(home, "verify_after_sync: false\nrsync:\n  io_timeout: 60\n  delete: false\n")
        write_job(home, "m", pairs_job(pair("/a/in", "/b/out"), extra="rsync:\n  delete: true\n"))
        job = config.find_job("m", config.load_config())
        assert job.rsync.io_timeout == 60 and job.rsync.delete is True and job.verify_after_sync is False

    def test_a_job_can_override_verify(self, home):
        write_config(home, "verify_after_sync: false\n")
        write_job(home, "m", pairs_job(pair("/a/in", "/b/out"), extra="verify_after_sync: true\n"))
        assert config.find_job("m", config.load_config()).verify_after_sync is True

    def test_a_blank_override_keeps_the_global_value(self, home):
        write_config(home, "rsync:\n  io_timeout: 60\n")
        write_job(home, "m", pairs_job(pair("/a/in", "/b/out"), extra="rsync:\n  io_timeout:\n"))
        assert config.find_job("m", config.load_config()).rsync.io_timeout == 60

    def test_pair_names_default_to_the_source_name(self, home):
        write_config(home)
        write_job(home, "m", pairs_job(pair("/a/photos/", "/b/photos"), pair("/a/docs", "/b/d", "    name: papers\n")))
        job = config.find_job("m", config.load_config())
        assert [job.label(p) for p in job.pairs] == ["m:photos", "m:papers"]

    def test_the_job_name_defaults_to_the_file_name(self, home):
        write_config(home)
        write_job(home, "from-file", pairs_job(pair("/a/in", "/b/out")))
        assert config.find_job("from-file", config.load_config()).name == "from-file"

    @pytest.mark.parametrize("body, message", [
        ("pairs: []\n", "pairs"),
        (pairs_job(pair("relative", "/b/out")), "absolute"),
        (pairs_job(pair("/a", "/a")), "contain each other"),
        (pairs_job(pair("/a", "/a/inside")), "contain each other"),
        (pairs_job(pair("/a/inside", "/a")), "contain each other"),
        (pairs_job(pair("/a/x", "/b/x"), pair("/a/x", "/b/y")), "unique"),
        (pairs_job(pair("/a", "/b"), extra="schedules:\n  - cron: nonsense\n"), "5-field cron"),
        (pairs_job(pair("/a", "/b"), extra="rsync:\n  nope: 1\n"), "nope"),
        (pairs_job(pair("/a", "/b"), extra="surprise: 1\n"), "surprise"),
    ])
    def test_invalid_jobs_are_rejected_with_the_reason(self, home, body, message):
        write_config(home)
        path = write_job(home, "bad", body)
        with pytest.raises(ConfigError, match=message):
            config.load_job(path, config.load_config())

    def test_a_sibling_directory_with_the_same_prefix_is_not_nesting(self, home):
        write_config(home)
        write_job(home, "m", pairs_job(pair("/data/media", "/data/media-backup")))
        assert config.find_job("m", config.load_config()).pairs

    def test_common_excludes_come_first_and_can_be_switched_off(self, home):
        (home / "config" / "common_exclude.yaml").write_text('- "*.tmp"\n- ".cache/"\n')
        write_config(home)
        write_job(home, "m", pairs_job(pair("/a", "/b", "    exclude: [pairexclude]\n"), extra='exclude: ["*.iso"]\n'))
        job = config.find_job("m", config.load_config())
        assert job.exclude == ["*.tmp", ".cache/", "*.iso"]
        assert job.pair_excludes(job.pairs[0]) == ["*.tmp", ".cache/", "*.iso", "pairexclude"]
        write_config(home, "use_common_exclude: false\n")
        assert config.find_job("m", config.load_config()).exclude == ["*.iso"]

    def test_a_pair_backup_dir_wins_over_the_job_setting(self, home):
        write_config(home, "rsync:\n  backup_dir: /backups\n")
        write_job(home, "m", pairs_job(pair("/a", "/b"), pair("/c", "/d", "    backup_dir: /own\n")))
        job = config.find_job("m", config.load_config())
        assert [job.pair_backup_dir(p) for p in job.pairs] == ["/backups", "/own"]

    def test_find_job_by_file_path_and_by_job_name(self, home):
        write_config(home)
        path = write_job(home, "file-name", 'job_name: shown\n' + pairs_job(pair("/a", "/b")))
        cfg = config.load_config()
        assert config.find_job(str(path), cfg).name == "shown"
        assert config.find_job("shown", cfg).name == "shown"
        with pytest.raises(ConfigError, match="not found"):
            config.find_job("missing", cfg)

    def test_find_job_by_id(self, home):
        write_config(home)
        path = write_job(home, "file-name", 'job_name: shown\n' + pairs_job(pair("/a", "/b")))
        assert config.find_job(jid("file-name"), config.load_config()).path == str(path)

    def test_a_job_file_without_an_id_names_the_fix(self, home):
        write_config(home)
        path = write_job(home, "old", pairs_job(pair("/a", "/b")))
        path.write_text(pairs_job(pair("/a", "/b")))
        with pytest.raises(ConfigError, match="job adopt old.yaml"):
            config.load_job(path, config.load_config())

    def test_the_templates_placeholder_id_is_refused(self, home):
        write_config(home)
        path = write_job(home, "copy", f'job_id: "{config.NIL_JOB_ID}"\n' + pairs_job(pair("/a", "/b")))
        with pytest.raises(ConfigError, match="placeholder"):
            config.load_job(path, config.load_config())

    @pytest.mark.parametrize("bad", ["not-a-uuid", "1234", "ABCDEF12-0000-4000-8000-000000000000"])
    def test_a_malformed_id_is_refused(self, home, bad):
        write_config(home)
        path = write_job(home, "bad", f'job_id: "{bad}"\n' + pairs_job(pair("/a", "/b")))
        with pytest.raises(ConfigError, match="job_id"):
            config.load_job(path, config.load_config())

    def test_two_files_with_the_same_id_are_reported(self, home):
        write_config(home)
        write_job(home, "a", f'job_id: "{jid("x")}"\njob_name: a\n' + pairs_job(pair("/a", "/b")))
        write_job(home, "b", f'job_id: "{jid("x")}"\njob_name: b\n' + pairs_job(pair("/c", "/d")))
        _, errors = config.load_jobs(config.load_config())
        assert any(jid("x") in e and "more than one" in e and "job adopt" in e for e in errors)

    def test_renaming_a_job_keeps_its_id_and_a_pairs_target_id_is_its_name(self, home):
        write_config(home)
        path = write_job(home, "a", f'job_id: "{jid("x")}"\njob_name: old\n' + pairs_job(pair("/a", "/b")))
        cfg = config.load_config()
        before = config.load_job(path, cfg)
        path.write_text(path.read_text().replace("job_name: old", "job_name: new"))
        after = config.load_job(path, cfg)
        assert (before.job_id, before.name, after.job_id, after.name) == (jid("x"), "old", jid("x"), "new")
        assert before.pairs[0].resolved_name() == after.pairs[0].resolved_name()
        assert before.label(before.pairs[0]) != after.label(after.pairs[0])

    def test_one_broken_file_does_not_hide_the_others(self, home):
        write_config(home)
        write_job(home, "good", pairs_job(pair("/a", "/b")))
        write_job(home, "bad", "pairs: []\n")
        jobs, errors = config.load_jobs(config.load_config())
        assert [j.name for j in jobs] == ["good"] and len(errors) == 1 and "bad.yaml" in errors[0]

    def test_duplicate_job_names_are_reported(self, home):
        write_config(home)
        write_job(home, "a", "job_name: same\n" + pairs_job(pair("/a", "/b")))
        write_job(home, "b", "job_name: same\n" + pairs_job(pair("/c", "/d")))
        _, errors = config.load_jobs(config.load_config())
        assert any("same" in e and "more than one" in e for e in errors)


class TestTemplates:
    ROOT = Path(__file__).resolve().parent.parent

    def text(self) -> str:
        return (self.ROOT / "config" / "templates" / "job.yaml").read_text(encoding="utf-8")

    def uncommented(self, key: str) -> dict:
        """The commented-out `key:` block of the template, uncommented and parsed."""
        lines, found = [], False
        for line in self.text().splitlines():
            if re.match(rf"^# {key}:", line):
                found = True
                lines.append(line[2:])
            elif found and line.startswith("#   "):
                lines.append(line[2:])
            elif found:
                break
        assert found, f"{key} is not in the template"
        return yaml.safe_load("\n".join(lines))

    def test_the_example_config_is_valid_and_lists_every_setting(self):
        text = (self.ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
        cfg = config.Config.model_validate(yaml.safe_load(text))
        assert cfg.rsync.model_dump() == config.RsyncOptions().model_dump()
        missing = [name for name in config.RsyncOptions.model_fields if not re.search(rf"^\s+{name}:", text, re.M)]
        assert not missing

    def test_the_job_template_is_a_valid_job(self, home):
        write_config(home)
        write_job(home, "example", self.text().replace(config.NIL_JOB_ID, jid("example")))
        job = config.find_job("example", config.load_config())
        assert job.name == "example" and job.job_id == jid("example") and not job.enabled_crons()

    def test_the_template_ships_the_placeholder_id(self, home):
        write_config(home)
        write_job(home, "example", self.text())
        with pytest.raises(ConfigError, match="placeholder"):
            config.find_job("example", config.load_config())

    def test_every_setting_a_job_can_have_is_listed(self):
        text = self.text()
        names = set(config.JobFile.model_fields) | set(config.RsyncOptions.model_fields) | set(config.Pair.model_fields)
        missing = sorted(name for name in names if not re.search(rf"^#?\s*(- )?{name}:", text, re.M))
        assert not missing

    def test_the_commented_overrides_are_valid_when_uncommented(self):
        merged = {}
        for key in ("rsync", "exclude"):
            merged.update(self.uncommented(key))
        config.JobFile.model_validate({"pairs": [{"source": "/a", "destination": "/b"}], **merged})
