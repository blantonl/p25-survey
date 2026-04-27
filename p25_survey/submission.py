"""Markdown submission report for survey data not in RadioReference.

Run after the scan completes (and after enrichment). Walks the survey
records + their EnrichmentResults and produces a `survey-*-submissions.md`
file organized as:

  1. Summary header (counts, per-band ppm offset table)
  2. New systems (WACN/SYSID we found that RR has no record of)
  3. New sites (system in RR, but RFSS/Site not yet listed)
  4. New / corrected frequencies (site in RR, decoded freq not listed
     or off by more than 1 kHz)
  5. Neighbor differences (sites we observed in ADJ_STS_BCST whose
     RFSS+Site isn't in RR's roster, plus the inverse direction for
     admin awareness)

Designed to be pasted into a forum thread or RR support ticket.
"""

from __future__ import annotations

from io import StringIO
from typing import Iterable

from p25_survey.enrich import (
    BandOffsetSummary,
    EnrichmentResult,
    summarize_band_offsets,
)
from p25_survey.survey import SurveyRecord


def _fmt_freq_mhz(freq_hz: int) -> str:
    rounded = (freq_hz + 5) // 10 * 10
    return f"{rounded // 1_000_000}.{(rounded % 1_000_000) // 10:05d} MHz"


def _hex(value: int | None, width: int) -> str:
    return format(value, f"0{width}X") if value is not None else "—"


def _wrote_section(out: StringIO, header: str, body_writer) -> bool:
    """Run body_writer; only emit the header if it wrote anything."""
    buf = StringIO()
    body_writer(buf)
    text = buf.getvalue()
    if not text.strip():
        return False
    out.write(header)
    out.write(text)
    return True


def render(records: list[SurveyRecord],
           enrichments: dict[int, EnrichmentResult]) -> str:
    """Build the full submission markdown."""
    out = StringIO()
    out.write("# RadioReference submission report\n\n")
    out.write(f"Survey produced this report by comparing decoded P25 control "
              f"channels against the RadioReference database. Each section below "
              f"lists data we found that isn't in RR (or differs from what RR "
              f"has). Items here are candidates for submission.\n\n")

    # --- header counts
    n_total = len(records)
    n_with_enrich = sum(1 for r in records if enrichments.get(r.freq_hz) is not None)
    n_new_system = sum(
        1 for r in records
        if (e := enrichments.get(r.freq_hz)) and not e.system_match and r.wacn is not None
    )
    n_new_site = sum(
        1 for r in records
        if (e := enrichments.get(r.freq_hz)) and e.system_match and not e.site_match
    )
    n_freq_mismatch = sum(
        1 for r in records
        if (e := enrichments.get(r.freq_hz)) and e.site_match and not e.cc_freq_in_db
    )
    n_neighbor_diff = sum(
        1 for r in records
        if (e := enrichments.get(r.freq_hz))
        and e.site_match
        and (e.neighbors_in_rr_not_decoded or e.neighbors_decoded_not_in_rr)
    )

    out.write("## Summary\n\n")
    out.write(f"- Records scanned: **{n_total}**\n")
    out.write(f"- Enriched against RR: **{n_with_enrich}**\n")
    out.write(f"- New systems candidates: **{n_new_system}**\n")
    out.write(f"- New sites candidates: **{n_new_site}**\n")
    out.write(f"- Frequency mismatches: **{n_freq_mismatch}**\n")
    out.write(f"- Sites with neighbor diffs: **{n_neighbor_diff}**\n\n")

    # --- per-band ppm
    band_offsets = summarize_band_offsets(records, enrichments)
    if band_offsets:
        out.write("## SDR frequency offsets observed (per band)\n\n")
        out.write("Use these to calibrate the SDR's `--ppm`. A consistent non-zero "
                  "value across multiple sites in one band suggests SDR clock error "
                  "rather than RR-database error.\n\n")
        out.write("| Band | Sites matched | Mean offset | Mean ppm | Median ppm |\n")
        out.write("|---|---:|---:|---:|---:|\n")
        for s in band_offsets:
            sign = "+" if s.mean_offset_hz >= 0 else ""
            out.write(f"| {s.band_name} | {s.n_samples} | "
                      f"{sign}{s.mean_offset_hz} Hz | "
                      f"{sign}{s.mean_ppm:.3f} | "
                      f"{s.median_ppm:+.3f} |\n")
        out.write("\n")

    # --- new systems
    _wrote_section(out, "## New systems\n\nSystems whose WACN+SYSID isn't in RR.\n\n",
                   lambda b: _write_new_systems(b, records, enrichments))

    # --- new sites
    _wrote_section(out, "## New sites\n\nSystem found in RR; this RFSS/Site isn't.\n\n",
                   lambda b: _write_new_sites(b, records, enrichments))

    # --- frequency mismatches
    _wrote_section(out, "## Frequency mismatches\n\n"
                        "Site is in RR but our decoded CC frequency doesn't match any "
                        "listed CC for that site. Could be a new CC, an SDR offset, or "
                        "a site reconfiguration RR hasn't picked up.\n\n",
                   lambda b: _write_freq_mismatches(b, records, enrichments))

    # --- neighbor diffs
    _wrote_section(out, "## Neighbor differences\n\n"
                        "Sites where the (RFSS, Site) IDs we decoded from "
                        "ADJ_STS_BCST diverge from the RR roster for the "
                        "system. Useful as a hint for admins to verify or "
                        "extend the database; the inverse direction (RR has "
                        "a site we didn't observe) is informational — "
                        "ADJ_STS_BCST advertises a configured subset, so "
                        "missing observations are expected when neighbors "
                        "aren't physically adjacent or were off-air during "
                        "the scan.\n\n",
                   lambda b: _write_neighbor_diffs(b, records, enrichments))

    if not any([n_new_system, n_new_site, n_freq_mismatch, n_neighbor_diff]):
        out.write("## (Nothing to submit)\n\n"
                  "All decoded systems matched RadioReference cleanly. No submissions needed.\n")

    return out.getvalue()


def render_file(records: list[SurveyRecord],
                enrichments: dict[int, EnrichmentResult],
                path: str) -> None:
    from pathlib import Path
    Path(path).write_text(render(records, enrichments), encoding="utf-8")


# ---------------------------------------------------------------------------
# Section writers
# ---------------------------------------------------------------------------


def _write_new_systems(out: StringIO, records: list[SurveyRecord],
                       enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or e.system_match or r.wacn is None:
            continue
        out.write(f"### WACN {_hex(r.wacn, 5)} / SYSID {_hex(r.sysid, 3)} "
                  f"(NAC {_hex(r.nac, 3)})\n\n")
        out.write(f"- Discovered on: {_fmt_freq_mhz(r.freq_hz)}\n")
        out.write(f"- RFSS / Site: {r.rfss_id} / {r.site_id}\n")
        if r.signal.rssi_dbfs_mean is not None:
            out.write(f"- Signal: RSSI {r.signal.rssi_dbfs_mean} dBFS, "
                      f"BER {r.signal.ber_pct_mean}%\n")
        if r.iden_up:
            out.write(f"- Band plan: {len(r.iden_up)} IDEN_UP entries\n")
        if r.neighbors:
            out.write(f"- Neighbors (also probably new):\n")
            for n in sorted(r.neighbors, key=lambda x: x.freq_hz):
                out.write(f"    - {_fmt_freq_mhz(n.freq_hz)}, "
                          f"RFSS {n.rfss_id}, Site {n.site_id}\n")
        out.write("\n")


def _write_new_sites(out: StringIO, records: list[SurveyRecord],
                     enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.system_match or e.site_match:
            continue
        out.write(f"### {e.rr_system_name}: RFSS {r.rfss_id} / Site {r.site_id}\n\n")
        out.write(f"- Control channel: {_fmt_freq_mhz(r.freq_hz)}\n")
        out.write(f"- NAC: {_hex(r.nac, 3)}\n")
        out.write(f"- WACN: {_hex(r.wacn, 5)}\n")
        if r.signal.rssi_dbfs_mean is not None:
            out.write(f"- Signal: RSSI {r.signal.rssi_dbfs_mean} dBFS\n")
        if r.iden_up:
            out.write(f"- Band plan ({len(r.iden_up)} entries):\n")
            for ie in r.iden_up:
                kind = f"TDMA x{ie.slots_per_carrier}" if ie.is_tdma else "FDMA"
                out.write(f"    - iden {ie.iden}: base {_fmt_freq_mhz(ie.base_freq_hz)}, "
                          f"step {ie.step_hz / 1e3:g} kHz, "
                          f"offset {ie.offset_hz / 1e6:+g} MHz [{kind}]\n")
        if r.neighbors:
            out.write(f"- Reported neighbors:\n")
            for n in sorted(r.neighbors, key=lambda x: x.freq_hz):
                out.write(f"    - {_fmt_freq_mhz(n.freq_hz)}, "
                          f"RFSS {n.rfss_id}, Site {n.site_id}\n")
        out.write("\n")


def _write_freq_mismatches(out: StringIO, records: list[SurveyRecord],
                           enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.site_match or e.cc_freq_in_db:
            continue
        if e.cc_freq_offset is None:
            continue
        out.write(f"### {e.rr_system_name}: RFSS {r.rfss_id} / Site {r.site_id}"
                  f"{' — ' + e.rr_site_description if e.rr_site_description else ''}\n\n")
        out.write(f"- **RR has:** {_fmt_freq_mhz(e.cc_freq_offset.expected_hz)}\n")
        out.write(f"- **We decoded:** {_fmt_freq_mhz(e.cc_freq_offset.decoded_hz)}\n")
        sign = "+" if e.cc_freq_offset.offset_hz >= 0 else ""
        out.write(f"- Offset: {sign}{e.cc_freq_offset.offset_hz} Hz "
                  f"({sign}{e.cc_freq_offset.ppm:.3f} ppm)\n")
        out.write(f"- WACN/SYSID/NAC: {_hex(r.wacn, 5)} / "
                  f"{_hex(r.sysid, 3)} / {_hex(r.nac, 3)}\n\n")


def _write_neighbor_diffs(out: StringIO, records: list[SurveyRecord],
                          enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.site_match:
            continue
        if not e.neighbors_in_rr_not_decoded and not e.neighbors_decoded_not_in_rr:
            continue
        out.write(f"### {e.rr_system_name}: RFSS {r.rfss_id} / Site {r.site_id}"
                  f"{' — ' + e.rr_site_description if e.rr_site_description else ''}\n\n")
        out.write(f"- Decoded CC: {_fmt_freq_mhz(r.freq_hz)}\n")
        if e.neighbors_decoded_not_in_rr:
            out.write(f"- **Neighbors we observed whose RFSS/Site aren't in "
                      f"RR's roster (admins: candidates to add):**\n")
            for n in e.neighbors_decoded_not_in_rr:
                freq = f", {_fmt_freq_mhz(n.freq_hz)}" if n.freq_hz else ""
                out.write(f"    - RFSS {n.rfss_id} / Site {n.site_id}{freq}\n")
        if e.neighbors_in_rr_not_decoded:
            out.write(f"- RR-roster sites we did not see in ADJ_STS_BCST "
                      f"(informational — may not actually neighbor this site):\n")
            for n in e.neighbors_in_rr_not_decoded:
                freq = f", {_fmt_freq_mhz(n.freq_hz)}" if n.freq_hz else ""
                desc = f" — {n.description}" if n.description else ""
                out.write(f"    - RFSS {n.rfss_id} / Site {n.site_id}{freq}{desc}\n")
        out.write("\n")
