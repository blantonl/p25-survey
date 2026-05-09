"""Match decoded SurveyRecords against RadioReference ground-truth data.

Given a decoded `SurveyRecord` and an `RRClient`, produce an
`EnrichmentResult` capturing:
  - whether the WACN/SYSID is in RR
  - whether the RFSS/Site is in RR
  - whether the decoded CC frequency matches one listed for the site
  - the absolute and ppm offset between decoded and listed frequency
  - the observed ADJ_STS_BCST neighbors annotated with RR site data,
    so admins can spot-check the per-neighbor description/text
  - which observed neighbors have (RFSS, Site) IDs that don't resolve
    in the RR roster (new-site submission candidates)
  - per-neighbor CC verification: whether the advertised CC frequency
    is listed for that neighbor's RR site, and whether it's flagged
    as a control channel (use code "d" or "a")

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
class NeighborRef:
    """Reference to a neighbor site by (rfss, site) identity.

    Carries along whatever frequency / RR site context we know so the
    report can render something more useful than bare numbers. `in_rr`
    distinguishes neighbors that match an RR site from new-site
    candidates (which never have description/location).
    """
    rfss_id: int
    site_id: int
    freq_hz: int | None = None        # advertised CC from ADJ_STS_BCST
    in_rr: bool = False
    description: str | None = None    # RR site description, when in_rr
    location: str | None = None       # RR site location, when in_rr
    county: str | None = None         # RR site county, when in_rr


@dataclass
class NeighborCCMismatch:
    """An ADJ_STS_BCST neighbor whose advertised CC freq doesn't line up
    with the RR-listed frequencies for that site.

    Two failure modes:
      - kind="missing_from_rr": advertised freq isn't in the site's RR
        frequency list at all (within 1 kHz tolerance) → freq needs to
        be added to RR
      - kind="not_marked_control": freq is listed but its `use` code
        isn't "d"/"a" → use-code is wrong in RR
    """
    rfss_id: int
    site_id: int
    advertised_cc_hz: int
    kind: str                              # "missing_from_rr" | "not_marked_control"
    rr_use_code: str | None = None         # populated when kind="not_marked_control"
    rr_site_description: str | None = None


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
    # True when the system has multiple sites with this RFSS/Site number
    # (multi-subsystem layout) and NAC didn't pick one. Distinct from a
    # genuine "new site" — we know the site exists, just can't tell which.
    ambiguous_site: bool = False

    # When system_match=True
    rr_system_name: str | None = None
    rr_sid: int | None = None

    # When site_match=True
    rr_site_description: str | None = None
    rr_site_location: str | None = None
    rr_site_county: str | None = None
    rr_site_nac: str | None = None
    rr_site_lat: float | None = None
    rr_site_lon: float | None = None
    rr_site_licenses: list[str] = field(default_factory=list)

    # Frequency comparison (only set when site_match=True)
    cc_freq_offset: FreqOffset | None = None
    cc_freq_in_db: bool = False              # decoded freq matches some listed CC

    # All neighbors observed in ADJ_STS_BCST, annotated with RR data when
    # we matched them to the system roster. Useful for admins eyeballing
    # whether per-neighbor descriptions need updating. Only populated on
    # site_match=True.
    observed_neighbors: list[NeighborRef] = field(default_factory=list)

    # Subset of observed_neighbors with `in_rr=False` — the
    # high-signal "new site" submission candidates.
    neighbors_decoded_not_in_rr: list[NeighborRef] = field(default_factory=list)

    # Per-neighbor CC verification (only when site_match=True). For each
    # observed neighbor that resolved to an RR site, we check whether the
    # advertised CC frequency is listed and flagged as control. Misses
    # become entries here.
    neighbor_cc_mismatches: list[NeighborCCMismatch] = field(default_factory=list)

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

    Multi-stage matching:
      1. WACN/SYSID → list of candidate systems. RR sometimes lists more
         than one distinct system carrying the same WACN/SYSID (e.g. two
         single-site Mississippi county networks both running 92448/00A).
      2. For each candidate, try to resolve the decoded RFSS/Site (with
         NAC disambiguation when one system has multiple subsystems).
      3. Pick the candidate(s) that yielded a site_match. Exactly one →
         that's our result. Zero or many → flag ambiguous and explain.

    Defensive against partial records (e.g. WACN seen but SYSID not yet) —
    such records get an empty EnrichmentResult with a note.
    """
    if record.wacn is None or record.sysid is None:
        return EnrichmentResult(notes=["partial record: missing WACN or SYSID, skipped enrichment"])

    wacn_hex = format(record.wacn, "05X")
    sysid_hex = format(record.sysid, "03X")

    try:
        candidate_systems = client.find_systems_by_wacn_sysid(wacn_hex, sysid_hex)
    except RRError as exc:
        return EnrichmentResult(notes=[f"RR lookup failed: {exc}"])

    if not candidate_systems:
        return EnrichmentResult(
            system_match=False,
            notes=[f"new system: WACN {wacn_hex} / SYSID {sysid_hex} not found in RadioReference"],
        )

    # If RFSS/Site weren't decoded we can't disambiguate among candidates.
    # Fall back to the first match and surface the others in a note so the
    # admin knows the system attribution may be wrong.
    if record.rfss_id is None or record.site_id is None:
        first = candidate_systems[0]
        result = EnrichmentResult(
            system_match=True,
            rr_system_name=first.name,
            rr_sid=first.sid,
        )
        result.notes.append("RFSS/Site not decoded; can't match site-level data")
        if len(candidate_systems) > 1:
            others = ", ".join(f"{s.name!r} (sid={s.sid})" for s in candidate_systems[1:])
            result.notes.append(
                f"WACN {wacn_hex}/SYSID {sysid_hex} matches multiple RR systems; "
                f"reporting against first: {first.name!r} (sid={first.sid}); also: {others}"
            )
        return result

    # Try to resolve the site against each candidate system.
    resolved: list[EnrichmentResult] = []
    per_candidate_state: list[tuple[RRSystem, list[RRSite], bool]] = []
    for system in candidate_systems:
        try:
            sites = client.get_sites(system.sid)
        except RRError as exc:
            # Note but don't abort — try the other candidates.
            per_candidate_state.append((system, [], False))
            continue
        site, ambiguous = _find_site(sites, record.rfss_id, record.site_id, record.nac)
        per_candidate_state.append((system, sites, ambiguous))
        if site is not None:
            resolved.append(_build_site_match(record, system, sites, site))

    if len(resolved) == 1:
        return resolved[0]

    if len(resolved) > 1:
        # More than one RR system has this WACN/SYSID *and* an RFSS/Site
        # match. Genuinely ambiguous — surface the candidates and refuse to
        # pick. Very rare in practice but worth being explicit about.
        first = resolved[0]
        first.ambiguous_site = True
        names = ", ".join(f"{r.rr_system_name!r} (sid={r.rr_sid})" for r in resolved)
        first.notes.append(
            f"ambiguous: WACN {wacn_hex}/SYSID {sysid_hex} matches multiple RR systems "
            f"that all have RFSS {record.rfss_id} / Site {record.site_id}: {names}"
        )
        return first

    # No site_match in any candidate. Three sub-cases:
    first = candidate_systems[0]
    result = EnrichmentResult(
        system_match=True,
        rr_system_name=first.name,
        rr_sid=first.sid,
    )

    # 3a. Some candidate had a within-system multi-subsystem ambiguity
    #     (same RFSS/Site number on multiple subsystems within one RR sid;
    #     NAC didn't pick one). Surface that case explicitly.
    for system, sites, ambiguous in per_candidate_state:
        if ambiguous:
            result.ambiguous_site = True
            nac_hex = format(record.nac, "03X") if record.nac is not None else "—"
            candidate_nacs = ", ".join(
                f"{s.nac or '(blank)'}"
                for s in sites
                if s.rfss == record.rfss_id and s.site_number == record.site_id
            )
            result.notes.append(
                f"ambiguous site: RFSS {record.rfss_id} / Site {record.site_id} "
                f"matches multiple subsystems in {system.name!r} (sid={system.sid}); "
                f"decoded NAC {nac_hex} doesn't match any candidate "
                f"(RR has NAC {candidate_nacs})"
            )
            return result

    # 3b. Multiple candidate systems share this WACN/SYSID but none has
    #     this RFSS/Site. Russ's case: we can't tell which of the
    #     candidate systems the new site would belong to, so flag rather
    #     than falsely attribute it to whichever returned first.
    if len(candidate_systems) > 1:
        result.ambiguous_site = True
        names = ", ".join(f"{s.name!r} (sid={s.sid})" for s in candidate_systems)
        result.notes.append(
            f"ambiguous: WACN {wacn_hex}/SYSID {sysid_hex} matches multiple RR systems "
            f"({names}) but RFSS {record.rfss_id} / Site {record.site_id} isn't in any "
            f"of them — can't tell which system this would be a new site for"
        )
        return result

    # 3c. Single candidate, no site found. Genuine new-site case.
    result.notes.append(
        f"new site: RFSS {record.rfss_id} / Site {record.site_id} not in "
        f"RR for system {first.name!r} (sid={first.sid})"
    )
    return result


def _build_site_match(record: SurveyRecord, system: RRSystem,
                      sites: list[RRSite], site: RRSite) -> EnrichmentResult:
    """Build a fully-populated EnrichmentResult once we've matched a site."""
    result = EnrichmentResult(
        system_match=True,
        rr_system_name=system.name,
        rr_sid=system.sid,
        site_match=True,
        rr_site_description=site.description or None,
        rr_site_location=site.location or None,
        rr_site_county=site.county or None,
        rr_site_nac=site.nac or None,
        rr_site_lat=site.lat,
        rr_site_lon=site.lon,
        rr_site_licenses=list(site.licenses),
    )

    cc_freqs = site.control_freqs_hz()
    if cc_freqs:
        expected = min(cc_freqs, key=lambda f: abs(f - record.freq_hz))
        offset = record.freq_hz - expected
        result.cc_freq_offset = FreqOffset(
            decoded_hz=record.freq_hz,
            expected_hz=expected,
            offset_hz=offset,
            ppm=round(offset / record.freq_hz * 1e6, 3) if record.freq_hz else 0.0,
        )
        result.cc_freq_in_db = abs(offset) < 1000  # within 1 kHz = same channel

    # Neighbor cross-reference. ADJ_STS_BCST gives us a small adjacency
    # list with per-neighbor (RFSS, Site) IDs and an advertised CC freq;
    # we annotate each entry with the RR site (when found) for
    # admin-verification, flag IDs not in RR as new-site candidates, and
    # cross-check the advertised CC against the RR frequency list.
    rr_neighbor_sites = _rr_neighbor_sites(sites, exclude_site=site)

    for n in sorted(record.neighbors, key=lambda x: (x.rfss_id, x.site_id)):
        rr_site = rr_neighbor_sites.get((n.rfss_id, n.site_id))
        if rr_site is None:
            ref = NeighborRef(
                rfss_id=n.rfss_id, site_id=n.site_id,
                freq_hz=n.freq_hz, in_rr=False,
            )
            result.observed_neighbors.append(ref)
            result.neighbors_decoded_not_in_rr.append(ref)
            continue

        result.observed_neighbors.append(NeighborRef(
            rfss_id=n.rfss_id, site_id=n.site_id,
            freq_hz=n.freq_hz, in_rr=True,
            description=rr_site.description or None,
            location=rr_site.location or None,
            county=rr_site.county or None,
        ))

        mismatch = _check_neighbor_cc(n, rr_site)
        if mismatch is not None:
            result.neighbor_cc_mismatches.append(mismatch)

    return result


def _check_neighbor_cc(neighbor: NeighborSite,
                       rr_site: RRSite) -> NeighborCCMismatch | None:
    """Verify the advertised CC frequency against the neighbor's RR site.

    Returns None if the advertised freq is listed AND flagged as control,
    otherwise a NeighborCCMismatch describing the gap.
    """
    if not rr_site.frequencies:
        # No frequency data at all — treat as missing from RR.
        return NeighborCCMismatch(
            rfss_id=neighbor.rfss_id, site_id=neighbor.site_id,
            advertised_cc_hz=neighbor.freq_hz, kind="missing_from_rr",
            rr_site_description=rr_site.description or None,
        )

    closest = min(rr_site.frequencies, key=lambda f: abs(f.freq_hz - neighbor.freq_hz))
    if abs(closest.freq_hz - neighbor.freq_hz) >= 1000:  # >1 kHz away = different channel
        return NeighborCCMismatch(
            rfss_id=neighbor.rfss_id, site_id=neighbor.site_id,
            advertised_cc_hz=neighbor.freq_hz, kind="missing_from_rr",
            rr_site_description=rr_site.description or None,
        )
    if not closest.is_control:
        return NeighborCCMismatch(
            rfss_id=neighbor.rfss_id, site_id=neighbor.site_id,
            advertised_cc_hz=neighbor.freq_hz, kind="not_marked_control",
            rr_use_code=closest.use or "",
            rr_site_description=rr_site.description or None,
        )
    return None


def _find_site(sites: Iterable[RRSite], rfss_id: int, site_id: int,
               nac: int | None = None) -> tuple[RRSite | None, bool]:
    """Match decoded RFSS/Site (and NAC) to RR's per-site records.

    Decoded `site_id` corresponds to RR's `siteNumber` (the P25 site number),
    not RR's internal `siteId` DB key.

    Multi-subsystem networks (one RR `sid` covering more than one P25
    WACN/SYSID, e.g. Alabama AIRS) commonly have the same RFSS/Site
    number on both subsystems. NAC is what disambiguates them — each
    subsystem uses its own NAC range. When the RFSS/Site lookup matches
    more than one site, we require NAC equality to pick.

    Returns (site, ambiguous):
      - (site, False)  → unique match (or NAC narrowed it down)
      - (None, False)  → no RFSS/Site match at all (genuine new site)
      - (None, True)   → multiple RFSS/Site matches, NAC didn't pick one
    """
    matches = [s for s in sites if s.rfss == rfss_id and s.site_number == site_id]
    if not matches:
        return None, False
    if len(matches) == 1:
        return matches[0], False
    if nac is not None:
        nac_hex = format(nac, "03X").upper().lstrip("0") or "0"
        nac_matches = [
            s for s in matches
            if (s.nac or "").upper().lstrip("0") == nac_hex
        ]
        if len(nac_matches) == 1:
            return nac_matches[0], False
    return None, True


def site_nac_diff(decoded_nac: int | None, rr_site_nac: str | None) -> str | None:
    """Compare decoded vs RR site NAC. Returns "missing", "differs", or None.

    "missing": RR's site has no NAC field populated; decoded NAC is a
        candidate to fill in (many systems carry a "PLEASE SUBMIT" note
        about missing NACs). "differs": RR has a NAC and it doesn't match
        the decoded NAC. Comparison strips leading zeros and uppercases on
        both sides — same normalization as `_find_site` above.
    """
    if decoded_nac is None:
        return None
    rr_raw = (rr_site_nac or "").strip()
    if not rr_raw:
        return "missing"
    decoded_hex = format(decoded_nac, "03X").upper().lstrip("0") or "0"
    rr_norm = rr_raw.upper().lstrip("0") or "0"
    return "differs" if rr_norm != decoded_hex else None


def _rr_neighbor_sites(all_sites: Iterable[RRSite],
                       exclude_site: RRSite) -> dict[tuple[int, int], RRSite]:
    """All sites in the system keyed by (rfss, site_number), minus current."""
    out: dict[tuple[int, int], RRSite] = {}
    for s in all_sites:
        if s.site_number == exclude_site.site_number and s.rfss == exclude_site.rfss:
            continue
        out[(s.rfss, s.site_number)] = s
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


@dataclass
class PpmRecommendation:
    """Single recommended `--ppm` value derived from all matched sites.

    Aggregates across bands (not within) on the assumption that the SDR
    has a single oscillator — true for every supported driver
    (RTL-SDR, Airspy, HackRF). The per-band breakdown remains useful
    as a diagnostic; this is the actionable number for `--ppm`.
    """
    n_samples: int
    median_ppm: float | None              # None when n_samples < MIN_SAMPLES
    iqr_ppm: float | None                 # interquartile range; None when n_samples < MIN_SAMPLES
    consistency: str                      # "high" | "medium" | "low" | "insufficient_data"
    cross_band_warning: str | None = None # set when per-band medians diverge


# Minimum number of matched sites before we'll recommend a value at all.
# Below this, the median is too easily skewed by a single bad site.
_PPM_MIN_SAMPLES = 3


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


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile on a pre-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def recommend_ppm(records: Iterable[SurveyRecord],
                  enrichments: dict[int, EnrichmentResult]
                  ) -> PpmRecommendation:
    """Single `--ppm` recommendation across all matched sites.

    Median of every per-site ppm offset (one per matched site). Median
    is robust to one bad site dragging the average. IQR drives a
    consistency rating so the user knows whether to trust it.

    Cross-band sanity check: if multiple bands each have ≥2 samples and
    their medians span more than 0.5 ppm, attach a warning — that's
    suspicious for a single-TCXO dongle and could mean one band has a
    site with a real RR-database error skewing things.
    """
    by_band: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        e = enrichments.get(rec.freq_hz)
        if e is None or not e.cc_freq_offset:
            continue
        band = find_band(rec.freq_hz)
        band_name = band.name if band else "unknown"
        by_band[band_name].append(e.cc_freq_offset.ppm)

    all_ppms = sorted(p for ppms in by_band.values() for p in ppms)
    n = len(all_ppms)
    if n < _PPM_MIN_SAMPLES:
        return PpmRecommendation(
            n_samples=n, median_ppm=None, iqr_ppm=None,
            consistency="insufficient_data",
        )

    median = _percentile(all_ppms, 0.5)
    iqr = _percentile(all_ppms, 0.75) - _percentile(all_ppms, 0.25)
    if iqr < 0.2:
        consistency = "high"
    elif iqr < 0.5:
        consistency = "medium"
    else:
        consistency = "low"

    warning: str | None = None
    multi_band = [(name, ppms) for name, ppms in by_band.items() if len(ppms) >= 2]
    if len(multi_band) >= 2:
        band_medians = {name: _percentile(sorted(ppms), 0.5) for name, ppms in multi_band}
        spread = max(band_medians.values()) - min(band_medians.values())
        if spread > 0.5:
            hi = max(band_medians, key=band_medians.get)
            lo = min(band_medians, key=band_medians.get)
            warning = (
                f"per-band medians diverge by {spread:.2f} ppm "
                f"({lo}: {band_medians[lo]:+.2f}, {hi}: {band_medians[hi]:+.2f}); "
                f"unusual for a single-oscillator SDR — check whether one band has a "
                f"site with a stale RR frequency"
            )

    return PpmRecommendation(
        n_samples=n,
        median_ppm=round(median, 3),
        iqr_ppm=round(iqr, 3),
        consistency=consistency,
        cross_band_warning=warning,
    )
