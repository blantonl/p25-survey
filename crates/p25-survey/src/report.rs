//! The reports, from survey records (ours or the Python version's):
//!
//! - the text report: every control channel with its identity, band plan,
//!   secondary control channels and neighbours (and RR's view, if looked
//!   up); copies, other P25 and the rest after;
//! - the submissions report (Markdown, with RR): what the survey found that
//!   RadioReference doesn't have, or has differently, to paste into a forum
//!   thread or a ticket. Sections as the Python version's.
//!
//! Both start with the radio's clock: its frequency error, measured on the
//! control channels (base stations are far more accurate than any SDR).

use std::collections::BTreeMap;
use std::fmt::Write;

use crate::enrich::{Enrichment, site_nac_diff};
use crate::rows::{Row, band_name, mhz};

/// Fewer matched channels than this and no ppm is advised.
const PPM_MIN: usize = 3;

pub struct Clock {
    /// "measured" (carrier offsets on the air) or "RadioReference" (against
    /// RR's listed frequencies: the Python version's records).
    pub source: &'static str,
    pub n: usize,
    pub median_ppm: f64,
    pub iqr_ppm: f64,
    /// "high", "medium" or "low".
    pub consistency: &'static str,
    /// Per band: (name, n, median ppm, mean offset Hz).
    pub bands: Vec<(&'static str, usize, f64, f64)>,
    pub warning: Option<String>,
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    match sorted.len() {
        0 => 0.0,
        1 => sorted[0],
        n => {
            let k = (n - 1) as f64 * p;
            let lo = k as usize;
            let hi = (lo + 1).min(n - 1);
            sorted[lo] + (sorted[hi] - sorted[lo]) * (k - lo as f64)
        }
    }
}

/// The radio's frequency error. Our records carry each control channel's
/// carrier offset as measured; the Python version's only RR's offsets.
pub fn clock(rows: &[Row]) -> Option<Clock> {
    let measured: Vec<(u64, f64)> = rows
        .iter()
        .filter(|r| r.is_control() && r.signal.decode_rate_pct.unwrap_or(0.0) >= 50.0)
        // The radio's whole error: the correction it had (--ppm) and what
        // was still measured, so the advice doesn't depend on the correction.
        .filter_map(|r| Some((r.freq_hz, r.carrier_offset_hz? + r.sdr_ppm * 1e-6 * r.freq_hz as f64)))
        .collect();
    let (source, samples) = if !measured.is_empty() {
        ("measured", measured)
    } else {
        ("RadioReference", rows.iter().filter_map(|r| r.rr.as_ref()?.cc_freq_offset.as_ref().map(|o| (r.freq_hz, o.offset_hz as f64))).collect())
    };
    if samples.len() < PPM_MIN {
        return None;
    }
    let ppm = |f: u64, hz: f64| hz / f as f64 * 1e6;
    let mut all: Vec<f64> = samples.iter().map(|&(f, hz)| ppm(f, hz)).collect();
    all.sort_by(f64::total_cmp);
    let median = percentile(&all, 0.5);
    let iqr = percentile(&all, 0.75) - percentile(&all, 0.25);
    let mut by_band: BTreeMap<&'static str, Vec<(f64, f64)>> = BTreeMap::new();
    for &(f, hz) in &samples {
        by_band.entry(band_name(f)).or_default().push((ppm(f, hz), hz));
    }
    let bands: Vec<(&'static str, usize, f64, f64)> = by_band
        .into_iter()
        .map(|(b, v)| {
            let mut p: Vec<f64> = v.iter().map(|x| x.0).collect();
            p.sort_by(f64::total_cmp);
            (b, v.len(), percentile(&p, 0.5), v.iter().map(|x| x.1).sum::<f64>() / v.len() as f64)
        })
        .collect();
    let multi: Vec<_> = bands.iter().filter(|b| b.1 >= 2).collect();
    let warning = (multi.len() >= 2)
        .then(|| {
            let hi = multi.iter().max_by(|a, b| a.2.total_cmp(&b.2)).unwrap();
            let lo = multi.iter().min_by(|a, b| a.2.total_cmp(&b.2)).unwrap();
            (hi.2 - lo.2 > 0.5).then(|| {
                format!("per-band medians differ by {:.2} ppm ({}: {:+.2}, {}: {:+.2}); unusual for one oscillator", hi.2 - lo.2, lo.0, lo.2, hi.0, hi.2)
            })
        })
        .flatten();
    Some(Clock {
        source,
        n: samples.len(),
        median_ppm: median,
        iqr_ppm: iqr,
        consistency: if iqr < 0.2 {
            "high"
        } else if iqr < 0.5 {
            "medium"
        } else {
            "low"
        },
        bands,
        warning,
    })
}

fn hex(v: &Option<String>) -> &str {
    v.as_deref().unwrap_or("—")
}

fn opt<T: ToString>(v: Option<T>) -> String {
    v.map_or("—".into(), |v| v.to_string())
}

/// One line on what RR made of a record.
fn rr_line(r: &Row) -> Option<String> {
    let e = r.rr.as_ref()?;
    if !e.system_match {
        return r.wacn.is_some().then(|| "RR: NEW SYSTEM (not in database)".to_string());
    }
    let name = e.rr_system_name.clone().unwrap_or_else(|| "(unknown)".into());
    if !e.site_match {
        return Some(format!("RR: {name} — {}", if e.ambiguous_site { "site ambiguous (see notes)" } else { "RFSS/Site not in RR" }));
    }
    let mut parts = vec![format!("RR: {name}")];
    parts.extend(e.rr_site_description.clone());
    parts.extend(e.rr_site_county.clone());
    if let Some(o) = &e.cc_freq_offset {
        if e.cc_freq_in_db {
            parts.push(format!("frequency listed (offset {:+} Hz)", o.offset_hz));
        } else {
            parts.push(format!("FREQUENCY NOT LISTED: nearest RR control channel {}", mhz(o.expected_hz as u64)));
        }
    }
    match site_nac_diff(r.nac.as_deref(), e.rr_site_nac.as_deref()) {
        Some("missing") => parts.push("RR has no NAC".into()),
        Some(_) => parts.push(format!("NAC differs (RR {})", e.rr_site_nac.clone().unwrap_or_default())),
        None => {}
    }
    if !e.neighbors_decoded_not_in_rr.is_empty() {
        parts.push(format!("{} neighbour(s) not in RR", e.neighbors_decoded_not_in_rr.len()));
    }
    if !e.neighbor_cc_mismatches.is_empty() {
        parts.push(format!("{} neighbour control channel(s) not as RR has them", e.neighbor_cc_mismatches.len()));
    }
    Some(parts.join("; "))
}

fn write_clock(out: &mut String, c: &Clock, markdown: bool) {
    let how = if c.source == "measured" { "measured on the control channels' carriers" } else { "against RadioReference's listed frequencies" };
    if markdown {
        let _ = writeln!(out, "## SDR clock calibration\n");
        let _ = writeln!(
            out,
            "**The radio reads {:+.2} ppm** ({how}): median over {} control channels, IQR {:.2} ppm, consistency **{}**. Single-oscillator SDRs take one value for every band.\n",
            c.median_ppm, c.n, c.iqr_ppm, c.consistency
        );
        if let Some(w) = &c.warning {
            let _ = writeln!(out, "> ⚠ {w}\n");
        }
        let _ = writeln!(out, "| Band | Channels | Mean offset | Median ppm |\n|---|---:|---:|---:|");
        for (b, n, p, hz) in &c.bands {
            let _ = writeln!(out, "| {b} | {n} | {hz:+.0} Hz | {p:+.3} |");
        }
        out.push('\n');
    } else {
        let _ = writeln!(out, "Radio clock: {:+.2} ppm ({how}; median of {}, IQR {:.2}, consistency {})", c.median_ppm, c.n, c.iqr_ppm, c.consistency);
        for (b, n, p, hz) in &c.bands {
            let _ = writeln!(out, "  {b}: {p:+.3} ppm ({hz:+.0} Hz mean, {n} channel(s))");
        }
        if let Some(w) = &c.warning {
            let _ = writeln!(out, "  warning: {w}");
        }
        out.push('\n');
    }
}

fn write_record(out: &mut String, r: &Row) {
    let head = format!("{}  WACN {}  SYS {}  NAC {}", mhz(r.freq_hz), hex(&r.wacn), hex(&r.sysid), hex(&r.nac));
    let _ = writeln!(out, "{head}\n{}", "-".repeat(head.chars().count()));
    let _ = writeln!(out, "  RFSS / Site:  {} / {}", opt(r.rfss_id), opt(r.site_id));
    let _ = writeln!(out, "  Detected at:  {}", r.ts);
    let _ = writeln!(out, "  Dwell:        {} ms  ({})", r.dwell_ms, if r.complete { "complete" } else { "partial" });
    let mut signal = Vec::new();
    if let Some(l) = r.level_db {
        signal.push(format!("{l:.1} dB over the floor"));
    }
    if let Some(v) = r.signal.rssi_dbfs_mean {
        signal.push(format!("RSSI {v:.1} dBFS"));
    }
    if let Some(v) = r.signal.decode_rate_pct {
        signal.push(format!("decode {v:.1}%"));
    }
    if let Some(o) = r.carrier_offset_hz {
        signal.push(format!("carrier {o:+.0} Hz ({:+.2} ppm)", o / r.freq_hz as f64 * 1e6));
    }
    let _ = writeln!(out, "  Signal:       {}", if signal.is_empty() { "—".into() } else { signal.join(", ") });
    if let Some(l) = rr_line(r) {
        let _ = writeln!(out, "  {l}");
    }
    if !r.sdr_driver.is_empty() {
        let _ = writeln!(out, "  SDR:          {}, gain {}, ppm {}", r.sdr_driver, r.sdr_gain_db.map_or("default".into(), |g| g.to_string()), r.sdr_ppm);
    }
    if !r.iden_up.is_empty() {
        let _ = writeln!(out, "  Band plan:    {} entr{}", r.iden_up.len(), if r.iden_up.len() == 1 { "y" } else { "ies" });
        for i in &r.iden_up {
            let kind = if i.is_tdma { format!("TDMA x{}", i.slots_per_carrier) } else { "FDMA".into() };
            let _ = writeln!(
                out,
                "    iden {}: base {}, step {} kHz, offset {:+} MHz [{kind}]",
                i.iden,
                mhz(i.base_freq_hz),
                i.step_hz as f64 / 1e3,
                i.offset_hz as f64 / 1e6
            );
        }
    }
    if !r.secondary_cc.is_empty() {
        let _ = writeln!(out, "  Secondary CC: {}", r.secondary_cc.iter().map(|&f| mhz(f)).collect::<Vec<_>>().join(", "));
    }
    if r.neighbors.is_empty() {
        let _ = writeln!(out, "  Neighbors:    (none reported)");
    } else {
        let _ = writeln!(out, "  Neighbors:    {}", r.neighbors.len());
        let _ = writeln!(out, "    {:<14} {:>4} {:>4} {:>6} {:>4}  RR site", "Freq", "RFSS", "Site", "WACN", "SYS");
        let mut ns: Vec<_> = r.neighbors.iter().collect();
        ns.sort_by_key(|n| n.freq_hz);
        for n in ns {
            let rr = r.rr.as_ref().and_then(|e| e.observed_neighbors.iter().find(|o| o.rfss_id == n.rfss_id && o.site_id == n.site_id)).map(|o| {
                if o.in_rr {
                    [o.description.clone(), o.location.clone(), o.county.clone()].into_iter().flatten().collect::<Vec<_>>().join(" / ")
                } else {
                    "not in RR".into()
                }
            });
            let _ = writeln!(
                out,
                "    {:<14} {:>4} {:>4} {:>6} {:>4}  {}",
                mhz(n.freq_hz),
                n.rfss_id,
                n.site_id,
                hex(&n.wacn),
                hex(&n.sysid),
                rr.unwrap_or_default()
            );
        }
    }
    let notes: Vec<&String> = r.notes.iter().chain(r.rr.iter().flat_map(|e| e.notes.iter())).collect();
    if !notes.is_empty() {
        let _ = writeln!(out, "  Notes:");
        for n in notes {
            let _ = writeln!(out, "    - {n}");
        }
    }
    out.push('\n');
}

/// The text report.
pub fn text(rows: &[Row]) -> String {
    let mut sorted: Vec<&Row> = rows.iter().collect();
    sorted.sort_by_key(|r| r.freq_hz);
    let ccs: Vec<&Row> = sorted.iter().copied().filter(|r| r.is_control()).collect();
    let copies: Vec<&Row> = sorted.iter().copied().filter(|r| r.is_copy()).collect();
    let other: Vec<&Row> = sorted.iter().copied().filter(|r| r.is_other_p25()).collect();
    let rest: Vec<&Row> = sorted.iter().copied().filter(|r| !r.is_control() && !r.is_copy() && !r.is_other_p25()).collect();
    let mut out = String::new();
    let rule = "=".repeat(78);
    let _ = writeln!(out, "{rule}\nP25 SURVEY REPORT — {} control channels ({} candidates)\n{rule}\n", ccs.len(), rows.len());
    let complete = ccs.iter().filter(|r| r.complete).count();
    let _ = writeln!(
        out,
        "Summary: {complete} complete, {} partial; {} copies; {} other P25; {} not P25.\n",
        ccs.len() - complete,
        copies.len(),
        other.len(),
        rest.len()
    );
    if let Some(c) = clock(rows) {
        write_clock(&mut out, &c, false);
    }
    for r in &ccs {
        write_record(&mut out, r);
    }
    if !copies.is_empty() {
        let _ = writeln!(out, "Copies (the radio's image or intermodulation of a stronger control channel; not sites of their own):");
        for r in &copies {
            let _ = writeln!(
                out,
                "  {}  copy of {}  (NAC {}, RFSS/Site {} / {})",
                mhz(r.freq_hz),
                r.copy_of_hz.map_or("?".into(), mhz),
                hex(&r.nac),
                opt(r.rfss_id),
                opt(r.site_id)
            );
        }
        out.push('\n');
    }
    if !other.is_empty() {
        let _ = writeln!(out, "Other P25 (voice, data or conventional; no control channel):");
        for r in &other {
            let _ = writeln!(out, "  {}  NAC {}", mhz(r.freq_hz), hex(&r.nac));
        }
        out.push('\n');
    }
    if !rest.is_empty() {
        let _ = writeln!(out, "Not P25 (carriers that stayed on, but no P25 frame):");
        for r in &rest {
            let _ = writeln!(out, "  {}", mhz(r.freq_hz));
        }
        out.push('\n');
    }
    out
}

fn e(r: &Row) -> Option<&Enrichment> {
    r.rr.as_ref()
}

fn site_header(r: &Row, e: &Enrichment) -> String {
    let desc = e.rr_site_description.as_ref().map_or(String::new(), |d| format!(" — {d}"));
    format!("### {}: RFSS {} / Site {}{desc}\n\n", e.rr_system_name.clone().unwrap_or_default(), opt(r.rfss_id), opt(r.site_id))
}

/// A section, written only if it has entries.
fn section(out: &mut String, header: &str, body: String) -> bool {
    if body.trim().is_empty() {
        return false;
    }
    out.push_str(header);
    out.push_str(&body);
    true
}

/// The submissions report.
pub fn submissions(rows: &[Row]) -> String {
    let mut rs: Vec<&Row> = rows.iter().filter(|r| r.complete).collect();
    rs.sort_by_key(|r| r.freq_hz);
    let mut out = String::new();
    out.push_str("# RadioReference submission report\n\nThis survey compared the P25 control channels it decoded with the RadioReference database. Each section lists what it found that isn't in RR, or differs from what RR has: candidates for submission.\n\n");
    let count = |f: &dyn Fn(&Row, &Enrichment) -> bool| rs.iter().filter(|r| e(r).is_some_and(|en| f(r, en))).count();
    let new_systems = count(&|r, e| !e.system_match && r.wacn.is_some());
    let new_sites = count(&|r, e| e.system_match && !e.site_match && !e.ambiguous_site && r.rfss_id.is_some() && r.site_id.is_some());
    let freq = count(&|_, e| e.site_match && !e.cc_freq_in_db && e.cc_freq_offset.is_some());
    let nac = count(&|r, e| e.site_match && site_nac_diff(r.nac.as_deref(), e.rr_site_nac.as_deref()).is_some());
    let nbr_new = count(&|_, e| e.site_match && !e.neighbors_decoded_not_in_rr.is_empty());
    let nbr_cc = count(&|_, e| e.site_match && !e.neighbor_cc_mismatches.is_empty());
    let _ = writeln!(
        out,
        "## Summary\n\n- Records scanned: **{}**\n- Looked up in RR: **{}**\n- New system candidates: **{new_systems}**\n- New site candidates: **{new_sites}**\n- Frequency mismatches: **{freq}**\n- Site NAC mismatches: **{nac}**\n- Sites with new-site neighbor candidates: **{nbr_new}**\n- Sites with neighbor CC mismatches: **{nbr_cc}**\n",
        rows.len(),
        rows.iter().filter(|r| r.rr.is_some()).count()
    );
    if let Some(c) = clock(rows) {
        write_clock(&mut out, &c, true);
    }

    let mut b = String::new();
    for r in &rs {
        let Some(en) = e(r) else { continue };
        if en.system_match || r.wacn.is_none() {
            continue;
        }
        let _ = writeln!(b, "### WACN {} / SYSID {} (NAC {})\n", hex(&r.wacn), hex(&r.sysid), hex(&r.nac));
        let _ = writeln!(b, "- Discovered on: {}\n- RFSS / Site: {} / {}", mhz(r.freq_hz), opt(r.rfss_id), opt(r.site_id));
        if !r.iden_up.is_empty() {
            let _ = writeln!(b, "- Band plan: {} IDEN_UP entries", r.iden_up.len());
        }
        if !r.neighbors.is_empty() {
            let _ = writeln!(b, "- Neighbors (also probably new):");
            for n in &r.neighbors {
                let _ = writeln!(b, "    - {}, RFSS {}, Site {}", mhz(n.freq_hz), n.rfss_id, n.site_id);
            }
        }
        b.push('\n');
    }
    section(&mut out, "## New systems\n\nSystems whose WACN+SYSID isn't in RR.\n\n", b);

    let mut b = String::new();
    for r in &rs {
        let Some(en) = e(r) else { continue };
        if !en.system_match || en.site_match || en.ambiguous_site || r.rfss_id.is_none() || r.site_id.is_none() {
            continue;
        }
        let _ = writeln!(b, "### {}: RFSS {} / Site {}\n", en.rr_system_name.clone().unwrap_or_default(), opt(r.rfss_id), opt(r.site_id));
        let _ = writeln!(b, "- Control channel: {}\n- NAC: {}\n- WACN: {}\n- SYSID: {}", mhz(r.freq_hz), hex(&r.nac), hex(&r.wacn), hex(&r.sysid));
        if !r.iden_up.is_empty() {
            let _ = writeln!(b, "- Band plan ({} entries):", r.iden_up.len());
            for i in &r.iden_up {
                let kind = if i.is_tdma { format!("TDMA x{}", i.slots_per_carrier) } else { "FDMA".into() };
                let _ = writeln!(
                    b,
                    "    - iden {}: base {}, step {} kHz, offset {:+} MHz [{kind}]",
                    i.iden,
                    mhz(i.base_freq_hz),
                    i.step_hz as f64 / 1e3,
                    i.offset_hz as f64 / 1e6
                );
            }
        }
        if !r.neighbors.is_empty() {
            let _ = writeln!(b, "- Reported neighbors:");
            for n in &r.neighbors {
                let _ = writeln!(b, "    - {}, RFSS {}, Site {}", mhz(n.freq_hz), n.rfss_id, n.site_id);
            }
        }
        b.push('\n');
    }
    section(&mut out, "## New sites\n\nSystem found in RR; this RFSS/Site isn't.\n\n", b);

    let mut b = String::new();
    for r in &rs {
        let Some(en) = e(r) else { continue };
        let Some(o) = en.cc_freq_offset.as_ref().filter(|_| en.site_match && !en.cc_freq_in_db) else { continue };
        b += &site_header(r, en);
        let _ = writeln!(b, "- **RR has:** {}\n- **We decoded:** {}\n- Offset: {:+} Hz", mhz(o.expected_hz as u64), mhz(o.decoded_hz as u64), o.offset_hz);
        let _ = writeln!(b, "- Decoded WACN/SYSID/NAC: {} / {} / {}\n", hex(&r.wacn), hex(&r.sysid), hex(&r.nac));
    }
    section(
        &mut out,
        "## Frequency mismatches\n\nThe site is in RR, but the control channel decoded (on the frequency it announces for itself) isn't among RR's control channels for it: a new control channel, or one RR lists elsewhere.\n\n",
        b,
    );

    let mut b = String::new();
    for r in &rs {
        let Some(en) = e(r).filter(|en| en.site_match) else { continue };
        let Some(kind) = site_nac_diff(r.nac.as_deref(), en.rr_site_nac.as_deref()) else { continue };
        b += &site_header(r, en);
        let _ = writeln!(b, "- Control channel: {}\n- Decoded NAC: **{}**", mhz(r.freq_hz), hex(&r.nac));
        if kind == "missing" {
            let _ = writeln!(b, "- RR site NAC: *(not listed)* — submit the decoded NAC\n");
        } else {
            let _ = writeln!(b, "- RR site NAC: **{}** — submit a correction\n", en.rr_site_nac.clone().unwrap_or_default());
        }
    }
    section(&mut out, "## Site NAC mismatches\n\nThe site is in RR, but RR has no NAC for it, or another one.\n\n", b);

    let mut b = String::new();
    for r in &rs {
        let Some(en) = e(r).filter(|en| en.site_match && !en.neighbors_decoded_not_in_rr.is_empty()) else { continue };
        b += &site_header(r, en);
        let _ = writeln!(b, "- Decoded CC: {}\n- New-site candidates from ADJ_STS_BCST:", mhz(r.freq_hz));
        for n in &en.neighbors_decoded_not_in_rr {
            let _ = writeln!(b, "    - RFSS {} / Site {}{}", n.rfss_id, n.site_id, n.freq_hz.map_or(String::new(), |f| format!(", {}", mhz(f))));
        }
        b.push('\n');
    }
    section(&mut out, "## New-site neighbor candidates\n\nNeighbours announced in ADJ_STS_BCST whose (RFSS, Site) isn't in RR's roster for the system.\n\n", b);

    let mut b = String::new();
    for r in &rs {
        let Some(en) = e(r).filter(|en| en.site_match && !en.neighbor_cc_mismatches.is_empty()) else { continue };
        b += &site_header(r, en);
        let _ = writeln!(b, "- Decoded CC: {}", mhz(r.freq_hz));
        for m in &en.neighbor_cc_mismatches {
            let who = format!("RFSS {} / Site {}{}", m.rfss_id, m.site_id, m.rr_site_description.as_ref().map_or(String::new(), |d| format!(" — {d}")));
            if m.kind == "missing_from_rr" {
                let _ = writeln!(b, "    - {who}: advertised CC **{}** is not in the site's RR frequency list", mhz(m.advertised_cc_hz));
            } else {
                let _ = writeln!(
                    b,
                    "    - {who}: advertised CC **{}** is listed but `use=\"{}\"` (should be \"d\" or \"a\")",
                    mhz(m.advertised_cc_hz),
                    m.rr_use_code.clone().filter(|u| !u.is_empty()).unwrap_or("(blank)".into())
                );
            }
        }
        b.push('\n');
    }
    section(
        &mut out,
        "## Neighbor control-channel mismatches\n\nNeighbours whose announced control channel isn't in their RR site's frequency list, or is listed but not as a control channel.\n\n",
        b,
    );

    let mut b = String::new();
    for r in rows.iter().filter(|r| r.is_control()) {
        let Some(en) = e(r).filter(|en| en.site_match && !en.observed_neighbors.is_empty()) else { continue };
        b += &site_header(r, en);
        for n in &en.observed_neighbors {
            let f = n.freq_hz.map_or("(no CC)".into(), mhz);
            if n.in_rr {
                let bits: Vec<String> = [n.description.clone(), n.location.clone(), n.county.clone()].into_iter().flatten().collect();
                let _ = writeln!(
                    b,
                    "- RFSS {} / Site {}, {f}{}",
                    n.rfss_id,
                    n.site_id,
                    if bits.is_empty() { String::new() } else { format!(" — {}", bits.join(" / ")) }
                );
            } else {
                let _ = writeln!(b, "- RFSS {} / Site {}, {f} — *not in RR roster (new-site candidate)*", n.rfss_id, n.site_id);
            }
        }
        b.push('\n');
    }
    section(&mut out, "## Observed neighbor roster\n\nThe neighbours each site announced, with RR's description of each.\n\n", b);

    if new_systems + new_sites + freq + nac + nbr_new + nbr_cc == 0 {
        out.push_str("## (Nothing to submit)\n\nEvery system decoded matched RadioReference. No submissions needed.\n");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::enrich::{FreqOffset, NeighborRef};
    use crate::rows::Neighbor;

    fn cc(freq: u64, offset_hz: f64) -> Row {
        Row {
            freq_hz: freq,
            complete: true,
            verdict: Some("complete".into()),
            wacn: Some("BEE00".into()),
            sysid: Some("44A".into()),
            nac: Some("440".into()),
            rfss_id: Some(1),
            site_id: Some(1),
            carrier_offset_hz: Some(offset_hz),
            signal: crate::rows::Signal { decode_rate_pct: Some(99.0), ..Default::default() },
            ..Default::default()
        }
    }

    #[test]
    fn the_clock_from_measured_offsets() {
        let rows = vec![cc(855_000_000, 800.0), cc(858_000_000, 810.0), cc(771_000_000, 720.0)];
        let c = clock(&rows).unwrap();
        assert_eq!(c.source, "measured");
        assert!((c.median_ppm - 0.94).abs() < 0.01, "{}", c.median_ppm);
        assert_eq!(c.consistency, "high");
        assert!(clock(&rows[..2]).is_none());
    }

    #[test]
    fn reports_say_what_rr_lacks() {
        let mut a = cc(858_437_500, 800.0);
        a.neighbors = vec![Neighbor { freq_hz: 859_712_500, rfss_id: 1, site_id: 8, ..Default::default() }];
        a.rr = Some(Enrichment {
            system_match: true,
            site_match: true,
            rr_system_name: Some("NTIRN".into()),
            rr_site_nac: None,
            cc_freq_offset: Some(FreqOffset { decoded_hz: 858_437_500, expected_hz: 858_437_500, offset_hz: 0, ppm: 0.0 }),
            cc_freq_in_db: true,
            neighbors_decoded_not_in_rr: vec![NeighborRef { rfss_id: 1, site_id: 8, freq_hz: Some(859_712_500), ..Default::default() }],
            ..Default::default()
        });
        let mut b = cc(769_281_250, 700.0);
        b.wacn = Some("7494E".into());
        b.rr = Some(Enrichment { notes: vec!["new system".into()], ..Default::default() });
        let mut copy = cc(773_106_250, 0.0);
        copy.copy_of_hz = Some(771_368_750);
        copy.complete = false;
        let rows = vec![a, b, copy];
        let md = submissions(&rows);
        assert!(md.contains("## New systems") && md.contains("WACN 7494E"), "{md}");
        assert!(md.contains("## Site NAC mismatches") && md.contains("*(not listed)*"));
        assert!(md.contains("RFSS 1 / Site 8, 859.71250 MHz"));
        assert!(!md.contains("773.10625"));
        let txt = text(&rows);
        assert!(txt.contains("2 control channels") && txt.contains("copy of 771.36875 MHz"), "{txt}");
        assert!(txt.contains("RR: NEW SYSTEM"));
    }
}
