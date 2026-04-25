"""Live console rendering for Phase 2 P25 decode.

Uses `rich` when stdout is a TTY: a Live-updating table rolls each new
SurveyRecord into place. When piped to a file (or `rich` isn't installed),
falls back to plain stdout lines that look identical to the v1 output.
"""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from typing import Any

from p25_survey.survey import SurveyRecord


def _hex_or_dash(value: int | None, width: int) -> str:
    return format(value, f"0{width}X") if value is not None else "—"


def _row_values(record: SurveyRecord, status: str) -> tuple[str, ...]:
    return (
        f"{record.freq_hz / 1_000_000:.5f}",
        _hex_or_dash(record.wacn, 5),
        _hex_or_dash(record.sysid, 3),
        _hex_or_dash(record.nac, 3),
        str(record.rfss_id) if record.rfss_id is not None else "—",
        str(record.site_id) if record.site_id is not None else "—",
        str(len(record.neighbors)),
        f"{record.dwell_ms} ms",
        status,
    )


_HEADERS = ("Freq (MHz)", "WACN", "SYS", "NAC", "RFSS", "Site", "Nbrs", "Dwell", "Status")
_PLAIN_FMT = "  {:>10}  {:>5}  {:>4}  {:>4}  {:>4}  {:>4}  {:>4}  {:>8}  {:<8}"


class _PlainDisplay(AbstractContextManager):
    """Fallback when rich isn't available or stdout isn't a TTY."""

    def __init__(self, total: int, skipped: int) -> None:
        self.total = total
        self.skipped = skipped

    def __enter__(self) -> "_PlainDisplay":
        print()
        print(f"Phase 2 — P25 decode  ({self.total} to characterize, "
              f"{self.skipped} skipped via resume)")
        print(_PLAIN_FMT.format(*_HEADERS))
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def add(self, record: SurveyRecord, status: str) -> None:
        print(_PLAIN_FMT.format(*_row_values(record, status)), flush=True)


class _RichDisplay(AbstractContextManager):
    """Live-updating rich table."""

    def __init__(self, total: int, skipped: int) -> None:
        from rich.console import Console  # noqa: PLC0415
        from rich.live import Live  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415

        self._Table = Table
        self._console = Console()
        self._total = total
        self._skipped = skipped
        self._rows: list[tuple[tuple[str, ...], str]] = []  # (row, status)
        self._live = Live(
            self._render(), console=self._console,
            refresh_per_second=4, vertical_overflow="visible",
        )

    def __enter__(self) -> "_RichDisplay":
        self._live.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._live.__exit__(*exc)

    def add(self, record: SurveyRecord, status: str) -> None:
        self._rows.append((_row_values(record, status), status))
        self._live.update(self._render())

    def _status_style(self, status: str) -> str:
        return {
            "complete": "green",
            "partial": "yellow",
            "no-cc": "dim",
        }.get(status, "")

    def _render(self):
        title = (f"Phase 2 — P25 decode  "
                 f"[{len(self._rows)}/{self._total} done, "
                 f"{self._skipped} skipped via resume]")
        table = self._Table(title=title, show_header=True, header_style="bold")
        for h, justify in zip(_HEADERS, (
            "right", "right", "right", "right", "right", "right",
            "right", "right", "left",
        )):
            table.add_column(h, justify=justify)
        for row, status in self._rows:
            style = self._status_style(status)
            table.add_row(*row, style=style)
        return table


def make_display(total: int, skipped: int) -> AbstractContextManager:
    """Return the right display for the current stdout (rich if TTY + installed)."""
    if not sys.stdout.isatty():
        return _PlainDisplay(total, skipped)
    try:
        import rich  # noqa: F401, PLC0415
    except ImportError:
        return _PlainDisplay(total, skipped)
    return _RichDisplay(total, skipped)
