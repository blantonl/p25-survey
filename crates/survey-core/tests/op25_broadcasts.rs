//! Real broadcasts from the corpus, as op25 decoded them, through our parser:
//! the site must come out as the Python p25-survey (v0.3.5) recorded it from
//! the same messages, except where DECISIONS.md says we differ.
//!
//! Fixtures (tests/data), made from the corpus baseline:
//! - broadcasts.json: every distinct site broadcast op25 passed on four
//!   control channels: NAC (2 octets) + the TSBK's 10 message octets, hex.
//! - op25_records.json: what p25-survey made of them.

use std::collections::BTreeSet;

use serde::Deserialize;
use survey_core::p25::{SiteInfo, TSBK_LEN, parse_mac, parse_tsbk};

#[derive(Deserialize)]
struct Fixture {
    freq_hz: u64,
    kind: String,
    hex: String,
}

#[derive(Deserialize)]
struct Record {
    freq_hz: u64,
    wacn: Option<String>,
    sysid: Option<String>,
    nac: Option<String>,
    rfss_id: Option<u8>,
    site_id: Option<u8>,
    iden_up: Vec<Iden>,
    neighbors: Vec<Neighbor>,
    #[serde(default)]
    secondary_cc: Vec<u64>,
}

#[derive(Deserialize)]
struct Iden {
    iden: u8,
    base_freq_hz: u64,
    step_hz: u32,
    offset_hz: i64,
    is_tdma: bool,
    slots_per_carrier: u8,
}

#[derive(Deserialize)]
struct Neighbor {
    freq_hz: u64,
    rfss_id: u8,
    site_id: u8,
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}

fn hex_id(s: &Option<String>) -> Option<u32> {
    s.as_deref().map(|s| u32::from_str_radix(s, 16).unwrap())
}

fn load<T: for<'a> Deserialize<'a>>(name: &str) -> T {
    // P25S_FIXTURES: another directory of the same two files (the whole corpus).
    let dir = std::env::var("P25S_FIXTURES").unwrap_or_else(|_| format!("{}/tests/data", env!("CARGO_MANIFEST_DIR")));
    let path = format!("{dir}/{name}");
    serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap()
}

#[test]
fn sites_match_op25() {
    let fixtures: Vec<Fixture> = load("broadcasts.json");
    let records: Vec<Record> = load("op25_records.json");
    assert!(!records.is_empty());
    // What isn't a disagreement with op25 but is worth knowing: a channel that
    // announces another frequency (a copy of a control channel heard
    // elsewhere), or channel numbers the band plan heard doesn't resolve.
    let mut notes = Vec::new();
    for rec in &records {
        let mut site = SiteInfo::default();
        let mut taken = 0;
        for f in fixtures.iter().filter(|f| f.freq_hz == rec.freq_hz) {
            let b = unhex(&f.hex);
            let nac = u16::from_be_bytes([b[0], b[1]]);
            if nac != 0 && nac != 0xffff {
                site.nac_heard(nac);
            }
            let parsed = match f.kind.as_str() {
                "fdma" => parse_tsbk(b[2..2 + TSBK_LEN].try_into().unwrap()),
                _ => parse_mac(&b[2..]),
            };
            let Some(parsed) = parsed else { panic!("{} MHz: {} isn't a site broadcast", rec.freq_hz as f64 / 1e6, f.hex) };
            site.push(parsed);
            taken += 1;
        }
        let at = format!("{:.5} MHz", rec.freq_hz as f64 / 1e6);
        assert!(taken > 0, "{at}: no fixtures");

        assert_eq!(site.wacn, hex_id(&rec.wacn), "{at}: WACN");
        assert_eq!(site.sysid.map(u32::from), hex_id(&rec.sysid), "{at}: system");
        assert_eq!(site.nac.map(u32::from), hex_id(&rec.nac), "{at}: NAC");
        assert_eq!(site.rfss, rec.rfss_id, "{at}: RFSS");
        assert_eq!(site.site, rec.site_id, "{at}: site");
        if site.announced_hz() != Some(rec.freq_hz) {
            notes.push(format!("{at}: announces {:?}", site.announced_hz().map(|f| f as f64 / 1e6)));
        }
        if site.unresolved() > 0 {
            notes.push(format!("{at}: {} channel number(s) not in the band plan heard", site.unresolved()));
        }

        let plan: Vec<_> = site.plan.entries().map(|i| (i.iden, i.base_hz, i.step_hz, i.offset_hz, i.tdma, i.slots)).collect();
        // (p25-survey 0.3.5 called a 1-slot IDEN_UP_TDMA "TDMA"; it's FDMA.)
        let want: Vec<_> =
            rec.iden_up.iter().map(|i| (i.iden, i.base_freq_hz, i.step_hz, i.offset_hz, i.is_tdma && i.slots_per_carrier > 1, i.slots_per_carrier)).collect();
        assert_eq!(plan, want, "{at}: band plan");

        // (p25-survey lists only the neighbours it could resolve.)
        let ours: BTreeSet<_> = site.neighbors().filter_map(|(n, f)| Some((f?, n.rfss, n.site))).collect();
        let theirs: BTreeSet<_> = rec.neighbors.iter().map(|n| (n.freq_hz, n.rfss_id, n.site_id)).collect();
        assert_eq!(ours, theirs, "{at}: neighbours");

        let ours: BTreeSet<_> = site.secondary().filter_map(|(_, f)| f).collect();
        let theirs: BTreeSet<_> = rec.secondary_cc.iter().copied().collect();
        assert_eq!(ours, theirs, "{at}: secondary control channels");
    }
    for n in &notes {
        eprintln!("note: {n}");
    }
    // The committed fixtures are four clean control channels: no notes.
    if std::env::var_os("P25S_FIXTURES").is_none() {
        assert!(notes.is_empty());
    }
}
