"""Survey output — SurveyRecord dataclass and append-only NDJSON writer.

One JSON object per line, fsynced after each write so a crashed scan
leaves a usable file. The writer also reads back existing records on
construction (when --resume is set) so the orchestrator can skip
already-characterized frequencies.

Hex-encoded fields (wacn, sysid, nac) are stored as integers in the
dataclass and serialized as uppercase hex strings without prefix, matching
RadioReference conventions. Round-trip via from_json restores the ints.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NeighborSite:
    freq_hz: int
    rfss_id: int
    site_id: int
    sysid: int | None = None
    wacn: int | None = None
    # ADJ_STS_BCST status flags (None when not decoded; see tsbk.AdjStsBcst).
    conventional: bool | None = None
    site_failure: bool | None = None
    valid: bool | None = None
    network_active: bool | None = None


@dataclass(frozen=True)
class IdenUpEntry:
    iden: int
    base_freq_hz: int
    step_hz: int
    offset_hz: int
    is_tdma: bool = False
    slots_per_carrier: int = 1
    bandwidth_hz: int | None = None


@dataclass
class SignalQuality:
    rssi_dbfs_mean: float | None = None
    rssi_dbfs_peak: float | None = None
    ber_pct_mean: float | None = None
    decode_rate_pct: float | None = None


@dataclass
class SurveyRecord:
    """One detected control channel + everything we extracted about it."""
    freq_hz: int
    ts: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"))
    complete: bool = False
    wacn: int | None = None
    sysid: int | None = None
    nac: int | None = None
    rfss_id: int | None = None
    site_id: int | None = None
    # RFSS_STS_BCST A bit for this site: True == active RFSS network connection,
    # False == failsoft / site-trunking, None == not decoded.
    site_network_active: bool | None = None
    neighbors: list[NeighborSite] = field(default_factory=list)
    secondary_cc: list[int] = field(default_factory=list)
    iden_up: list[IdenUpEntry] = field(default_factory=list)
    signal: SignalQuality = field(default_factory=SignalQuality)
    dwell_ms: int = 0
    sdr_driver: str = ""
    sdr_gain_db: float | None = None
    sdr_ppm: float = 0.0
    notes: list[str] = field(default_factory=list)
    # RR enrichment, attached after lookup. Stored as a generic dict so
    # survey.py stays free of any RR-specific imports. Populated from
    # EnrichmentResult.to_json_dict() in the orchestrator.
    rr: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Hex-encode the system identity fields. None stays None.
        for key, width in (("wacn", 5), ("sysid", 3), ("nac", 3)):
            if d[key] is not None:
                d[key] = format(d[key], f"0{width}X")
        for n in d["neighbors"]:
            if n["wacn"] is not None:
                n["wacn"] = format(n["wacn"], "05X")
            if n["sysid"] is not None:
                n["sysid"] = format(n["sysid"], "03X")
        return d

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "SurveyRecord":
        # Decode hex strings back to ints.
        for key in ("wacn", "sysid", "nac"):
            if d.get(key) is not None and isinstance(d[key], str):
                d[key] = int(d[key], 16)
        neighbors = []
        for n in d.get("neighbors", []) or []:
            for key in ("wacn", "sysid"):
                if n.get(key) is not None and isinstance(n[key], str):
                    n[key] = int(n[key], 16)
            neighbors.append(NeighborSite(**n))
        iden_up = [IdenUpEntry(**i) for i in d.get("iden_up", []) or []]
        signal = SignalQuality(**(d.get("signal", {}) or {}))
        return cls(
            freq_hz=d["freq_hz"],
            ts=d.get("ts", ""),
            complete=d.get("complete", False),
            wacn=d.get("wacn"),
            sysid=d.get("sysid"),
            nac=d.get("nac"),
            rfss_id=d.get("rfss_id"),
            site_id=d.get("site_id"),
            site_network_active=d.get("site_network_active"),
            neighbors=neighbors,
            secondary_cc=list(d.get("secondary_cc", []) or []),
            iden_up=iden_up,
            signal=signal,
            dwell_ms=d.get("dwell_ms", 0),
            sdr_driver=d.get("sdr_driver", ""),
            sdr_gain_db=d.get("sdr_gain_db"),
            sdr_ppm=d.get("sdr_ppm", 0.0),
            notes=list(d.get("notes", []) or []),
            rr=d.get("rr"),
        )


# ---------------------------------------------------------------------------
# NDJSON writer
# ---------------------------------------------------------------------------


class SurveyWriter:
    """NDJSON writer with optional resume.

    Default behavior: truncates the output file at construction so a
    fresh scan never silently appends to a previous run's data.

    With resume=True: reads any existing records from `path`, keeps them in
    place, and exposes `already_characterized(freq_hz)` so the caller can
    skip already-done frequencies and append only new ones.
    """

    def __init__(self, path: str | Path, resume: bool = False) -> None:
        self.path = Path(path)
        self._existing_freqs: set[int] = set()
        if resume and self.path.exists():
            self._existing_freqs = _read_existing_freqs(self.path)
        elif not resume:
            # Truncate any previous content. Crash safety for the new run is
            # preserved because each subsequent append() flushes + fsyncs.
            if self.path.exists() or self.path.parent.exists():
                self.path.write_text("", encoding="utf-8")

    @property
    def existing_freqs(self) -> set[int]:
        return self._existing_freqs

    def already_characterized(self, freq_hz: int) -> bool:
        return freq_hz in self._existing_freqs

    def append(self, record: SurveyRecord) -> None:
        line = json.dumps(record.to_json_dict(), ensure_ascii=False, sort_keys=True)
        # Append + fsync so the file survives a crash mid-scan.
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._existing_freqs.add(record.freq_hz)


def _read_existing_freqs(path: Path) -> set[int]:
    """Read freq_hz values from an existing NDJSON survey, ignoring bad lines."""
    freqs: set[int] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            freq = rec.get("freq_hz")
            if isinstance(freq, int):
                freqs.add(freq)
    return freqs


def read_survey(path: str | Path) -> list[SurveyRecord]:
    """Load all records from an NDJSON survey file."""
    p = Path(path)
    records: list[SurveyRecord] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            records.append(SurveyRecord.from_json_dict(d))
    return records
