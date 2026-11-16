"""The console's tabs, in display order."""

from .diagnostics import DiagnosticsPane
from .docs import DocsPane
from .files import FilesPane
from .jobs import JobsPane
from .service import ServicePane
from .verify import VerifyPane

PANES = [JobsPane, VerifyPane, FilesPane, DiagnosticsPane, ServicePane, DocsPane]
