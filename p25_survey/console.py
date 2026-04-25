"""Live console rendering for Phase 2 P25 decode.

Uses `rich` when stdout is a TTY: a Live-updating table rolls each new
SurveyRecord into place. When piped to a file (or `rich` isn't installed),
falls back to plain stdout lines that look identical to the v1 output.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import AbstractContextManager
from typing import Any

from p25_survey.survey import SurveyRecord


def _hex_or_dash(value: int | None, width: int) -> str:
    return format(value, f"0{width}X") if value is not None else "—"


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _rr_cell(record: SurveyRecord) -> str:
    """Cell summarizing the RR enrichment result. Format:
        "✓ <SYSTEM NAME> · <SITE> · <ppm offset>"  for full match
        "<SYSTEM NAME> · new RFSS/Site"            for new site
        "NEW SYS (not in RR)"                       for unknown system
        ""                                          for no enrichment
    Names truncated to keep the cell ≤ ~50 chars.
    """
    rr = record.rr
    if not rr:
        return ""
    if not rr.get("system_match"):
        return "NEW SYS (not in RR)" if record.wacn is not None else ""
    sys_name = _truncate(rr.get("rr_system_name") or "(unnamed)", 30)
    if not rr.get("site_match"):
        return f"{sys_name} · new RFSS/Site"
    site_label = (rr.get("rr_site_description")
                  or (f"Site {record.site_id}" if record.site_id is not None else "Site"))
    site_label = _truncate(site_label, 16)
    cc = rr.get("cc_freq_offset")
    if cc and not rr.get("cc_freq_in_db"):
        sign = "+" if cc.get("offset_hz", 0) >= 0 else ""
        match = f"off {sign}{cc['offset_hz']} Hz"
    elif cc:
        match = f"{cc.get('ppm', 0.0):+.2f} ppm"
    else:
        match = "✓"
    return f"✓ {sys_name} · {site_label} · {match}"


def _row_values(record: SurveyRecord, status: str) -> tuple[str, ...]:
    sig = record.signal
    rssi = f"{sig.rssi_dbfs_mean:+.1f}" if sig.rssi_dbfs_mean is not None else "—"
    ber = f"{sig.ber_pct_mean:.1f}%" if sig.ber_pct_mean is not None else "—"
    return (
        f"{record.freq_hz / 1_000_000:.5f}",
        _hex_or_dash(record.wacn, 5),
        _hex_or_dash(record.sysid, 3),
        _hex_or_dash(record.nac, 3),
        str(record.rfss_id) if record.rfss_id is not None else "—",
        str(record.site_id) if record.site_id is not None else "—",
        str(len(record.neighbors)),
        rssi,
        ber,
        f"{record.dwell_ms} ms",
        status,
        _rr_cell(record),
    )


_HEADERS = ("Freq (MHz)", "WACN", "SYS", "NAC", "RFSS", "Site", "Nbrs",
            "RSSI", "BER", "Dwell", "Status", "RR Match")
_PLAIN_FMT = ("  {:>10}  {:>5}  {:>4}  {:>4}  {:>4}  {:>4}  {:>4}  "
              "{:>6}  {:>6}  {:>8}  {:<8}  {:<55}")


class _PlainDisplay(AbstractContextManager):
    """Fallback when rich isn't available or stdout isn't a TTY."""

    def __init__(self, total: int, skipped: int, hide_no_cc: bool = False) -> None:
        self.total = total
        self.skipped = skipped
        self.hide_no_cc = hide_no_cc
        self.hidden = 0

    def __enter__(self) -> "_PlainDisplay":
        print()
        print(f"Phase 2 — P25 decode  ({self.total} to characterize, "
              f"{self.skipped} skipped via resume)")
        print(_PLAIN_FMT.format(*_HEADERS))
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.hidden:
            print(f"  ({self.hidden} no-cc rows hidden via --hide-no-cc)")

    def set_current(self, freq_hz: int | None, max_dwell_s: float = 0.0) -> None:
        """No-op in plain mode (output is line-by-line; rows appear as decoded)."""
        return None

    def add(self, record: SurveyRecord, status: str) -> None:
        if self.hide_no_cc and status == "no-cc":
            self.hidden += 1
            return
        print(_PLAIN_FMT.format(*_row_values(record, status)), flush=True)


class _RichDisplay(AbstractContextManager):
    """Live-updating rich table with in-flight candidate spinner."""

    _SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, total: int, skipped: int, hide_no_cc: bool = False) -> None:
        from rich.console import Console  # noqa: PLC0415
        from rich.live import Live  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415

        self._Table = Table
        self._console = Console()
        self._total = total
        self._skipped = skipped
        self._hide_no_cc = hide_no_cc
        self._hidden = 0
        self._rows: list[tuple[tuple[str, ...], str]] = []  # (row, status)
        self._current_freq_hz: int | None = None
        self._current_started: float = 0.0
        self._current_max_dwell_s: float = 0.0
        self._lock = threading.Lock()
        self._stop_ticker = threading.Event()
        self._ticker_thread: threading.Thread | None = None
        self._live = Live(
            self._render(), console=self._console,
            refresh_per_second=8, vertical_overflow="visible",
            auto_refresh=False,  # we drive refreshes from the ticker
        )

    def __enter__(self) -> "_RichDisplay":
        self._live.__enter__()
        self._ticker_thread = threading.Thread(target=self._ticker, daemon=True)
        self._ticker_thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop_ticker.set()
        if self._ticker_thread is not None:
            self._ticker_thread.join(timeout=1.0)
        self._live.__exit__(*exc)
        if self._hidden:
            self._console.print(
                f"  ({self._hidden} no-cc rows hidden via --hide-no-cc)",
                style="dim",
            )

    def _ticker(self) -> None:
        """Re-render the live view on a fixed cadence so the spinner animates
        and elapsed-time counters update without depending on add()/start()
        being called frequently."""
        while not self._stop_ticker.is_set():
            with self._lock:
                if self._current_freq_hz is not None:
                    try:
                        self._live.update(self._render(), refresh=True)
                    except Exception:
                        pass
            self._stop_ticker.wait(timeout=0.125)

    def set_current(self, freq_hz: int | None, max_dwell_s: float = 0.0) -> None:
        with self._lock:
            self._current_freq_hz = freq_hz
            self._current_started = time.monotonic() if freq_hz is not None else 0.0
            self._current_max_dwell_s = max_dwell_s
            self._live.update(self._render(), refresh=True)

    def add(self, record: SurveyRecord, status: str) -> None:
        with self._lock:
            if self._hide_no_cc and status == "no-cc":
                self._hidden += 1
            else:
                self._rows.append((_row_values(record, status), status))
            self._current_freq_hz = None
            self._live.update(self._render(), refresh=True)

    def _status_style(self, status: str) -> str:
        return {
            "complete": "green",
            "partial": "yellow",
            "no-cc": "dim",
        }.get(status, "")

    def _spinner_frame(self) -> str:
        idx = int(time.monotonic() * 10) % len(self._SPINNER_FRAMES)
        return self._SPINNER_FRAMES[idx]

    def _render_current(self):
        """Status line for the candidate currently being decoded.
        Renders a spinner + elapsed time + max budget."""
        from rich.text import Text  # noqa: PLC0415
        if self._current_freq_hz is None:
            return Text("")
        elapsed = time.monotonic() - self._current_started
        budget = self._current_max_dwell_s
        budget_str = f"/ {budget:.0f}s budget" if budget else ""
        return Text.assemble(
            ("  ", ""),
            (self._spinner_frame() + " ", "cyan"),
            (f"Decoding {self._current_freq_hz / 1_000_000:.5f} MHz", "bold"),
            (f"   {elapsed:4.1f}s {budget_str}", "dim"),
        )

    def _render(self):
        from rich.console import Group  # noqa: PLC0415
        title = (f"Phase 2 — P25 decode  "
                 f"[{len(self._rows)}/{self._total} done, "
                 f"{self._skipped} skipped via resume]")
        table = self._Table(title=title, show_header=True, header_style="bold")
        for h, justify in zip(_HEADERS, (
            "right", "right", "right", "right", "right", "right", "right",
            "right", "right", "right", "left", "left",
        )):
            table.add_column(h, justify=justify)
        for row, status in self._rows:
            style = self._status_style(status)
            table.add_row(*row, style=style)
        return Group(table, self._render_current())


def make_display(total: int, skipped: int, hide_no_cc: bool = False) -> AbstractContextManager:
    """Return the right display for the current stdout (rich if TTY + installed)."""
    if not sys.stdout.isatty():
        return _PlainDisplay(total, skipped, hide_no_cc=hide_no_cc)
    try:
        import rich  # noqa: F401, PLC0415
    except ImportError:
        return _PlainDisplay(total, skipped, hide_no_cc=hide_no_cc)
    return _RichDisplay(total, skipped, hide_no_cc=hide_no_cc)
