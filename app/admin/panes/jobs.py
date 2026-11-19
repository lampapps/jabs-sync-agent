"""Jobs tab: what is scheduled, and running or stopping a configured job, live or as a dry run."""

import asyncio
from pathlib import Path
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static
from textual.widgets.data_table import RowDoesNotExist

from ... import config, ops, settings
from .. import editing
from ..app import Pane
from ..logs import audit

LIVE_NOTE = ("Syncs every pair of the job now, exactly like a scheduled run. Files are copied and, with `delete` on, "
             "files missing from a source are removed from its destination. Reports to the dashboard, emails on "
             "failure and pings Uptime Kuma if configured.")
DRY_NOTE = ("rsync --dry-run: works out what would change and changes nothing. The run is still reported to the "
            "dashboard, marked [DRY RUN].")


class JobsPane(Pane):
    TAB_ID = "jobs"
    TAB_TITLE = "1 Jobs"
    ACTIONS = {"jobs.list": "reload", "jobs.run": "do_run", "jobs.stop": "do_stop", "jobs.edit": "do_edit",
               "jobs.new": "do_new", "jobs.delete": "do_delete", "jobs.adopt": "do_adopt"}
    BINDINGS = [
        Binding("d", "dry_run", "Dry run"), Binding("s", "sync", "Sync now"), Binding("t", "stop", "Stop"),
        Binding("e", "edit", "Edit"), Binding("n", "new", "New job"), Binding("x", "delete", "Delete job"),
        Binding("i", "adopt", "Add missing ids"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._jobs: dict = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="jobs-note", markup=False, classes="note")
        yield DataTable(id="jobs-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Dry run", id="dry_run", variant="primary")
            yield Button("Sync now", id="sync", variant="success")
            yield Button("Stop", id="stop", variant="warning")
            yield Button("Edit job file", id="edit")
            yield Button("New job", id="new")
            yield Button("Delete job", id="delete", variant="error")
            yield Button("Add missing ids", id="adopt")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Job", "Pairs", "Schedule", "Next run", "Last run", "Now")
        self.set_interval(10, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self.admin.active_pane() is self:
            self.admin.spawn(self.reload)

    def selected(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    async def reload(self) -> None:
        note = self.query_one("#jobs-note", Static)
        table = self.query_one(DataTable)
        keep = self.selected()
        try:
            cfg = await asyncio.to_thread(config.load_config)
            rows, errors = await asyncio.to_thread(ops.jobs_overview, cfg)
        except config.ConfigError as exc:
            table.clear()
            self._jobs = {}
            note.update(f"config.yaml needs attention (Files tab):\n{exc}")
            return
        self._jobs = {row["job"].job_id: row["job"] for row in rows}
        table.clear()
        for row in rows:
            job = row["job"]
            nxt = row["next"]
            running = row["running"]
            table.add_row(
                job.name, str(len(job.pairs)), ", ".join(job.enabled_crons()) or "none enabled",
                nxt.strftime("%Y-%m-%d %H:%M") if nxt else "-", ops.format_last(row["last"]),
                f"RUNNING (pid {running.get('pid', '?')})" if running is not None else "idle", key=job.job_id)
        if keep is not None:
            try:
                table.move_cursor(row=table.get_row_index(keep))
            except RowDoesNotExist:
                pass
        if errors:
            note.update(f"{len(errors)} job file(s) have problems and are not listed; see the Files tab.\n" + errors[0].splitlines()[0])
        elif rows:
            note.update(f"{len(rows)} job(s). Dry run (d) shows what a job would change without changing anything; Sync now (s) runs it for real.")
        else:
            note.update(f"No jobs yet. Press n to create one from the template in {settings.JOBS_DIR}.")

    # ---------------------------------------------------------------- actions

    async def do_run(self, dry_run: bool = True) -> None:
        name = self.selected()
        job = self._jobs.get(name) if name else None
        if job is None:
            await self.admin.show("No job selected", "Select a job in the table first.")
            return
        lock = await asyncio.to_thread(ops.job_lock_info, job.job_id)
        if lock is not None:
            await self.admin.error(f"'{job.name}' is already running (pid {lock.get('pid', '?')}). Wait for it to finish.")
            return
        message = DRY_NOTE if dry_run else LIVE_NOTE
        ok, _ = await self.admin.confirm(f"{'Dry run' if dry_run else 'Sync now'}: {job.name}", message,
                                         confirm="Run", danger=not dry_run)
        if not ok:
            return
        args = ["run", job.path] + (["--dry-run"] if dry_run else [])
        mode = "dry run" if dry_run else "live sync"
        audit("job run started", job=job.name, mode=mode)
        code = await self.admin.run_command(f"{'Dry run' if dry_run else 'Sync'}: {job.name}", args, message)
        audit("job run finished", job=job.name, mode=mode, exit=code)
        await self.reload()

    async def do_stop(self) -> None:
        name = self.selected()
        job = self._jobs.get(name) if name else None
        if job is None:
            await self.admin.show("No job selected", "Select a job in the table first.")
            return
        if await asyncio.to_thread(ops.job_lock_info, job.job_id) is None:
            await self.admin.show("Not running", f"'{job.name}' is not running.")
            return
        ok, _ = await self.admin.confirm(
            f"Stop {job.name}", "Ends the running sync gracefully. rsync keeps its partial file, the dashboard shows the "
            "run as stopped (not an error) and the job carries on at its next run.", confirm="Stop")
        if not ok:
            return
        stopped = await asyncio.to_thread(ops.stop_job, job)
        audit("job stop requested", job=job.name, stopped=stopped)
        self.notify(f"Asked '{job.name}' to stop" if stopped else f"'{job.name}' had already finished")
        await self.reload()

    async def do_edit(self) -> None:
        name = self.selected()
        entry = editing.entry_for_path(self._jobs[name].path) if name in self._jobs else None
        if entry is None:
            await self.admin.show("No job selected", "Select a job in the table first.")
            return
        await editing.edit_entry(self.admin, entry)
        await self.reload()

    async def do_new(self) -> None:
        fields = [{"key": "name", "label": "Job name", "kind": "text",
                   "help": "Letters, digits, . _ and -. It becomes config/jobs/<name>.yaml, created from the template "
                           "with a new job id and its schedule disabled."}]

        def submit(values: dict):
            return ops.new_job(values["name"])

        path = await self.admin.form("New job", fields, submit, "Create")
        if path is None:
            return
        audit("job file created", file=path.name)
        entry = editing.entry_for_path(path)
        await self.reload()
        if entry is not None:
            await editing.edit_entry(self.admin, entry)
            await self.reload()

    async def do_adopt(self) -> None:
        changed = await asyncio.to_thread(ops.adopt_jobs)
        for path, job_id in changed:
            audit("job id added", file=path.name, job_id=job_id)
        await self.reload()
        await self.admin.show("Add missing ids", "Gave an id to: " + ", ".join(path.name for path, _ in changed)
                              if changed else "Every job file already has an id.")

    async def do_delete(self) -> None:
        name = self.selected()
        job = self._jobs.get(name) if name else None
        if job is None:
            await self.admin.show("No job selected", "Select a job in the table first.")
            return
        if await asyncio.to_thread(ops.job_lock_info, job.job_id) is not None:
            await self.admin.error(f"'{job.name}' is running; wait for it to finish before deleting it.")
            return
        ok, _ = await self.admin.confirm(
            f"Delete job {job.name}",
            f"Deletes {job.path} and forgets the job's schedule state. This cannot be undone from here.\n\n"
            "Files already synced stay where they are, and the dashboard keeps the job's run history. "
            "Syncing for this job stops.",
            confirm="Delete job", danger=True, type_word=job.name)
        if not ok:
            return
        paths_before = await editing.writable_paths_before()
        await asyncio.to_thread(ops.delete_job, job)
        audit("job deleted", job=job.name, file=Path(job.path).name)
        await self.reload()
        self.notify(f"Deleted {Path(job.path).name}")
        await editing.offer_reinstall_if_paths_changed(self.admin, paths_before)

    # ---------------------------------------------------------------- bindings and buttons

    def action_dry_run(self) -> None:
        self.admin.spawn(self.do_run, True)

    def action_sync(self) -> None:
        self.admin.spawn(self.do_run, False)

    def action_stop(self) -> None:
        self.admin.spawn(self.do_stop)

    def action_edit(self) -> None:
        self.admin.spawn(self.do_edit)

    def action_new(self) -> None:
        self.admin.spawn(self.do_new)

    def action_delete(self) -> None:
        self.admin.spawn(self.do_delete)

    def action_adopt(self) -> None:
        self.admin.spawn(self.do_adopt)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_dry_run()
