"""Plain-text survey report.

Generated post-scan from the NDJSON survey file. Designed to be readable
in a terminal or pasted into a forum thread.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from p25_survey.enrich import site_nac_diff
from p25_survey.survey import SurveyRecord, read_survey


def _fmt_hex(value: int | None, width: int) -> str:
    if value is None:
        return "—"
    return format(value, f"0{width}X")


def _fmt_freq_mhz(freq_hz: int) -> str:
    # 10 Hz precision via integer math — preserves the 6.25 kHz raster exactly
    # and avoids round-half-to-even ambiguity that bites %f at 5 decimals.
    rounded = (freq_hz + 5) // 10 * 10
    mhz_int = rounded // 1_000_000
    mhz_frac = (rounded % 1_000_000) // 10
    return f"{mhz_int}.{mhz_frac:05d} MHz"


def _fmt_signal(record: SurveyRecord) -> str:
    s = record.signal
    parts = []
    if s.rssi_dbfs_mean is not None:
        parts.append(f"RSSI {s.rssi_dbfs_mean:.1f} dBFS")
    if s.rssi_dbfs_peak is not None:
        parts.append(f"peak {s.rssi_dbfs_peak:.1f} dBFS")
    if s.ber_pct_mean is not None:
        parts.append(f"BER {s.ber_pct_mean:.2f}%")
    if s.decode_rate_pct is not None:
        parts.append(f"decode {s.decode_rate_pct:.1f}%")
    return ", ".join(parts) if parts else "—"


def _fmt_rr(record: SurveyRecord) -> str:
    """One-line RR match summary, or empty string if no enrichment."""
    rr = record.rr
    if not rr:
        return ""
    if not rr.get("system_match"):
        if record.wacn is not None:
            return "RR: NEW SYSTEM (not in database)"
        return ""
    sys_name = rr.get("rr_system_name") or "(unknown)"
    if not rr.get("site_match"):
        return f"RR: {sys_name} — Unidentified RFSS/Site"
    parts = [f"RR: {sys_name}"]
    if rr.get("rr_site_description"):
        parts.append(rr["rr_site_description"])
    if rr.get("rr_site_county"):
        parts.append(rr["rr_site_county"])
    cc = rr.get("cc_freq_offset")
    if cc:
        offset_hz = cc.get("offset_hz", 0)
        ppm = cc.get("ppm", 0.0)
        if rr.get("cc_freq_in_db"):
            sign = "+" if offset_hz >= 0 else ""
            parts.append(f"freq match (offset {sign}{offset_hz} Hz, {sign}{ppm:.3f} ppm)")
        else:
            sign = "+" if offset_hz >= 0 else ""
            parts.append(f"FREQ MISMATCH: RR={cc['expected_hz'] / 1e6:.5f} MHz, "
                         f"offset {sign}{offset_hz} Hz")
    nac_diff = site_nac_diff(record.nac, rr.get("rr_site_nac"))
    if nac_diff == "differs":
        parts.append(f"NAC MISMATCH: RR={rr['rr_site_nac']}, "
                     f"decoded {_fmt_hex(record.nac, 3)}")
    elif nac_diff == "missing":
        parts.append(f"NAC MISMATCH: RR has none listed, "
                     f"decoded {_fmt_hex(record.nac, 3)}")
    n_sec_cc = len(rr.get("secondary_cc_mismatches") or [])
    if n_sec_cc:
        parts.append(f"{n_sec_cc} secondary CC mismatch(es)")
    n_extra = len(rr.get("neighbors_decoded_not_in_rr") or [])
    n_cc_miss = len(rr.get("neighbor_cc_mismatches") or [])
    if n_extra:
        parts.append(f"{n_extra} new-site candidate neighbor(s)")
    if n_cc_miss:
        parts.append(f"{n_cc_miss} neighbor CC mismatch(es)")
    return "; ".join(parts)


def _fmt_neighbor_site(ref: dict | None) -> str:
    """RR site description for a neighbor row, or a marker / empty string.

    `ref` is one serialized NeighborRef dict from the enrichment's
    observed_neighbors list. Returns "" when no enrichment matched this
    neighbor (no site_match, or the record wasn't enriched).
    """
    if not ref:
        return ""
    if not ref.get("in_rr"):
        return "(not in RR roster)"
    bits = [b for b in (ref.get("description"), ref.get("location"),
                        ref.get("county")) if b]
    return " / ".join(bits)


def _fmt_neighbor_flags(n) -> str:
    """Trailing status-flag tag for a neighbor row. Only flags worth a glance:
    a conventional channel, a site in failure, a stale/last-known entry, or a
    neighbor running failsoft. Empty when nothing notable (or nothing decoded)."""
    tags = []
    if n.conventional:
        tags.append("conventional")
    if n.site_failure:
        tags.append("FAILURE")
    if n.network_active is False:
        tags.append("failsoft")
    if n.valid is False:
        tags.append("stale")
    return f"  [{', '.join(tags)}]" if tags else ""


def render_record(record: SurveyRecord, out: StringIO) -> None:
    """Write one record's section."""
    header = f"{_fmt_freq_mhz(record.freq_hz)}  WACN {_fmt_hex(record.wacn, 5)}  " \
             f"SYS {_fmt_hex(record.sysid, 3)}  NAC {_fmt_hex(record.nac, 3)}"
    out.write(header + "\n")
    out.write("-" * len(header) + "\n")
    failsoft = ("  [FAILSOFT — no active RFSS network connection]"
                if record.site_network_active is False else "")
    out.write(f"  RFSS / Site:  {record.rfss_id if record.rfss_id is not None else '—'}"
              f" / {record.site_id if record.site_id is not None else '—'}{failsoft}\n")
    out.write(f"  Detected at:  {record.ts}\n")
    out.write(f"  Dwell:        {record.dwell_ms} ms"
              f"  ({'complete' if record.complete else 'partial'})\n")
    out.write(f"  Signal:       {_fmt_signal(record)}\n")
    rr_line = _fmt_rr(record)
    if rr_line:
        out.write(f"  {rr_line}\n")
    if record.sdr_driver:
        gain_str = f"{record.sdr_gain_db} dB" if record.sdr_gain_db is not None else "default"
        out.write(f"  SDR:          {record.sdr_driver}, gain {gain_str}, ppm {record.sdr_ppm}\n")

    if record.iden_up:
        out.write(f"  Band plan:    {len(record.iden_up)} entr"
                  f"{'y' if len(record.iden_up) == 1 else 'ies'}\n")
        for ie in record.iden_up:
            kind = f"TDMA x{ie.slots_per_carrier}" if ie.is_tdma else "FDMA"
            if ie.bandwidth_hz:
                kind += f", {ie.bandwidth_hz / 1e3:g} kHz BW"
            sign = "+" if ie.offset_hz >= 0 else "-"
            out.write(
                f"    iden {ie.iden}: base {_fmt_freq_mhz(ie.base_freq_hz)}, "
                f"step {ie.step_hz / 1e3:g} kHz, "
                f"offset {sign}{abs(ie.offset_hz) / 1_000_000:g} MHz "
                f"[{kind}]\n"
            )

    if record.secondary_cc:
        rr = record.rr or {}
        sec_miss = {m["freq_hz"]: m
                    for m in (rr.get("secondary_cc_mismatches") or [])}
        parts = []
        for f in record.secondary_cc:
            m = sec_miss.get(f)
            if m is None:
                parts.append(_fmt_freq_mhz(f))
            elif m["kind"] == "missing_from_rr":
                parts.append(f"{_fmt_freq_mhz(f)} [not in RR frequency list]")
            else:
                use = m.get("rr_use_code") or "(blank)"
                parts.append(f"{_fmt_freq_mhz(f)} [in RR, use=\"{use}\" not control]")
        out.write(f"  Secondary CC: {', '.join(parts)}\n")

    if record.neighbors:
        rr = record.rr or {}
        nbr_rr = {(ref.get("rfss_id"), ref.get("site_id")): ref
                  for ref in (rr.get("observed_neighbors") or [])}
        out.write(f"  Neighbors:    {len(record.neighbors)}\n")
        out.write(f"    {'Freq':<14} {'RFSS':>4} {'Site':>4} {'WACN':>6} "
                  f"{'SYS':>4}  RR site\n")
        for n in sorted(record.neighbors, key=lambda x: x.freq_hz):
            site_txt = _fmt_neighbor_site(nbr_rr.get((n.rfss_id, n.site_id)))
            row = (
                f"    {_fmt_freq_mhz(n.freq_hz):<14} "
                f"{n.rfss_id:>4} {n.site_id:>4} "
                f"{_fmt_hex(n.wacn, 5):>6} {_fmt_hex(n.sysid, 3):>4}  {site_txt}"
                f"{_fmt_neighbor_flags(n)}"
            )
            out.write(row.rstrip() + "\n")
    else:
        out.write("  Neighbors:    (none reported)\n")

    if record.notes:
        out.write("  Notes:\n")
        for note in record.notes:
            out.write(f"    - {note}\n")
    out.write("\n")


def render(records: list[SurveyRecord]) -> str:
    """Render a list of records as one report. Returns the full text.

    Only records with at least some P25 identity (WACN, RFSS, or NAC seen)
    are detailed. Pure no-cc noise candidates appear in a one-line tally
    at the bottom so the report stays focused on the systems found.
    """
    is_cc = lambda r: r.wacn is not None or r.rfss_id is not None or r.nac is not None
    cc_records = [r for r in records if is_cc(r)]
    no_cc_records = [r for r in records if not is_cc(r)]
    complete_count = sum(1 for r in cc_records if r.complete)

    buf = StringIO()
    buf.write("=" * 78 + "\n")
    buf.write(f"P25 SURVEY REPORT — {len(cc_records)} control channels detected"
              f" ({len(records)} candidates scanned)\n")
    buf.write("=" * 78 + "\n\n")

    if not cc_records:
        buf.write("(no control channels detected)\n")
        return buf.getvalue()

    buf.write(f"Summary: {complete_count} complete, "
              f"{len(cc_records) - complete_count} partial,"
              f" {len(no_cc_records)} non-CC candidates.\n\n")

    for r in sorted(cc_records, key=lambda x: x.freq_hz):
        render_record(r, buf)

    if no_cc_records:
        buf.write("Non-CC candidates (energy-scan peaks that produced no P25 broadcasts):\n")
        for r in sorted(no_cc_records, key=lambda x: x.freq_hz):
            buf.write(f"  {_fmt_freq_mhz(r.freq_hz)}\n")
        buf.write("\n")

    return buf.getvalue()


def render_file(json_path: str | Path, txt_path: str | Path) -> None:
    """Read NDJSON survey at json_path and write text report to txt_path."""
    records = read_survey(json_path)
    Path(txt_path).write_text(render(records), encoding="utf-8")
