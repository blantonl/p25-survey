//! Survey records as read back from NDJSON (ours, or the Python version's):
//! what enrichment and the reports work from. Fields this doesn't name are
//! kept and written back as they were.

use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::enrich::Enrichment;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Neighbor {
    pub freq_hz: u64,
    pub rfss_id: u32,
    pub site_id: u32,
    #[serde(default)]
    pub sysid: Option<String>,
    #[serde(default)]
    pub wacn: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Iden {
    pub iden: u32,
    pub base_freq_hz: u64,
    pub step_hz: u64,
    pub offset_hz: i64,
    #[serde(default)]
    pub is_tdma: bool,
    #[serde(default = "one")]
    pub slots_per_carrier: u32,
}

fn one() -> u32 {
    1
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Signal {
    #[serde(default)]
    pub rssi_dbfs_mean: Option<f64>,
    #[serde(default)]
    pub rssi_dbfs_peak: Option<f64>,
    #[serde(default)]
    pub ber_pct_mean: Option<f64>,
    #[serde(default)]
    pub decode_rate_pct: Option<f64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Row {
    pub freq_hz: u64,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub complete: bool,
    #[serde(default)]
    pub wacn: Option<String>,
    #[serde(default)]
    pub sysid: Option<String>,
    #[serde(default)]
    pub nac: Option<String>,
    #[serde(default)]
    pub rfss_id: Option<u32>,
    #[serde(default)]
    pub site_id: Option<u32>,
    #[serde(default)]
    pub neighbors: Vec<Neighbor>,
    #[serde(default)]
    pub secondary_cc: Vec<u64>,
    #[serde(default)]
    pub iden_up: Vec<Iden>,
    #[serde(default)]
    pub signal: Signal,
    #[serde(default)]
    pub dwell_ms: u64,
    #[serde(default)]
    pub sdr_driver: String,
    #[serde(default)]
    pub sdr_gain_db: Option<f64>,
    #[serde(default)]
    pub sdr_ppm: f64,
    #[serde(default)]
    pub notes: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rr: Option<Enrichment>,
    // Ours (absent from the Python version's records):
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verdict: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub level_db: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub copy_of_hz: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub carrier_offset_hz: Option<f64>,
    /// Everything else, kept as it came.
    #[serde(flatten)]
    pub rest: Map<String, Value>,
}

impl Row {
    /// A control channel of its own. Ours say so; a Python record counts if
    /// any identity was heard (which takes in voice channels: it couldn't tell).
    pub fn is_control(&self) -> bool {
        match self.verdict.as_deref() {
            Some(v) => matches!(v, "complete" | "partial") && self.copy_of_hz.is_none(),
            None => self.wacn.is_some() || self.rfss_id.is_some() || self.nac.is_some(),
        }
    }

    pub fn is_copy(&self) -> bool {
        self.copy_of_hz.is_some()
    }

    /// P25 heard, but not a control channel (ours only).
    pub fn is_other_p25(&self) -> bool {
        self.verdict.as_deref() == Some("p25-not-control")
    }
}

pub fn read(path: &Path) -> Result<Vec<Row>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .enumerate()
        .map(|(i, l)| serde_json::from_str(l).map_err(|e| format!("{} line {}: {e}", path.display(), i + 1)))
        .collect()
}

pub fn write(path: &Path, rows: &[Row]) -> Result<(), String> {
    let mut s = String::new();
    for r in rows {
        s += &serde_json::to_string(r).map_err(|e| e.to_string())?;
        s.push('\n');
    }
    std::fs::write(path, s).map_err(|e| format!("{}: {e}", path.display()))
}

/// MHz to 10 Hz, exactly (no rounding surprises on the 6.25 kHz raster).
pub fn mhz(freq_hz: u64) -> String {
    let r = (freq_hz + 5) / 10 * 10;
    format!("{}.{:05} MHz", r / 1_000_000, (r % 1_000_000) / 10)
}

/// The band a frequency is in, named as the Python version names them.
pub fn band_name(freq_hz: u64) -> &'static str {
    match freq_hz {
        769_000_000..=775_000_000 => "700 MHz PS narrowband downlink (base TX, mobile RX)",
        799_000_000..=805_000_000 => "700 MHz PS narrowband uplink (mobile TX, base RX)",
        851_000_000..=869_000_000 => "800 MHz public safety (rebanded)",
        935_000_000..=941_000_000 => "900 MHz",
        380_000_000..=512_000_000 => "UHF land mobile",
        150_000_000..=174_000_000 => "VHF land mobile",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mhz_is_exact() {
        assert_eq!(mhz(851_006_250), "851.00625 MHz");
        assert_eq!(mhz(155_377_500), "155.37750 MHz");
    }

    #[test]
    fn a_python_record_reads_and_keeps_what_it_has() {
        let l = r#"{"freq_hz": 851087500, "ts": "2026-05-04T12:55:34.926+00:00", "complete": true, "wacn": "00001", "sysid": "3CC", "nac": "3CC", "rfss_id": 20, "site_id": 1, "neighbors": [{"freq_hz": 774481250, "rfss_id": 20, "site_id": 2, "sysid": null, "wacn": null}], "iden_up": [{"iden": 0, "base_freq_hz": 851006250, "step_hz": 6250, "offset_hz": -45000000, "is_tdma": true, "slots_per_carrier": 1}], "signal": {"rssi_dbfs_mean": -53.92, "rssi_dbfs_peak": -52.5, "ber_pct_mean": 17.86, "decode_rate_pct": 82.14}, "dwell_ms": 2322, "sdr_driver": "airspy", "sdr_gain_db": null, "sdr_ppm": 0.0, "notes": [], "something_new": 7}"#;
        let r: Row = serde_json::from_str(l).unwrap();
        assert!(r.is_control());
        assert_eq!(r.wacn.as_deref(), Some("00001"));
        let back = serde_json::to_value(&r).unwrap();
        assert_eq!(back["something_new"], 7);
        assert!(back.get("verdict").is_none());
    }
}
