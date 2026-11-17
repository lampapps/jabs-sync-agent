from app.config import load_config, load_job

from .conftest import write_config, write_job

BODY = 'pairs:\n  - source: /a\n    destination: /b\nexclude:\n  - "mine/"\n{extra}'


def exclude_of(home, global_flag: str, extra: str = "") -> list:
    write_config(home, f"use_common_exclude: {global_flag}\n")
    (home / "config" / "common_exclude.yaml").write_text('- "common/"\n', encoding="utf-8")
    return load_job(write_job(home, "j", BODY.format(extra=extra)), load_config()).exclude


def test_the_global_setting_applies_by_default(home):
    assert exclude_of(home, "true") == ["common/", "mine/"]
    assert exclude_of(home, "false") == ["mine/"]


def test_a_job_can_turn_the_common_list_off_or_on(home):
    assert exclude_of(home, "true", "use_common_exclude: false\n") == ["mine/"]
    assert exclude_of(home, "false", "use_common_exclude: true\n") == ["common/", "mine/"]
