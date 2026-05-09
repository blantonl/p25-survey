"""Markdown submission report for survey data not in RadioReference.

Run after the scan completes (and after enrichment). Walks the survey
records + their EnrichmentResults and produces a `survey-*-submissions.md`
file organized as:

  1. Summary header (counts, per-band ppm offset table)
  2. New systems (WACN/SYSID we found that RR has no record of)
  3. New sites (system in RR, but RFSS/Site not yet listed)
  4. New / corrected frequencies (site in RR, decoded freq not listed
     or off by more than 1 kHz)
  5. Site NAC mismatches (RR has no NAC listed, or the listed NAC
     disagrees with what we decoded — the case Russ Mason flagged)
  6. New-site neighbor candidates (sites we observed in ADJ_STS_BCST
     whose (RFSS, Site) ID isn't in RR's roster)
  7. Neighbor CC mismatches (advertised neighbor CC freq either
     missing from RR or not flagged as a control channel)
  8. Observed neighbor roster (per-site enumeration of ADJ_STS_BCST
     neighbors with their RR site descriptions for spot-checking)

Designed to be pasted into a forum thread or RR support ticket.
"""

from __future__ import annotations

from io import StringIO
from typing import Iterable

from p25_survey.enrich import (
    BandOffsetSummary,
    EnrichmentResult,
    recommend_ppm,
    site_nac_diff,
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
        if r.complete
        and (e := enrichments.get(r.freq_hz)) and not e.system_match and r.wacn is not None
    )
    n_new_site = sum(
        1 for r in records
        if r.complete
        and r.rfss_id is not None and r.site_id is not None
        and (e := enrichments.get(r.freq_hz))
        and e.system_match and not e.site_match and not e.ambiguous_site
    )
    n_freq_mismatch = sum(
        1 for r in records
        if r.complete
        and (e := enrichments.get(r.freq_hz)) and e.site_match and not e.cc_freq_in_db
    )
    n_site_nac_mismatch = sum(
        1 for r in records
        if r.complete
        and (e := enrichments.get(r.freq_hz)) and e.site_match
        and site_nac_diff(r.nac, e.rr_site_nac) is not None
    )
    n_neighbor_new = sum(
        1 for r in records
        if r.complete
        and (e := enrichments.get(r.freq_hz))
        and e.site_match and e.neighbors_decoded_not_in_rr
    )
    n_neighbor_cc_miss = sum(
        1 for r in records
        if r.complete
        and (e := enrichments.get(r.freq_hz))
        and e.site_match and e.neighbor_cc_mismatches
    )

    out.write("## Summary\n\n")
    out.write(f"- Records scanned: **{n_total}**\n")
    out.write(f"- Enriched against RR: **{n_with_enrich}**\n")
    out.write(f"- New systems candidates: **{n_new_system}**\n")
    out.write(f"- New sites candidates: **{n_new_site}**\n")
    out.write(f"- Frequency mismatches: **{n_freq_mismatch}**\n")
    out.write(f"- Site NAC mismatches: **{n_site_nac_mismatch}**\n")
    out.write(f"- Sites with new-site neighbor candidates: **{n_neighbor_new}**\n")
    out.write(f"- Sites with neighbor CC mismatches: **{n_neighbor_cc_miss}**\n\n")

    # --- ppm recommendation + per-band table
    band_offsets = summarize_band_offsets(records, enrichments)
    if band_offsets:
        rec = recommend_ppm(records, enrichments)
        out.write("## SDR clock calibration\n\n")
        if rec.consistency == "insufficient_data":
            out.write(f"Not enough matched sites ({rec.n_samples}) for a reliable "
                      f"`--ppm` recommendation; need at least 3.\n\n")
        else:
            out.write(f"**Recommended `--ppm {rec.median_ppm:+.2f}`** — median "
                      f"across {rec.n_samples} matched sites, IQR "
                      f"{rec.iqr_ppm:.2f} ppm, consistency: **{rec.consistency}**. "
                      f"Single-oscillator SDRs (RTL-SDR, Airspy, HackRF) take one "
                      f"global value; pass it as `--ppm <value>` on the next run.\n\n")
            if rec.cross_band_warning:
                out.write(f"> ⚠ {rec.cross_band_warning}\n\n")
        out.write("Per-band breakdown (diagnostic — a consistent non-zero value "
                  "across multiple sites in one band suggests SDR clock error rather "
                  "than RR-database error):\n\n")
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

    # --- site NAC mismatches
    _wrote_section(out, "## Site NAC mismatches\n\n"
                        "Site is in RR and the CC frequency matches, but the decoded "
                        "NAC differs from RR's value (or RR has no NAC listed for the "
                        "site — many systems carry a \"PLEASE SUBMIT\" note about "
                        "missing NACs). Each entry below is a candidate for a NAC "
                        "submission or correction.\n\n",
                   lambda b: _write_site_nac_mismatches(b, records, enrichments))

    # --- new-site neighbor candidates
    _wrote_section(out, "## New-site neighbor candidates\n\n"
                        "ADJ_STS_BCST neighbors whose (RFSS, Site) ID doesn't "
                        "resolve to any site in the RR roster for this system. "
                        "Strong candidates for admins to add.\n\n",
                   lambda b: _write_new_site_candidates(b, records, enrichments))

    # --- neighbor CC mismatches
    _wrote_section(out, "## Neighbor control-channel mismatches\n\n"
                        "Each row is an ADJ_STS_BCST neighbor whose advertised CC "
                        "frequency either isn't in the neighbor site's RR frequency "
                        "list at all, or is listed but not flagged as a control "
                        "channel (use code \"d\"/\"a\"). Both surface RR data gaps "
                        "rather than site-roster gaps.\n\n",
                   lambda b: _write_neighbor_cc_mismatches(b, records, enrichments))

    # --- observed neighbor roster (informational)
    _wrote_section(out, "## Observed neighbor roster\n\n"
                        "ADJ_STS_BCST neighbors actually observed at each site, "
                        "annotated with their RR site description/location. "
                        "Useful for admins to spot-check whether per-neighbor "
                        "site text is current.\n\n",
                   lambda b: _write_observed_neighbors(b, records, enrichments))

    if not any([n_new_system, n_new_site, n_freq_mismatch,
                n_site_nac_mismatch, n_neighbor_new, n_neighbor_cc_miss]):
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
        if e is None or e.system_match or r.wacn is None or not r.complete:
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
        if e is None or not e.system_match or e.site_match or e.ambiguous_site:
            continue
        if not r.complete or r.rfss_id is None or r.site_id is None:
            continue
        out.write(f"### {e.rr_system_name}: RFSS {r.rfss_id} / Site {r.site_id}\n\n")
        out.write(f"- Control channel: {_fmt_freq_mhz(r.freq_hz)}\n")
        out.write(f"- NAC: {_hex(r.nac, 3)}\n")
        out.write(f"- WACN: {_hex(r.wacn, 5)}\n")
        out.write(f"- SYSID: {_hex(r.sysid, 3)}\n")
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
        if e.cc_freq_offset is None or not r.complete:
            continue
        out.write(f"### {e.rr_system_name}: RFSS {r.rfss_id} / Site {r.site_id}"
                  f"{' — ' + e.rr_site_description if e.rr_site_description else ''}\n\n")
        out.write(f"- **RR has:** {_fmt_freq_mhz(e.cc_freq_offset.expected_hz)}\n")
        out.write(f"- **We decoded:** {_fmt_freq_mhz(e.cc_freq_offset.decoded_hz)}\n")
        sign = "+" if e.cc_freq_offset.offset_hz >= 0 else ""
        out.write(f"- Offset: {sign}{e.cc_freq_offset.offset_hz} Hz "
                  f"({sign}{e.cc_freq_offset.ppm:.3f} ppm)\n")
        decoded_nac = _hex(r.nac, 3)
        nac_line = f"- Decoded WACN/SYSID/NAC: {_hex(r.wacn, 5)} / {_hex(r.sysid, 3)} / {decoded_nac}"
        if e.rr_site_nac and e.rr_site_nac.upper().lstrip("0") != decoded_nac.lstrip("0"):
            nac_line += f" (RR site NAC: {e.rr_site_nac})"
        out.write(nac_line + "\n\n")


def _site_header(r: SurveyRecord, e: EnrichmentResult) -> str:
    desc = f" — {e.rr_site_description}" if e.rr_site_description else ""
    return f"### {e.rr_system_name}: RFSS {r.rfss_id} / Site {r.site_id}{desc}\n\n"


def _write_site_nac_mismatches(out: StringIO, records: list[SurveyRecord],
                               enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.site_match or not r.complete:
            continue
        kind = site_nac_diff(r.nac, e.rr_site_nac)
        if kind is None:
            continue
        out.write(_site_header(r, e))
        out.write(f"- Control channel: {_fmt_freq_mhz(r.freq_hz)}\n")
        out.write(f"- Decoded NAC: **{_hex(r.nac, 3)}**\n")
        if kind == "missing":
            out.write(f"- RR site NAC: *(not listed)* — submit the decoded NAC\n")
        else:
            out.write(f"- RR site NAC: **{e.rr_site_nac}** — submit a correction\n")
        out.write("\n")


def _write_new_site_candidates(out: StringIO, records: list[SurveyRecord],
                               enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.site_match or not e.neighbors_decoded_not_in_rr:
            continue
        if not r.complete:
            continue
        out.write(_site_header(r, e))
        out.write(f"- Decoded CC: {_fmt_freq_mhz(r.freq_hz)}\n")
        out.write(f"- New-site candidates from ADJ_STS_BCST:\n")
        for n in e.neighbors_decoded_not_in_rr:
            freq = f", {_fmt_freq_mhz(n.freq_hz)}" if n.freq_hz else ""
            out.write(f"    - RFSS {n.rfss_id} / Site {n.site_id}{freq}\n")
        out.write("\n")


def _write_neighbor_cc_mismatches(out: StringIO, records: list[SurveyRecord],
                                  enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.site_match or not e.neighbor_cc_mismatches:
            continue
        if not r.complete:
            continue
        out.write(_site_header(r, e))
        out.write(f"- Decoded CC: {_fmt_freq_mhz(r.freq_hz)}\n")
        for m in e.neighbor_cc_mismatches:
            who = f"RFSS {m.rfss_id} / Site {m.site_id}"
            if m.rr_site_description:
                who += f" — {m.rr_site_description}"
            advertised = _fmt_freq_mhz(m.advertised_cc_hz)
            if m.kind == "missing_from_rr":
                out.write(f"    - {who}: advertised CC **{advertised}** is "
                          f"not in the site's RR frequency list\n")
            elif m.kind == "not_marked_control":
                use = m.rr_use_code or "(blank)"
                out.write(f"    - {who}: advertised CC **{advertised}** is "
                          f"listed but `use=\"{use}\"` (should be \"d\" or \"a\")\n")
        out.write("\n")


def _write_observed_neighbors(out: StringIO, records: list[SurveyRecord],
                              enrichments: dict[int, EnrichmentResult]) -> None:
    for r in sorted(records, key=lambda x: x.freq_hz):
        e = enrichments.get(r.freq_hz)
        if e is None or not e.site_match or not e.observed_neighbors:
            continue
        out.write(_site_header(r, e))
        for n in e.observed_neighbors:
            freq = _fmt_freq_mhz(n.freq_hz) if n.freq_hz else "(no CC)"
            id_part = f"RFSS {n.rfss_id} / Site {n.site_id}"
            if n.in_rr:
                bits = [b for b in (n.description, n.location, n.county) if b]
                desc = f" — {' / '.join(bits)}" if bits else ""
                out.write(f"- {id_part}, {freq}{desc}\n")
            else:
                out.write(f"- {id_part}, {freq} — *not in RR roster (new-site candidate)*\n")
        out.write("\n")
