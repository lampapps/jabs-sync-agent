"""Verify tab: compare a job's sources with their destinations without syncing, quick or deep."""

import asyncio
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static
from textual.widgets.data_table import RowDoesNotExist

from ... import config, ops
from ..app import Pane
from ..logs import audit

QUICK_NOTE = "Compares file size and modification time only; no file content is read, so it is about as cheap as a sync."
DEEP_NOTE = ("Reads and compares the content of every file on both sides. This is slow and puts heavy load on both "
             "disks and the network, so it never runs automatically.")


class VerifyPane(Pane):
    TAB_ID = "verify"
    TAB_TITLE = "2 Verify"
    ACTIONS = {"verify.run": "do_verify"}
    BINDINGS = [
        Binding("v", "quick", "Quick verify"), Binding("d", "deep", "Deep verify"), Binding("a", "all", "Verify all"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pairs: dict = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="verify-note", markup=False, classes="note")
        yield DataTable(id="verify-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Quick verify", id="quick", variant="primary")
            yield Button("Deep verify", id="deep")
            yield Button("Verify all jobs", id="all")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Pair", "Source", "Destination", "Job last run")

    def selected_job(self) -> Optional[config.Job]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return self._pairs.get(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    async def reload(self) -> None:
        note = self.query_one("#verify-note", Static)
        table = self.query_one(DataTable)
        table.clear()
        self._pairs = {}
        try:
            cfg = await asyncio.to_thread(config.load_config)
            rows, errors = await asyncio.to_thread(ops.jobs_overview, cfg)
        except config.ConfigError as exc:
            note.update(f"config.yaml needs attention (Files tab):\n{exc}")
            return
        keep = None
        for row in rows:
            job = row["job"]
            for pair in job.pairs:
                label = job.label(pair)
                self._pairs[label] = job
                table.add_row(label, pair.source, pair.destination, ops.format_last(row["last"]), key=label)
        if keep is not None:
            try:
                table.move_cursor(row=table.get_row_index(keep))
            except RowDoesNotExist:
                pass
        if errors:
            note.update(f"{len(errors)} job file(s) have problems and are not listed; see the Files tab.")
        elif self._pairs:
            note.update("Verify compares the selected pair's job (every pair of it) without changing anything. "
                        "A sync in progress shows differences.")
        else:
            note.update("No jobs yet. Add one on the Jobs tab.")

    # ---------------------------------------------------------------- actions

    async def do_verify(self, deep: bool = False, everything: bool = False) -> None:
        args = ["verify"]
        title = "Deep verify" if deep else "Quick verify"
        if everything:
            title += ": all jobs"
        else:
            job = self.selected_job()
            if job is None:
                await self.admin.show("No pair selected", "Select a pair in the table first.")
                return
            if await asyncio.to_thread(ops.job_lock_info, job.name) is not None:
                await self.admin.error(f"'{job.name}' is running; verify after it finishes.")
                return
            args.append(job.path)
            title += f": {job.name}"
        if deep:
            args.append("--deep")
            ok, _ = await self.admin.confirm(title, DEEP_NOTE, confirm="Verify", danger=True)
            if not ok:
                return
        audit("verify started", job="all" if everything else args[1], deep=deep)
        code = await self.admin.run_command(title, args, DEEP_NOTE if deep else QUICK_NOTE)
        audit("verify finished", exit=code)

    # ---------------------------------------------------------------- bindings and buttons

    def action_quick(self) -> None:
        self.admin.spawn(self.do_verify, False, False)

    def action_deep(self) -> None:
        self.admin.spawn(self.do_verify, True, False)

    def action_all(self) -> None:
        self.admin.spawn(self.do_verify, False, True)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_quick()
