//! A survey record against RadioReference: is the system there, the site,
//! the control channel's frequency, the NAC; which neighbours it announces
//! are missing from RR's roster, and which neighbour control channels RR
//! doesn't list (or lists as traffic). Same matching, and the same record
//! (`rr`, field for field), as the Python version's enrich.py, so either
//! reads the other's.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::rows::Row;
use crate::rr::{Client, Site, System, norm_hex};

/// Within this, two frequencies are the same channel.
const SAME_HZ: i64 = 1000;

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct FreqOffset {
    pub decoded_hz: i64,
    pub expected_hz: i64,
    pub offset_hz: i64,
    pub ppm: f64,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct NeighborRef {
    pub rfss_id: u32,
    pub site_id: u32,
    #[serde(default)]
    pub freq_hz: Option<u64>,
    #[serde(default)]
    pub in_rr: bool,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub location: Option<String>,
    #[serde(default)]
    pub county: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct NeighborCcMismatch {
    pub rfss_id: u32,
    pub site_id: u32,
    pub advertised_cc_hz: u64,
    /// "missing_from_rr" or "not_marked_control".
    pub kind: String,
    #[serde(default)]
    pub rr_use_code: Option<String>,
    #[serde(default)]
    pub rr_site_description: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Enrichment {
    pub system_match: bool,
    pub site_match: bool,
    /// The site exists but can't be told apart (several subsystems with this
    /// RFSS/site and no NAC to choose; or several RR systems).
    pub ambiguous_site: bool,
    pub rr_system_name: Option<String>,
    pub rr_sid: Option<u32>,
    pub rr_site_description: Option<String>,
    pub rr_site_location: Option<String>,
    pub rr_site_county: Option<String>,
    pub rr_site_nac: Option<String>,
    pub rr_site_lat: Option<f64>,
    pub rr_site_lon: Option<f64>,
    pub rr_site_licenses: Vec<String>,
    pub cc_freq_offset: Option<FreqOffset>,
    pub cc_freq_in_db: bool,
    pub observed_neighbors: Vec<NeighborRef>,
    pub neighbors_decoded_not_in_rr: Vec<NeighborRef>,
    pub neighbor_cc_mismatches: Vec<NeighborCcMismatch>,
    pub notes: Vec<String>,
}

fn note(n: impl Into<String>) -> Enrichment {
    Enrichment { notes: vec![n.into()], ..Default::default() }
}

fn some(s: &str) -> Option<String> {
    (!s.is_empty()).then(|| s.to_string())
}

/// Look a record up. Partial records get a note and nothing else.
pub fn enrich(r: &Row, rr: &Client) -> Enrichment {
    let (Some(wacn), Some(sysid)) = (&r.wacn, &r.sysid) else {
        return note("partial record: missing WACN or SYSID, skipped enrichment");
    };
    let systems = match rr.systems(wacn, sysid) {
        Ok(s) => s,
        Err(e) => return note(format!("RR lookup failed: {e}")),
    };
    if systems.is_empty() {
        return note(format!("new system: WACN {wacn} / SYSID {sysid} not found in RadioReference"));
    }
    let name = |s: &System| format!("{:?} (sid={})", s.name, s.sid);
    let (Some(rfss), Some(site)) = (r.rfss_id, r.site_id) else {
        let first = &systems[0];
        let mut e = Enrichment { system_match: true, rr_system_name: Some(first.name.clone()), rr_sid: Some(first.sid), ..Default::default() };
        e.notes.push("RFSS/Site not decoded; can't match site-level data".into());
        if systems.len() > 1 {
            let others: Vec<String> = systems[1..].iter().map(name).collect();
            e.notes.push(format!(
                "WACN {wacn}/SYSID {sysid} matches multiple RR systems; reporting against first: {}; also: {}",
                name(first),
                others.join(", ")
            ));
        }
        return e;
    };

    let mut resolved = Vec::new();
    let mut tried: Vec<(&System, Vec<Site>, bool)> = Vec::new();
    for system in &systems {
        let Ok(sites) = rr.sites(system.sid) else {
            tried.push((system, Vec::new(), false));
            continue;
        };
        let (found, ambiguous) = find_site(&sites, rfss, site, r.nac.as_deref());
        if let Some(s) = found {
            resolved.push(site_match(r, system, &sites, s));
        }
        tried.push((system, sites, ambiguous));
    }
    match resolved.len() {
        1 => return resolved.pop().unwrap(),
        n if n > 1 => {
            let names: Vec<String> =
                resolved.iter().map(|e| format!("{:?} (sid={})", e.rr_system_name.clone().unwrap_or_default(), e.rr_sid.unwrap_or(0))).collect();
            let mut first = resolved.swap_remove(0);
            first.ambiguous_site = true;
            first.notes.push(format!(
                "ambiguous: WACN {wacn}/SYSID {sysid} matches multiple RR systems that all have RFSS {rfss} / Site {site}: {}",
                names.join(", ")
            ));
            return first;
        }
        _ => {}
    }
    let first = &systems[0];
    let mut e = Enrichment { system_match: true, rr_system_name: Some(first.name.clone()), rr_sid: Some(first.sid), ..Default::default() };
    if let Some((system, sites, _)) = tried.iter().find(|t| t.2) {
        e.ambiguous_site = true;
        let nacs: Vec<String> =
            sites.iter().filter(|s| s.rfss == rfss && s.site_number == site).map(|s| if s.nac.is_empty() { "(blank)".into() } else { s.nac.clone() }).collect();
        e.notes.push(format!(
            "ambiguous site: RFSS {rfss} / Site {site} matches multiple subsystems in {}; decoded NAC {} doesn't match any candidate (RR has NAC {})",
            name(system),
            r.nac.as_deref().unwrap_or("—"),
            nacs.join(", ")
        ));
        return e;
    }
    if systems.len() > 1 {
        e.ambiguous_site = true;
        let names: Vec<String> = systems.iter().map(name).collect();
        e.notes.push(format!(
            "ambiguous: WACN {wacn}/SYSID {sysid} matches multiple RR systems ({}) but RFSS {rfss} / Site {site} isn't in any of them — can't tell which system this would be a new site for",
            names.join(", ")
        ));
        return e;
    }
    e.notes.push(format!("new site: RFSS {rfss} / Site {site} not in RR for system {}", name(first)));
    e
}

/// The RR site with this RFSS and site number. Several (subsystems sharing
/// numbers) are told apart by NAC; (None, true) if they can't be.
pub fn find_site<'a>(sites: &'a [Site], rfss: u32, site: u32, nac: Option<&str>) -> (Option<&'a Site>, bool) {
    let matches: Vec<&Site> = sites.iter().filter(|s| s.rfss == rfss && s.site_number == site).collect();
    match matches.len() {
        0 => (None, false),
        1 => (Some(matches[0]), false),
        _ => {
            if let Some(nac) = nac {
                let by_nac: Vec<&Site> = matches.iter().copied().filter(|s| !s.nac.is_empty() && norm_hex(&s.nac) == norm_hex(nac)).collect();
                if by_nac.len() == 1 {
                    return (Some(by_nac[0]), false);
                }
            }
            (None, true)
        }
    }
}

fn site_match(r: &Row, system: &System, sites: &[Site], site: &Site) -> Enrichment {
    let mut e = Enrichment {
        system_match: true,
        site_match: true,
        rr_system_name: Some(system.name.clone()),
        rr_sid: Some(system.sid),
        rr_site_description: some(&site.description),
        rr_site_location: some(&site.location),
        rr_site_county: some(&site.county),
        rr_site_nac: some(&site.nac),
        rr_site_lat: site.lat,
        rr_site_lon: site.lon,
        rr_site_licenses: site.licenses.clone(),
        ..Default::default()
    };
    let f = r.freq_hz as i64;
    if let Some(expected) = site.control_freqs().map(|c| c as i64).min_by_key(|c| (c - f).abs()) {
        let offset = f - expected;
        e.cc_freq_offset = Some(FreqOffset {
            decoded_hz: f,
            expected_hz: expected,
            offset_hz: offset,
            ppm: if f == 0 { 0.0 } else { (offset as f64 / f as f64 * 1e9).round() / 1e3 },
        });
        e.cc_freq_in_db = offset.abs() < SAME_HZ;
    }
    let others: BTreeMap<(u32, u32), &Site> =
        sites.iter().filter(|s| !(s.rfss == site.rfss && s.site_number == site.site_number)).map(|s| ((s.rfss, s.site_number), s)).collect();
    let mut neighbors: Vec<_> = r.neighbors.iter().collect();
    neighbors.sort_by_key(|n| (n.rfss_id, n.site_id));
    for n in neighbors {
        match others.get(&(n.rfss_id, n.site_id)) {
            None => {
                let nr = NeighborRef { rfss_id: n.rfss_id, site_id: n.site_id, freq_hz: Some(n.freq_hz), in_rr: false, ..Default::default() };
                e.observed_neighbors.push(nr.clone());
                e.neighbors_decoded_not_in_rr.push(nr);
            }
            Some(s) => {
                e.observed_neighbors.push(NeighborRef {
                    rfss_id: n.rfss_id,
                    site_id: n.site_id,
                    freq_hz: Some(n.freq_hz),
                    in_rr: true,
                    description: some(&s.description),
                    location: some(&s.location),
                    county: some(&s.county),
                });
                if let Some(m) = neighbor_cc(n.rfss_id, n.site_id, n.freq_hz, s) {
                    e.neighbor_cc_mismatches.push(m);
                }
            }
        }
    }
    e
}

/// A neighbour's announced control channel against its RR site: listed, and
/// as a control channel?
fn neighbor_cc(rfss: u32, site: u32, freq: u64, s: &Site) -> Option<NeighborCcMismatch> {
    let m = |kind: &str, use_code: Option<String>| NeighborCcMismatch {
        rfss_id: rfss,
        site_id: site,
        advertised_cc_hz: freq,
        kind: kind.into(),
        rr_use_code: use_code,
        rr_site_description: some(&s.description),
    };
    match s.frequencies.iter().min_by_key(|f| (f.freq_hz as i64 - freq as i64).abs()) {
        None => Some(m("missing_from_rr", None)),
        Some(c) if (c.freq_hz as i64 - freq as i64).abs() >= SAME_HZ => Some(m("missing_from_rr", None)),
        Some(c) if !c.is_control() => Some(m("not_marked_control", Some(c.use_code.clone()))),
        _ => None,
    }
}

/// Decoded NAC against RR's for the site: "missing" (RR has none), "differs",
/// or None (the same, or none decoded).
pub fn site_nac_diff(decoded: Option<&str>, rr: Option<&str>) -> Option<&'static str> {
    let decoded = decoded?;
    match rr.map(str::trim).filter(|s| !s.is_empty()) {
        None => Some("missing"),
        Some(rr) => (norm_hex(rr) != norm_hex(decoded)).then_some("differs"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rows::Neighbor;
    use crate::rr::tests::client;

    const DETAILS: &str = "<getTrsDetailsReturn><sName>NTIRN</sName><sysid><item><wacn>BEE00</wacn><sysid>44A</sysid></item></sysid></getTrsDetailsReturn>";
    const SITES: &str = r#"<return>
        <item><rfss>1</rfss><siteNumber>1</siteNumber><nac>440</nac><siteDescr>Fort Worth</siteDescr><siteCt>Tarrant</siteCt>
          <siteFreqs><item><freq>858.4375</freq><use>d</use></item></siteFreqs></item>
        <item><rfss>1</rfss><siteNumber>2</siteNumber><nac></nac><siteDescr>Arlington</siteDescr>
          <siteFreqs><item><freq>858.1375</freq><use></use></item></siteFreqs></item>
      </return>"#;

    fn rr() -> Client {
        client(vec![
            ("getTrsBySysid", None, "<r><item><sid>5</sid></item></r>".into()),
            ("getTrsDetails", Some("5"), DETAILS.into()),
            ("getTrsSites", None, SITES.into()),
        ])
    }

    fn row(freq: u64, rfss: u32, site: u32) -> Row {
        Row {
            freq_hz: freq,
            wacn: Some("BEE00".into()),
            sysid: Some("44A".into()),
            nac: Some("440".into()),
            rfss_id: Some(rfss),
            site_id: Some(site),
            ..Default::default()
        }
    }

    #[test]
    fn a_known_site_with_its_neighbours_checked() {
        let mut r = row(858_437_500, 1, 1);
        r.neighbors = vec![
            Neighbor { freq_hz: 858_137_500, rfss_id: 1, site_id: 2, ..Default::default() },
            Neighbor { freq_hz: 859_712_500, rfss_id: 1, site_id: 8, ..Default::default() },
        ];
        let e = enrich(&r, &rr());
        assert!(e.system_match && e.site_match && e.cc_freq_in_db, "{e:?}");
        assert_eq!(e.rr_site_description.as_deref(), Some("Fort Worth"));
        assert_eq!(e.cc_freq_offset.as_ref().unwrap().offset_hz, 0);
        // 1-8 isn't in RR's roster; 1-2's control channel is listed as traffic.
        assert_eq!(e.neighbors_decoded_not_in_rr.len(), 1);
        assert_eq!(e.neighbors_decoded_not_in_rr[0].site_id, 8);
        assert_eq!(e.neighbor_cc_mismatches.len(), 1);
        assert_eq!(e.neighbor_cc_mismatches[0].kind, "not_marked_control");
    }

    #[test]
    fn a_new_site_and_a_new_system() {
        let e = enrich(&row(859_712_500, 1, 8), &rr());
        assert!(e.system_match && !e.site_match && !e.ambiguous_site);
        assert!(e.notes[0].starts_with("new site"), "{:?}", e.notes);
        let none = client(vec![("getTrsBySysid", None, "<r></r>".into())]);
        let e = enrich(&row(859_712_500, 1, 8), &none);
        assert!(!e.system_match);
        assert!(e.notes[0].starts_with("new system"));
    }

    #[test]
    fn a_control_channel_rr_doesnt_list() {
        let e = enrich(&row(858_537_500, 1, 1), &rr());
        assert!(e.site_match && !e.cc_freq_in_db);
        assert_eq!(e.cc_freq_offset.unwrap().offset_hz, 100_000);
    }

    #[test]
    fn nac_differences() {
        assert_eq!(site_nac_diff(Some("440"), Some("440")), None);
        assert_eq!(site_nac_diff(Some("440"), Some("0440")), None);
        assert_eq!(site_nac_diff(Some("440"), Some("")), Some("missing"));
        assert_eq!(site_nac_diff(Some("440"), Some("441")), Some("differs"));
        assert_eq!(site_nac_diff(None, Some("441")), None);
    }
}
