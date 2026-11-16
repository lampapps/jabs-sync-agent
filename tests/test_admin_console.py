"""Console tests drive the real Textual app headlessly. Dialogs are filled in and buttons pressed like a user would."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, OptionList, RichLog, Static, TabbedContent

from app import config, locking, ops, runner, settings, state
from app.admin import runscreen, service
from app.admin.app import AdminApp
from app.admin.editor import EditorError
from app.admin.panes.diagnostics import DiagnosticsPane
from app.admin.panes.docs import DocsPane
from app.admin.panes.files import FilesPane
from app.admin.panes.jobs import JobsPane
from app.admin.panes.service import ServicePane
from app.admin.panes.verify import VerifyPane

from .conftest import write_config, write_job

JOB = ('job_name: "{name}"\npairs:\n  - source: /data/{name}/in\n    destination: /data/{name}/out\n'
       'schedules:\n  - cron: "0 3 * * *"\n    enabled: true\n')
TEMPLATE = 'job_name: "example"\npairs:\n  - source: /p\n    destination: /q\n'


def drive(scenario, size=(130, 45)):
    """Run `scenario(app, pilot)` against a fresh console and return its result."""
    async def main():
        app = AdminApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await scenario(app, pilot)

    return asyncio.run(main())


async def wait_for(pilot, predicate, what="condition", timeout=8.0):
    waited = 0.0
    while waited < timeout:
        await pilot.pause(0.05)
        try:
            if predicate():
                return
        except NoMatches:  # a dialog is still closing, so the tab's widgets are not on the active screen yet
            pass
        waited += 0.05
    raise AssertionError(f"timed out waiting for {what}")


def dialog_title(app):
    labels = list(app.screen.query(".dialog-title"))
    return str(labels[0].render()) if labels else None


async def wait_dialog(pilot, app, title):
    await wait_for(pilot, lambda: dialog_title(app) == title, f"dialog '{title}'")


async def press(pilot, app, button_id):
    app.screen.query_one(f"#{button_id}", Button).press()
    await pilot.pause(0.1)


def pane_button(app, pane, button_id):
    return app.query_one(pane).query_one(f"#{button_id}", Button)


def table_rows(app, table_id):
    table = app.screen.query_one(f"#{table_id}", DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


def fill(app, **values):
    for key, value in values.items():
        app.screen.query_one(f"#f-{key}").value = value


def dialog_text(app):
    return " ".join(str(s.render()) for s in app.screen.query(Static))


async def open_tab(pilot, key):
    await pilot.press(key)
    await pilot.pause(0.3)


def run_output(app):
    return "\n".join(line.text for line in app.screen.query_one("#run-output", RichLog).lines)


def log_text(app, selector):
    return "\n".join(line.text for line in app.query_one(selector, RichLog).lines)


def hold_lock(name: str) -> subprocess.Popen:
    """A child process that holds a job's lock, like a running sync does."""
    code = ("import sys, time; sys.path.insert(0, %r); from app import locking; "
            "held = locking.acquire_lock(%r); print('locked', flush=True); time.sleep(60)") % (
                str(Path(runner.__file__).parent.parent), runner.lock_path(name))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "locked"
    return child


@pytest.fixture
def two_jobs(home):
    write_config(home)
    for name in ("alpha", "beta"):
        write_job(home, name, JOB.format(name=name))
    return home


@pytest.fixture
def child(monkeypatch):
    """Replace the child process with one that prints the arguments it was given."""
    seen = []

    def fake(args):
        seen.append(list(args))
        return [sys.executable, "-c", "import sys; print('ARGS', sys.argv[1:])", *args]

    monkeypatch.setattr(runscreen, "child_command", fake)
    return seen


class TestStartup:
    def test_opens_on_jobs_and_switches_tabs_with_number_keys(self, home):
        async def scenario(app, pilot):
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "jobs"
            for key, tab in (("2", "verify"), ("3", "files"), ("4", "diagnostics"), ("5", "service"),
                             ("6", "docs"), ("1", "jobs")):
                await pilot.press(key)
                await pilot.pause(0.1)
                assert tabs.active == tab

        drive(scenario)

    def test_starts_even_when_config_is_broken_and_says_so(self, home):
        write_config(home, "rsync:\n  io_timeoutt: 7\n")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "needs attention" in str(app.query_one("#jobs-note", Static).render()))

        drive(scenario)


class TestJobs:
    def test_lists_jobs_with_pairs_schedule_next_run_and_last_result(self, two_jobs):
        state.record_run("alpha", {"success": True, "finished_at": 1_800_000_000})
        state.record_run("beta", {"success": True, "stopped": True, "finished_at": 1_800_000_000})

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            rows = {r[0]: r for r in table_rows(app, "jobs-table")}
            assert rows["alpha"][1] == "1" and rows["alpha"][2] == "0 3 * * *" and rows["alpha"][4].startswith("ok 2027-")
            assert rows["alpha"][3] != "-" and rows["alpha"][5] == "idle" and rows["beta"][4].startswith("stopped ")

        drive(scenario)

    def test_a_running_job_is_marked(self, two_jobs):
        lock = locking.acquire_lock(runner.lock_path("beta"))

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            rows = {r[0]: r[5] for r in table_rows(app, "jobs-table")}
            assert rows["beta"].startswith("RUNNING (pid ") and rows["alpha"] == "idle"

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)

    def test_a_broken_job_file_is_reported_not_fatal(self, two_jobs):
        write_job(two_jobs, "broken", "pairs: []\n")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "problem" in str(app.query_one("#jobs-note", Static).render()))
            assert len(table_rows(app, "jobs-table")) == 2

        drive(scenario)

    def test_dry_run_asks_then_runs_the_job_with_the_flag(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "Dry run: alpha")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))
            await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled)
            assert "Finished: exit status 0 (success)" in str(app.screen.query_one("#run-status", Static).render())
            await press(pilot, app, "close")

        drive(scenario)
        assert child == [["run", str(settings.JOBS_DIR / "alpha.yaml"), "--dry-run"]]

    def test_a_live_sync_warns_about_deleting_and_runs_without_the_flag(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "sync").press()
            await wait_dialog(pilot, app, "Sync now: alpha")
            assert "removed from its destination" in dialog_text(app)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))
            await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled)

        drive(scenario)
        assert child == [["run", str(settings.JOBS_DIR / "alpha.yaml")]]

    def test_cancelling_runs_nothing(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "sync").press()
            await wait_dialog(pilot, app, "Sync now: alpha")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert dialog_title(app) is None

        drive(scenario)
        assert child == []

    def test_the_selected_job_is_the_one_that_runs(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            app.query_one("#jobs-table", DataTable).focus()
            await pilot.press("down")
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "Dry run: beta")

        drive(scenario)

    def test_a_job_that_is_already_running_is_refused(self, two_jobs, child):
        lock = locking.acquire_lock(runner.lock_path("alpha"))

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "sync").press()
            await wait_dialog(pilot, app, "Error")
            assert "already running" in dialog_text(app)

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)
        assert child == []

    def test_no_job_selected_explains(self, home):
        write_config(home)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "No jobs yet" in str(app.query_one("#jobs-note", Static).render()))
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "No job selected")

        drive(scenario)

    def test_a_failing_run_is_shown_as_failed(self, two_jobs, monkeypatch):
        monkeypatch.setattr(runscreen, "child_command", lambda args: [
            sys.executable, "-c", "import sys; print('boom'); sys.exit(1)"])

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "sync").press()
            await wait_dialog(pilot, app, "Sync now: alpha")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "FAILED" in str(app.screen.query_one("#run-status", Static).render()))
            assert "boom" in run_output(app)

        drive(scenario)

    def test_stop_in_the_run_window_interrupts_the_run(self, two_jobs, monkeypatch):
        monkeypatch.setattr(runscreen, "child_command", lambda args: [
            sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"])

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "sync").press()
            await wait_dialog(pilot, app, "Sync now: alpha")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "started" in run_output(app))
            assert app.screen.query_one("#close", Button).disabled is True
            await press(pilot, app, "stop")
            await wait_for(pilot, lambda: "Stopped" in str(app.screen.query_one("#run-status", Static).render()))
            assert app.screen.query_one("#close", Button).disabled is False

        drive(scenario)

    def test_closing_the_console_during_a_run_leaves_nothing_behind(self, two_jobs, monkeypatch, tmp_path):
        marker = tmp_path / "pid"
        monkeypatch.setattr(runscreen, "child_command", lambda args: [
            sys.executable, "-c", f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"])

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "sync").press()
            await wait_dialog(pilot, app, "Sync now: alpha")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: marker.exists() and marker.read_text())

        drive(scenario)
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail("the child process was left running")

    def test_the_stop_button_ends_a_job_running_elsewhere(self, two_jobs):
        holder = hold_lock("alpha")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "stop").press()
            await wait_dialog(pilot, app, "Stop alpha")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: holder.poll() is not None, "the job to be signalled")

        try:
            drive(scenario)
        finally:
            holder.kill()
            holder.wait()
        assert holder.returncode != 0

    def test_stopping_a_job_that_is_not_running_says_so(self, two_jobs):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "stop").press()
            await wait_dialog(pilot, app, "Not running")

        drive(scenario)

    def test_cancelling_the_stop_leaves_the_job_alone(self, two_jobs):
        holder = hold_lock("alpha")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "stop").press()
            await wait_dialog(pilot, app, "Stop alpha")
            await press(pilot, app, "cancel")
            await pilot.pause(0.3)

        try:
            drive(scenario)
            assert holder.poll() is None
        finally:
            holder.kill()
            holder.wait()

    def test_new_job_creates_the_file_from_the_template_and_opens_the_editor(self, two_jobs, monkeypatch):
        template = settings.CONFIG_DIR / "templates"
        template.mkdir()
        (template / "job.yaml").write_text(TEMPLATE)
        opened = []

        async def run_editor(self, path):
            opened.append(path)
            return 0

        monkeypatch.setattr(AdminApp, "run_editor", run_editor)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "new").press()
            await wait_dialog(pilot, app, "New job")
            fill(app, name="gamma")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: opened)
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 3)

        drive(scenario)
        assert 'job_name: "gamma"' in (settings.JOBS_DIR / "gamma.yaml").read_text()
        assert opened == [settings.JOBS_DIR / "gamma.yaml"]

    @pytest.mark.parametrize("name,message", [("bad name", "letters"), ("alpha", "already exists")])
    def test_new_job_rejects_bad_or_duplicate_names(self, two_jobs, name, message):
        (settings.CONFIG_DIR / "templates").mkdir()
        (settings.CONFIG_DIR / "templates" / "job.yaml").write_text(TEMPLATE)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "new").press()
            await wait_dialog(pilot, app, "New job")
            fill(app, name=name)
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: message in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)

    def test_edit_job_validates_and_can_revert(self, two_jobs, monkeypatch):
        before = (settings.JOBS_DIR / "alpha.yaml").read_text()

        async def run_editor(self, path):
            path.write_text("pairs: []\n")
            return 0

        monkeypatch.setattr(AdminApp, "run_editor", run_editor)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "edit").press()
            await wait_dialog(pilot, app, "jobs/alpha.yaml is not valid")
            await press(pilot, app, "choice-1")
            await wait_dialog(pilot, app, "Reverted")

        drive(scenario)
        assert (settings.JOBS_DIR / "alpha.yaml").read_text() == before


class TestVerify:
    def test_lists_every_pair_with_its_paths(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: len(table_rows(app, "verify-table")) == 2)
            rows = {r[0]: r for r in table_rows(app, "verify-table")}
            assert rows["alpha:in"][1:3] == ["/data/alpha/in", "/data/alpha/out"]

        drive(scenario)

    def test_quick_verify_runs_the_selected_pairs_job(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: len(table_rows(app, "verify-table")) == 2)
            app.query_one("#verify-table", DataTable).focus()
            await pilot.press("down")
            pane_button(app, VerifyPane, "quick").press()
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["verify", str(settings.JOBS_DIR / "beta.yaml")]]

    def test_deep_verify_asks_first(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "verify-table"))
            pane_button(app, VerifyPane, "deep").press()
            await wait_dialog(pilot, app, "Deep verify: alpha")
            assert "slow" in dialog_text(app)
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert child == []
            pane_button(app, VerifyPane, "deep").press()
            await wait_dialog(pilot, app, "Deep verify: alpha")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["verify", str(settings.JOBS_DIR / "alpha.yaml"), "--deep"]]

    def test_verify_all_jobs_names_no_job(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "verify-table"))
            pane_button(app, VerifyPane, "all").press()
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["verify"]]

    def test_a_running_job_is_not_verified(self, two_jobs, child):
        lock = locking.acquire_lock(runner.lock_path("alpha"))

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "verify-table"))
            pane_button(app, VerifyPane, "quick").press()
            await wait_dialog(pilot, app, "Error")
            assert "is running" in dialog_text(app)

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)
        assert child == []

    def test_nothing_selected_explains(self, home):
        write_config(home)

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: "No jobs yet" in str(app.query_one("#verify-note", Static).render()))
            pane_button(app, VerifyPane, "quick").press()
            await wait_dialog(pilot, app, "No pair selected")

        drive(scenario)


class TestFiles:
    @pytest.fixture
    def editor(self, monkeypatch):
        class Fake:
            script = []
            launches = []
            error = None
            reinstalls = []

        async def run_editor(self, path):
            if Fake.error:
                raise Fake.error
            Fake.launches.append(path)
            if Fake.script:
                path.write_text(Fake.script.pop(0), encoding="utf-8")
            return 0

        async def reinstall_units(self):
            Fake.reinstalls.append(1)

        Fake.script, Fake.launches, Fake.error, Fake.reinstalls = [], [], None, []
        monkeypatch.setattr(AdminApp, "run_editor", run_editor)
        monkeypatch.setattr(AdminApp, "reinstall_units", reinstall_units)
        return Fake

    def test_lists_config_env_excludes_and_jobs_with_their_state(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: len(table_rows(app, "files-table")) == 5)
            rows = {r[0]: r[1] for r in table_rows(app, "files-table")}
            assert rows == {"config.yaml": "OK", ".env": "missing", "common_exclude.yaml": "missing",
                            "jobs/alpha.yaml": "OK", "jobs/beta.yaml": "OK"}

        drive(scenario)

    def test_an_env_readable_by_others_is_flagged(self, two_jobs):
        settings.ENV_PATH.write_text("JABS_SMTP_USER=x\n")
        settings.ENV_PATH.chmod(0o644)

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: len(table_rows(app, "files-table")) == 5)
            assert {r[0]: r[1] for r in table_rows(app, "files-table")}[".env"] == "WARNING"

        drive(scenario)

    def test_an_invalid_edit_can_be_reverted_kept_or_fixed(self, two_jobs, editor):
        before = settings.CONFIG_PATH.read_text()
        editor.script = ["rsync:\n  nope: 1\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            assert "rsync.nope" in dialog_text(app)
            await press(pilot, app, "choice-1")
            await wait_dialog(pilot, app, "Reverted")

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == before

    def test_edit_again_then_valid_is_saved(self, two_jobs, editor):
        editor.script = ["rsync:\n  nope: 1\n", "verify_after_sync: false\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            await press(pilot, app, "choice-0")
            await wait_for(pilot, lambda: len(editor.launches) == 2)
            await pilot.pause(0.3)

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == "verify_after_sync: false\n"

    def test_a_valid_edit_needs_no_restart_and_no_prompt(self, two_jobs, editor):
        editor.script = ["verify_after_sync: false\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_for(pilot, lambda: editor.launches)
            await pilot.pause(0.5)
            assert dialog_title(app) is None

        drive(scenario)
        assert not editor.reinstalls

    def test_changing_a_writable_directory_offers_to_reinstall_the_units(self, two_jobs, editor, tmp_path):
        local = tmp_path / "kept"
        local.mkdir()
        editor.script = [f"rsync:\n  backup_dir: {local}\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "The service's writable directories changed")
            assert f"+ {local}" in dialog_text(app)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: editor.reinstalls)

        drive(scenario)

    def test_a_new_env_is_created_private(self, two_jobs, editor):
        editor.script = ["JABS_SMTP_USER=x\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one("#files-table", DataTable).focus()
            await pilot.press("down")
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_for(pilot, lambda: editor.launches)
            await pilot.pause(0.4)

        drive(scenario)
        assert (settings.ENV_PATH.stat().st_mode & 0o777) == 0o600

    def test_no_editor_available_is_reported(self, two_jobs, editor):
        editor.error = EditorError("no editor found; set the EDITOR environment variable")

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "Error")
            assert "EDITOR" in dialog_text(app)

        drive(scenario)

    def test_validate_all_reports_every_file(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#validate", Button).press()
            await wait_dialog(pilot, app, "Validation")
            text = dialog_text(app)
            assert "config.yaml: OK" in text and "jobs/alpha.yaml: OK" in text and ".env: missing" in text

        drive(scenario)


class TestDiagnostics:
    def test_tick_asks_first_then_runs_it(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "tick").press()
            await wait_dialog(pilot, app, "Run the tick now")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert child == []
            pane_button(app, DiagnosticsPane, "tick").press()
            await wait_dialog(pilot, app, "Run the tick now")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["tick"]]

    def test_testmail_runs_the_cli_command(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "testmail").press()
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["testmail"]]

    def test_dashboard_link_in_standalone_mode_explains_how_to_enable_it(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "dashboard").press()
            await wait_dialog(pilot, app, "Dashboard link")
            assert "standalone" in dialog_text(app) and "JABS_DASHBOARD_URL" in dialog_text(app)

        drive(scenario)

    @pytest.mark.parametrize("accepted,expect", [(True, "was accepted"), (False, "NOT accepted")])
    def test_dashboard_link_sends_a_heartbeat(self, two_jobs, monkeypatch, accepted, expect):
        from app import dashboard

        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash:5002")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        calls = []
        monkeypatch.setattr(dashboard.Client, "send_scheduler_check", lambda self, n=0: calls.append(n) or accepted)

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "dashboard").press()
            await wait_dialog(pilot, app, "Dashboard link")
            assert expect in dialog_text(app) and "http://dash:5002" in dialog_text(app)

        drive(scenario)
        assert calls == [0]

    def test_state_view_shows_results_and_running_jobs(self, two_jobs):
        state.record_run("alpha", {"success": True, "finished_at": 1_800_000_000})
        lock = locking.acquire_lock(runner.lock_path("beta"))

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "state").press()
            await wait_dialog(pilot, app, "Schedule state")
            text = dialog_text(app)
            assert '"alpha"' in text and '"running"' in text and '"beta"' in text

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)

    def test_paths_lists_the_writable_directories(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "paths").press()
            await wait_dialog(pilot, app, "Directories the service may write")
            assert str(settings.DATA_DIR) in dialog_text(app)

        drive(scenario)

    def test_reset_needs_the_word_and_removes_state_and_locks(self, two_jobs):
        state.record_run("alpha", {"success": True, "finished_at": 1})
        assert settings.STATE_PATH.exists()

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "reset").press()
            await wait_dialog(pilot, app, "Reset schedule state")
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word").value = "RESET"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "Reset")

        drive(scenario)
        assert not settings.STATE_PATH.exists()

    def test_cancelling_the_reset_changes_nothing(self, two_jobs):
        state.record_run("alpha", {"success": True, "finished_at": 1})

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "reset").press()
            await wait_dialog(pilot, app, "Reset schedule state")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)

        drive(scenario)
        assert settings.STATE_PATH.exists()


class TestService:
    @pytest.fixture
    def units(self, monkeypatch):
        calls = []
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "detect_base", lambda override=None: "jabs-sync-agent")
        monkeypatch.setattr(service, "unit_status", lambda unit: {
            "active": "inactive (dead)", "enabled": "enabled", "since": "today", "next": "in 3 min", "last": "2 min ago"})
        monkeypatch.setattr(service, "installed_options", lambda base: {"user": "jim", "read_all": True})

        async def run_systemctl(self, action, unit):
            calls.append((action, unit))
            return 0

        async def run_install(self, base, user, read_all):
            calls.append(("install", base, user, read_all))
            return 0

        monkeypatch.setattr(AdminApp, "run_systemctl", run_systemctl)
        monkeypatch.setattr(AdminApp, "run_install", run_install)
        return calls

    def test_lists_the_service_and_the_timer(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: len(table_rows(app, "units")) == 2)
            assert [r[0] for r in table_rows(app, "units")] == ["jabs-sync-agent.service", "jabs-sync-agent.timer"]
            assert table_rows(app, "units")[1][5] == "in 3 min"

        drive(scenario)

    def test_start_runs_a_tick_now_without_asking(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            pane_button(app, ServicePane, "start").press()
            await wait_for(pilot, lambda: units == [("start", "jabs-sync-agent.service")])

        drive(scenario)

    def test_disabling_the_timer_asks_first(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            app.query_one("#units", DataTable).focus()
            await pilot.press("down")
            pane_button(app, ServicePane, "disable").press()
            await wait_dialog(pilot, app, "Disable jabs-sync-agent.timer")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert units == []
            pane_button(app, ServicePane, "disable").press()
            await wait_dialog(pilot, app, "Disable jabs-sync-agent.timer")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: units == [("disable", "jabs-sync-agent.timer")])

        drive(scenario)

    def test_reinstall_keeps_the_installed_user_and_options(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            pane_button(app, ServicePane, "install").press()
            await wait_dialog(pilot, app, "Reinstall jabs-sync-agent")
            assert app.screen.query_one("#f-user").value == "jim" and app.screen.query_one("#f-read_all").value is True
            await press(pilot, app, "save")
            await wait_dialog(pilot, app, "Reinstall the units")

        drive(scenario)
        assert units == [("install", "jabs-sync-agent", "jim", True)]

    def test_reinstall_rejects_an_unusual_user_name(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            pane_button(app, ServicePane, "install").press()
            await wait_dialog(pilot, app, "Reinstall jabs-sync-agent")
            fill(app, user="Bad User; rm")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "unusual user" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)
        assert units == []

    def test_without_systemd_it_says_so(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: False)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: "systemd is not available" in str(app.query_one("#service-note", Static).render()))

        drive(scenario)

    def test_unknown_install_points_at_the_unit_option_and_install_script(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "detect_base", lambda override=None: None)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: "--unit" in str(app.query_one("#service-note", Static).render()))

        drive(scenario)


class TestDocs:
    @pytest.fixture
    def tree(self, home):
        (home / "README.md").write_text("# Install guide\n\nSee [the guide](docs/GUIDE.md).\n")
        (home / "docs").mkdir()
        (home / "docs" / "GUIDE.md").write_text("# Guide\n")
        (home / ".venv").mkdir()
        (home / ".venv" / "skip.md").write_text("no")
        return home

    def test_lists_markdown_files_and_shows_the_readme(self, tree):
        async def scenario(app, pilot):
            await open_tab(pilot, "6")
            pane = app.query_one(DocsPane)
            await wait_for(pilot, lambda: pane._current is not None)
            options = app.query_one("#docs-files", OptionList)
            assert [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)] == [
                "README.md", "docs/GUIDE.md"]
            await wait_for(pilot, lambda: "Install guide" in app.query_one("#docs-viewer").document.source)

        drive(scenario)

    def test_links_open_documents_and_outside_paths_do_not(self, tree):
        async def scenario(app, pilot):
            await open_tab(pilot, "6")
            pane = app.query_one(DocsPane)
            await wait_for(pilot, lambda: pane._current is not None)
            await pane._follow("https://example.com")
            await pane._follow("../x.md")
            assert pane._current.name == "README.md"
            await pane._follow("docs/GUIDE.md")
            assert pane._current.name == "GUIDE.md"
            pane.action_back()
            await wait_for(pilot, lambda: pane._current.name == "README.md")

        drive(scenario)

class TestRealSync:
    """A dry run and a live run through the console, with the real rsync and a real child process."""

    def test_dry_run_changes_nothing_and_a_live_run_copies_the_files(self, home, tmp_path, monkeypatch):
        import shutil

        if shutil.which("rsync") is None:
            pytest.skip("rsync is not installed")
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        (src / "a.txt").write_text("hello\n")
        write_config(home, "use_common_exclude: false\n")
        write_job(home, "docs", f"job_name: docs\npairs:\n  - source: {src}\n    destination: {dst}\n")
        monkeypatch.setenv("JABS_HOME", str(home))

        async def run_job(app, pilot, dry):
            pane_button(app, JobsPane, "dry_run" if dry else "sync").press()
            await wait_dialog(pilot, app, "Dry run: docs" if dry else "Sync now: docs")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled, timeout=60)
            status = str(app.screen.query_one("#run-status", Static).render())
            output = run_output(app)
            await press(pilot, app, "close")
            await pilot.pause(0.3)
            return status, output

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            status, output = await run_job(app, pilot, dry=True)
            assert "exit status 0 (success)" in status and "Starting sync" in output and "docs:src" in output
            assert not dst.exists()
            assert table_rows(app, "jobs-table")[0][4].startswith("ok ")
            status, output = await run_job(app, pilot, dry=False)
            assert "exit status 0 (success)" in status and "Sync complete" in output
            assert (dst / "a.txt").read_text() == "hello\n"

        drive(scenario)


class TestDeleteJob:
    def test_needs_the_job_name_typed_then_removes_the_file_and_its_state(self, two_jobs):
        state.record_run("alpha", {"success": True, "finished_at": 1})
        state.record_run("beta", {"success": True, "finished_at": 1})

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job alpha")
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word").value = "beta"
            await pilot.pause(0.1)
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word").value = "alpha"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: [r[0] for r in table_rows(app, "jobs-table")] == ["beta"])

        drive(scenario)
        assert not (settings.JOBS_DIR / "alpha.yaml").exists() and (settings.JOBS_DIR / "beta.yaml").exists()
        assert list(state.read()["jobs"]) == ["beta"]

    def test_the_selected_job_is_the_one_deleted(self, two_jobs):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            app.query_one("#jobs-table", DataTable).focus()
            await pilot.press("down")
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job beta")

        drive(scenario)
        assert (settings.JOBS_DIR / "beta.yaml").exists()

    def test_cancelling_keeps_the_job(self, two_jobs):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job alpha")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert dialog_title(app) is None

        drive(scenario)
        assert (settings.JOBS_DIR / "alpha.yaml").exists()

    def test_a_running_job_cannot_be_deleted(self, two_jobs):
        lock = locking.acquire_lock(runner.lock_path("alpha"))

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Error")
            assert "is running" in dialog_text(app)

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)
        assert (settings.JOBS_DIR / "alpha.yaml").exists()

    def test_no_job_selected_explains(self, home):
        write_config(home)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "No jobs yet" in str(app.query_one("#jobs-note", Static).render()))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "No job selected")

        drive(scenario)

    def test_removing_a_job_does_not_ask_to_reinstall_the_units(self, home, monkeypatch, tmp_path):
        local = tmp_path / "own-kept"
        local.mkdir()
        write_config(home)
        write_job(home, "alpha", JOB.format(name="alpha") + f"rsync:\n  backup_dir: {local}\n")
        reinstalls = []

        async def reinstall_units(self):
            reinstalls.append(1)

        monkeypatch.setattr(AdminApp, "reinstall_units", reinstall_units)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job alpha")
            app.screen.query_one("#word").value = "alpha"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: not (settings.JOBS_DIR / "alpha.yaml").exists())
            await pilot.pause(0.5)
            assert dialog_title(app) is None

        drive(scenario)
        assert reinstalls == []

    def test_a_job_whose_name_differs_from_its_file_deletes_the_right_file(self, home):
        write_config(home)
        write_job(home, "file-name", 'job_name: "display-name"\npairs:\n  - source: /d/in\n    destination: /d/out\n')

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job display-name")
            app.screen.query_one("#word").value = "display-name"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: not (settings.JOBS_DIR / "file-name.yaml").exists())

        drive(scenario)


class TestImportFromLocalSync:
    @pytest.fixture
    def old(self, tmp_path):
        (tmp_path / "old" / "config" / "jobs").mkdir(parents=True)
        return tmp_path / "old"

    def test_preview_runs_the_import_command_with_dry_run(self, two_jobs, child, old):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            pane_button(app, FilesPane, "import").press()
            await wait_dialog(pilot, app, "Import from local_sync_agent")
            fill(app, dir=str(old))
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: app.screen.query("#choice-0"))
            await press(pilot, app, "choice-0")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["import-local-sync", str(old), "--dry-run"]]

    def test_the_other_choices_pass_their_flags(self, two_jobs, child, old):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            for choice in ("choice-1", "choice-2"):
                pane_button(app, FilesPane, "import").press()
                await wait_dialog(pilot, app, "Import from local_sync_agent")
                fill(app, dir=str(old))
                await press(pilot, app, "save")
                await wait_for(pilot, lambda: app.screen.query("#choice-0"))
                await press(pilot, app, choice)
                await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled)
                await press(pilot, app, "close")
                await pilot.pause(0.2)

        drive(scenario)
        assert child == [["import-local-sync", str(old)], ["import-local-sync", str(old), "--keep-schedules"]]

    def test_a_folder_without_config_is_refused_in_the_form(self, two_jobs, child, tmp_path):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            pane_button(app, FilesPane, "import").press()
            await wait_dialog(pilot, app, "Import from local_sync_agent")
            fill(app, dir=str(tmp_path / "nothing"))
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "was not found" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)
        assert child == []

    def test_cancelling_the_choice_runs_nothing(self, two_jobs, child, old):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            pane_button(app, FilesPane, "import").press()
            await wait_dialog(pilot, app, "Import from local_sync_agent")
            fill(app, dir=str(old))
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: app.screen.query("#choice-3"))
            await press(pilot, app, "choice-3")
            await pilot.pause(0.3)
            assert dialog_title(app) is None

        drive(scenario)
        assert child == []
