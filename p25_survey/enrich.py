"""Match decoded SurveyRecords against RadioReference ground-truth data.

Given a decoded `SurveyRecord` and an `RRClient`, produce an
`EnrichmentResult` capturing:
  - whether the WACN/SYSID is in RR
  - whether the RFSS/Site is in RR
  - whether the decoded CC frequency matches one listed for the site
  - the absolute and ppm offset between decoded and listed frequency
  - which neighbors we found that aren't in RR's neighbor list

The result is attached to `SurveyRecord.rr` (a new optional field). The
output stages — text report, submission report, console summary — read
from there.

Per-band offset summary is computed across the whole scan in
`summarize_band_offsets()` so the user gets a "VHF: +0.4 ppm, 800: -0.2
ppm" overview at the end.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Iterable

from p25_survey.bands import describe_band, find_band
from p25_survey.radioreference import (
    RRClient,
    RRError,
    RRSite,
    RRSystem,
)
from p25_survey.survey import NeighborSite, SurveyRecord


@dataclass
class FreqOffset:
    """Difference between a decoded frequency and the RR-listed frequency."""
    decoded_hz: int
    expected_hz: int
    offset_hz: int                    # decoded - expected
    ppm: float                        # offset_hz / decoded_hz * 1e6


@dataclass
class EnrichmentResult:
    """All info we know about an SurveyRecord after RR cross-reference.

    Cardinality:
      - system_match=False, site_match=False     → unknown system; everything's a submission
      - system_match=True,  site_match=False     → known system, new site (RFSS/Site not in DB)
      - system_match=True,  site_match=True      → known site; check freq + neighbors
    """
    system_match: bool = False
    site_match: bool = False

    # When system_match=True
    rr_system_name: str | None = None
    rr_sid: int | None = None

    # When site_match=True
    rr_site_description: str | None = None
    rr_site_location: str | None = None
    rr_site_county: str | None = None
    rr_site_lat: float | None = None
    rr_site_lon: float | None = None
    rr_site_licenses: list[str] = field(default_factory=list)

    # Frequency comparison (only set when site_match=True)
    cc_freq_offset: FreqOffset | None = None
    cc_freq_in_db: bool = False              # decoded freq matches some listed CC

    # Neighbor diff (only when site_match=True)
    neighbors_in_rr_not_decoded: list[int] = field(default_factory=list)   # freqs we missed
    neighbors_decoded_not_in_rr: list[int] = field(default_factory=list)   # freqs RR doesn't list

    # Free-form
    notes: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Per-record matching
# ---------------------------------------------------------------------------


def enrich_record(record: SurveyRecord, client: RRClient) -> EnrichmentResult:
    """Look up a single record in RR and produce an EnrichmentResult.

    Defensive against partial records (e.g. WACN seen but SYSID not yet) —
    such records get an empty EnrichmentResult with a note.
    """
    if record.wacn is None or record.sysid is None:
        return EnrichmentResult(notes=["partial record: missing WACN or SYSID, skipped enrichment"])

    wacn_hex = format(record.wacn, "05X")
    sysid_hex = format(record.sysid, "03X")

    try:
        system = client.find_system_by_wacn_sysid(wacn_hex, sysid_hex)
    except RRError as exc:
        return EnrichmentResult(notes=[f"RR lookup failed: {exc}"])

    if system is None:
        return EnrichmentResult(
            system_match=False,
            notes=[f"new system: WACN {wacn_hex} / SYSID {sysid_hex} not found in RadioReference"],
        )

    result = EnrichmentResult(
        system_match=True,
        rr_system_name=system.name,
        rr_sid=system.sid,
    )

    if record.rfss_id is None or record.site_id is None:
        result.notes.append("RFSS/Site not decoded; can't match site-level data")
        return result

    try:
        sites = client.get_sites(system.sid)
    except RRError as exc:
        result.notes.append(f"could not fetch sites for sid={system.sid}: {exc}")
        return result

    site = _find_site(sites, record.rfss_id, record.site_id)
    if site is None:
        result.notes.append(
            f"new site: RFSS {record.rfss_id} / Site {record.site_id} not in "
            f"RR for system {system.name!r} (sid={system.sid})"
        )
        return result

    result.site_match = True
    result.rr_site_description = site.description or None
    result.rr_site_location = site.location or None
    result.rr_site_county = site.county or None
    result.rr_site_lat = site.lat
    result.rr_site_lon = site.lon
    result.rr_site_licenses = list(site.licenses)

    # Frequency comparison
    cc_freqs = site.control_freqs_hz()
    if cc_freqs:
        # Find the closest expected CC freq to what we decoded
        expected = min(cc_freqs, key=lambda f: abs(f - record.freq_hz))
        offset = record.freq_hz - expected
        result.cc_freq_offset = FreqOffset(
            decoded_hz=record.freq_hz,
            expected_hz=expected,
            offset_hz=offset,
            ppm=round(offset / record.freq_hz * 1e6, 3) if record.freq_hz else 0.0,
        )
        result.cc_freq_in_db = abs(offset) < 1000  # within 1 kHz = same channel

    # Neighbor comparison: compare RR's other-site CC freqs vs our neighbor list
    rr_neighbor_freqs = _rr_neighbor_freqs(sites, exclude_site=site)
    decoded_neighbor_freqs = {n.freq_hz for n in record.neighbors}

    result.neighbors_in_rr_not_decoded = sorted(rr_neighbor_freqs - decoded_neighbor_freqs)
    result.neighbors_decoded_not_in_rr = sorted(decoded_neighbor_freqs - rr_neighbor_freqs)

    return result


def _find_site(sites: Iterable[RRSite], rfss_id: int, site_id: int) -> RRSite | None:
    """Match decoded RFSS/Site to RR's per-site records.

    Decoded `site_id` corresponds to RR's `siteNumber` (the P25 site number),
    not RR's internal `siteId` DB key.
    """
    for s in sites:
        if s.rfss == rfss_id and s.site_number == site_id:
            return s
    return None


def _rr_neighbor_freqs(all_sites: Iterable[RRSite], exclude_site: RRSite) -> set[int]:
    """All control-channel freqs across the system, minus the current site."""
    out: set[int] = set()
    for s in all_sites:
        if s.site_number == exclude_site.site_number and s.rfss == exclude_site.rfss:
            continue
        out.update(s.control_freqs_hz())
    return out


# ---------------------------------------------------------------------------
# Per-band offset summary
# ---------------------------------------------------------------------------


@dataclass
class BandOffsetSummary:
    band_name: str
    n_samples: int
    mean_ppm: float
    median_ppm: float
    mean_offset_hz: int


def summarize_band_offsets(records: Iterable[SurveyRecord],
                           enrichments: dict[int, EnrichmentResult]
                          ) -> list[BandOffsetSummary]:
    """Group offsets by band and compute mean / median ppm per band.

    Records lacking a site_match offset contribute nothing. Bands with no
    matched records are absent from the output.
    """
    by_band: dict[str, list[FreqOffset]] = defaultdict(list)
    for rec in records:
        e = enrichments.get(rec.freq_hz)
        if e is None or not e.cc_freq_offset:
            continue
        band = find_band(rec.freq_hz)
        band_name = band.name if band else "unknown"
        by_band[band_name].append(e.cc_freq_offset)

    out: list[BandOffsetSummary] = []
    for band_name, offsets in sorted(by_band.items()):
        ppms = sorted(o.ppm for o in offsets)
        hzs = [o.offset_hz for o in offsets]
        n = len(ppms)
        mean = sum(ppms) / n
        median = ppms[n // 2] if n % 2 == 1 else (ppms[n // 2 - 1] + ppms[n // 2]) / 2
        out.append(BandOffsetSummary(
            band_name=band_name,
            n_samples=n,
            mean_ppm=round(mean, 3),
            median_ppm=round(median, 3),
            mean_offset_hz=int(round(sum(hzs) / n)),
        ))
    return out
