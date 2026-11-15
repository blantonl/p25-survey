//! Survey records as NDJSON, in the Python p25-survey's schema (docs/schema.md
//! there), so its reports and tools read them. Fields it doesn't know are
//! added at the top level only: it builds neighbours, band plan entries and
//! the signal block from exactly their keys.

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};
use survey_core::survey::{Record, Verdict};

/// The record for one candidate.
pub fn to_json(r: &Record, driver: &str, gain_db: Option<f64>) -> Value {
    let s = &r.site;
    let hex = |v: Option<u32>, w: usize| v.map(|v| format!("{v:0w$X}"));
    let mut notes = Vec::new();
    if let Some(c) = r.copy_of {
        notes.push(format!("a copy of the control channel at {:.5} MHz (an image or intermodulation), not one of its own", c / 1e6));
    }
    if s.unresolved() > 0 {
        notes.push(format!("{} channel number(s) not resolved: their band plan entry wasn't heard", s.unresolved()));
    }
    let rate = r.decode_rate();
    json!({
        "freq_hz": r.freq_hz as u64,
        "ts": now_utc(),
        "complete": r.verdict == Verdict::Complete && r.copy_of.is_none(),
        "wacn": hex(s.wacn, 5),
        "sysid": hex(s.sysid.map(u32::from), 3),
        "nac": hex(s.nac.map(u32::from), 3),
        "rfss_id": s.rfss,
        "site_id": s.site,
        "neighbors": s.neighbors().filter_map(|(n, f)| Some(json!({
            "freq_hz": f?,
            "rfss_id": n.rfss,
            "site_id": n.site,
            "sysid": format!("{:03X}", n.sysid),
            "wacn": n.wacn.map(|w| format!("{w:05X}")),
        }))).collect::<Vec<_>>(),
        "secondary_cc": s.secondary().filter_map(|(_, f)| f).collect::<Vec<_>>(),
        "iden_up": s.plan.entries().map(|i| json!({
            "iden": i.iden,
            "base_freq_hz": i.base_hz,
            "step_hz": i.step_hz,
            "offset_hz": i.offset_hz,
            "is_tdma": i.tdma,
            "slots_per_carrier": i.slots,
        })).collect::<Vec<_>>(),
        "signal": {
            "rssi_dbfs_mean": Value::Null,
            "rssi_dbfs_peak": Value::Null,
            "ber_pct_mean": rate.map(|r| round2(100.0 * (1.0 - r))),
            "decode_rate_pct": rate.map(|r| round2(100.0 * r)),
        },
        "dwell_ms": (r.dwell_s * 1000.0).round() as u64,
        "sdr_driver": driver,
        "sdr_gain_db": gain_db,
        "sdr_ppm": 0.0,
        "notes": notes,
        // Not in the Python schema:
        "verdict": verdict_str(r.verdict),
        "level_db": round2(r.level_db),
        "heard_hz": r.heard_hz as u64,
        "announced_hz": s.announced_hz(),
        "copy_of_hz": r.copy_of.map(|c| c as u64),
        "tsbks": r.tsbks,
        "carrier_offset_hz": r.offset_hz.round(),
        "receivers": {
            "c4fm": { "frames": r.c4fm.frames_total(), "tsbk_ok": r.c4fm.tsbk_ok, "tsbk_bad": r.c4fm.tsbk_bad },
            "cqpsk": { "frames": r.cqpsk.frames_total(), "tsbk_ok": r.cqpsk.tsbk_ok, "tsbk_bad": r.cqpsk.tsbk_bad },
        },
    })
}

pub fn verdict_str(v: Verdict) -> &'static str {
    match v {
        Verdict::Complete => "complete",
        Verdict::Partial => "partial",
        Verdict::NotControl => "p25-not-control",
        Verdict::NotP25 => "not-p25",
    }
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

/// Now, as ISO 8601 UTC with milliseconds.
pub fn now_utc() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = d.as_secs() as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + (month <= 2) as i64;
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}+00:00", rem / 3600, rem % 3600 / 60, rem % 60, d.subsec_millis())
}
