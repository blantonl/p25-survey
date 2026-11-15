//! RadioReference's SOAP API, the four calls the survey needs:
//!
//! - getUserData: check the login
//! - getTrsBySysid: the RR systems with a P25 System ID
//! - getTrsDetails: a system's name and its WACN/SYSID pairs
//! - getTrsSites: a system's sites, their RFSS/site numbers, NACs and
//!   frequencies
//!
//! Every call carries the user's login and the program's appKey. The login
//! is asked for at the start of a run and never stored. Answers are cached
//! for the run only.

use std::cell::RefCell;
use std::collections::HashMap;
use std::time::Duration;

use roxmltree::{Document, Node};

const ENDPOINT: &str = "https://api.radioreference.com/soap2/";
const NS: &str = "http://api.radioreference.com/soap2";
const SOAP_NS: &str = "http://schemas.xmlsoap.org/soap/envelope/";
/// RadioReference issues appKeys per application; this one is p25-survey's
/// (P25_SURVEY_APPKEY overrides it, e.g. to test with another).
const BUNDLED_APP_KEY: &str = "18e7613e-40c2-11f1-bb32-0ef97433b5f9";

pub fn app_key() -> String {
    std::env::var("P25_SURVEY_APPKEY").unwrap_or_else(|_| BUNDLED_APP_KEY.to_string())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RrError {
    /// The login was refused.
    pub auth: bool,
    pub message: String,
}

impl std::fmt::Display for RrError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

fn err(message: impl Into<String>) -> RrError {
    RrError { auth: false, message: message.into() }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct User {
    pub username: String,
    pub sub_expire_date: String,
}

/// One WACN/SYSID pair a system carries.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SysidEntry {
    pub wacn: String,
    pub sysid: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct System {
    /// RR's id for the system.
    pub sid: u32,
    pub name: String,
    pub sysids: Vec<SysidEntry>,
}

/// Hex without leading zeros, upper case: how RR's and ours are compared.
pub fn norm_hex(s: &str) -> String {
    let t = s.trim().trim_start_matches('0').to_ascii_uppercase();
    if t.is_empty() { "0".into() } else { t }
}

impl System {
    /// Carries this WACN and SYSID (an entry with no WACN matches any).
    pub fn has_wacn_sysid(&self, wacn: &str, sysid: &str) -> bool {
        let (w, s) = (norm_hex(wacn), norm_hex(sysid));
        self.sysids.iter().any(|e| (e.wacn.trim().is_empty() || norm_hex(&e.wacn) == w) && norm_hex(&e.sysid) == s)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SiteFreq {
    pub freq_hz: u64,
    /// "d" primary control, "a" alternate control, else traffic.
    pub use_code: String,
}

impl SiteFreq {
    pub fn is_control(&self) -> bool {
        matches!(self.use_code.to_ascii_lowercase().as_str(), "d" | "a")
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Site {
    pub rfss: u32,
    /// The P25 site number (what is decoded), not RR's database id.
    pub site_number: u32,
    pub nac: String,
    pub description: String,
    pub location: String,
    pub county: String,
    pub lat: Option<f64>,
    pub lon: Option<f64>,
    pub frequencies: Vec<SiteFreq>,
    pub licenses: Vec<String>,
}

impl Site {
    pub fn control_freqs(&self) -> impl Iterator<Item = u64> + '_ {
        self.frequencies.iter().filter(|f| f.is_control()).map(|f| f.freq_hz)
    }
}

/// Sends a SOAP envelope for an operation, returns the response body.
pub trait Transport: Send {
    fn post(&self, op: &str, envelope: &str) -> Result<String, RrError>;
}

/// HTTPS to api.radioreference.com.
pub struct Https {
    agent: ureq::Agent,
}

impl Https {
    pub fn new() -> Https {
        let agent = ureq::Agent::config_builder().timeout_global(Some(Duration::from_secs(30))).http_status_as_error(false).build().into();
        Https { agent }
    }
}

impl Transport for Https {
    fn post(&self, op: &str, envelope: &str) -> Result<String, RrError> {
        let mut resp = self
            .agent
            .post(ENDPOINT)
            .header("Content-Type", "text/xml; charset=utf-8")
            .header("SOAPAction", &format!("\"{op}\""))
            .header("User-Agent", concat!("p25-survey/", env!("CARGO_PKG_VERSION")))
            .send(envelope)
            .map_err(|e| err(format!("RR {op}: network error: {e}")))?;
        let status = resp.status().as_u16();
        let body = resp.body_mut().read_to_string().map_err(|e| err(format!("RR {op}: {e}")))?;
        if status >= 400 {
            let fault = fault_string(&body).unwrap_or_else(|| body.chars().take(200).collect());
            return Err(RrError { auth: status == 401 || fault.to_lowercase().contains("auth"), message: format!("RR {op}: HTTP {status}: {fault}") });
        }
        Ok(body)
    }
}

pub struct Client {
    username: String,
    password: String,
    app_key: String,
    transport: Box<dyn Transport>,
    systems: RefCell<HashMap<String, Vec<System>>>,
    details: RefCell<HashMap<u32, System>>,
    sites: RefCell<HashMap<u32, Vec<Site>>>,
}

fn escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&apos;")
}

fn local<'a>(n: &Node<'a, '_>) -> &'a str {
    n.tag_name().name()
}

/// The first element with this local name: `n` itself or any descendant.
fn find<'a, 'i>(n: Node<'a, 'i>, name: &str) -> Option<Node<'a, 'i>> {
    n.descendants().find(|d| d.is_element() && local(d) == name)
}

/// Every element (n or below) whose children include all of `names`: SOAP
/// array members and result structures, found by content, not wrapper.
fn with_children<'a, 'i>(n: Node<'a, 'i>, names: &[&str]) -> Vec<Node<'a, 'i>> {
    n.descendants().filter(|d| d.is_element() && names.iter().all(|name| d.children().any(|c| c.is_element() && local(&c) == *name))).collect()
}

/// Text of the first child with this name (else the first descendant).
fn text(n: Node, name: &str) -> String {
    n.children().find(|c| c.is_element() && local(c) == name).or_else(|| find(n, name)).and_then(|c| c.text()).unwrap_or("").trim().to_string()
}

fn int(n: Node, name: &str) -> u32 {
    text(n, name).parse().unwrap_or(0)
}

fn decimal(n: Node, name: &str) -> Option<f64> {
    text(n, name).parse().ok()
}

fn fault_string(body: &str) -> Option<String> {
    let doc = Document::parse(body).ok()?;
    find(doc.root(), "faultstring").and_then(|f| f.text()).map(|t| t.trim().to_string())
}

impl Client {
    pub fn new(username: &str, password: &str, transport: Box<dyn Transport>) -> Result<Client, RrError> {
        let app_key = app_key();
        if app_key.is_empty() {
            return Err(err("no RadioReference appKey (set P25_SURVEY_APPKEY)"));
        }
        Ok(Client {
            username: username.into(),
            password: password.into(),
            app_key,
            transport,
            systems: RefCell::default(),
            details: RefCell::default(),
            sites: RefCell::default(),
        })
    }

    fn envelope(&self, op: &str, params: &[(&str, String)]) -> String {
        let params: String = params.iter().map(|(k, v)| format!("<{k}>{}</{k}>", escape(v))).collect();
        format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{SOAP_NS}" xmlns:rr="{NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <rr:{op}>{params}<authInfo><username>{}</username><password>{}</password><appKey>{}</appKey><version>latest</version><style>rpc</style></authInfo></rr:{op}>
  </soap:Body>
</soap:Envelope>
"#,
            escape(&self.username),
            escape(&self.password),
            escape(&self.app_key)
        )
    }

    /// Call `op`; hand the response's SOAP Body to `read`.
    fn call<T>(&self, op: &str, params: &[(&str, String)], read: impl FnOnce(Node) -> Result<T, RrError>) -> Result<T, RrError> {
        let raw = self.transport.post(op, &self.envelope(op, params))?;
        if std::env::var_os("P25_SURVEY_RR_DEBUG").is_some() {
            eprintln!("--- RR {op} response ---\n{raw}\n--- end ---");
        }
        let doc = Document::parse(&raw).map_err(|e| err(format!("RR {op}: not XML: {e}")))?;
        if let Some(f) = find(doc.root(), "Fault") {
            let s = text(f, "faultstring");
            let s = if s.is_empty() { "(no faultstring)".to_string() } else { s };
            let l = s.to_lowercase();
            return Err(RrError { auth: l.contains("auth") || l.contains("login"), message: format!("RR {op}: {s}") });
        }
        let body = find(doc.root(), "Body").ok_or_else(|| err(format!("RR {op}: no SOAP Body")))?;
        read(body)
    }

    /// Check the login.
    pub fn user(&self) -> Result<User, RrError> {
        self.call("getUserData", &[], |body| {
            let info = with_children(body, &["username", "subExpireDate"]).into_iter().next().unwrap_or(body);
            let username = text(info, "username");
            if username.is_empty() {
                return Err(err("getUserData: no user in the response (P25_SURVEY_RR_DEBUG=1 shows it)"));
            }
            Ok(User { username, sub_expire_date: text(info, "subExpireDate") })
        })
    }

    /// Every RR system with this WACN and SYSID (RR sometimes has several:
    /// the same identity reused by neighbouring networks).
    pub fn systems(&self, wacn: &str, sysid: &str) -> Result<Vec<System>, RrError> {
        let key = format!("{}/{}", norm_hex(wacn), norm_hex(sysid));
        if let Some(s) = self.systems.borrow().get(&key) {
            return Ok(s.clone());
        }
        let sids: Vec<u32> = self.call("getTrsBySysid", &[("sysid", sysid.to_ascii_uppercase())], |body| {
            Ok(with_children(body, &["sid"]).into_iter().filter_map(|n| text(n, "sid").parse().ok()).collect())
        })?;
        let mut out = Vec::new();
        for sid in sids {
            let s = self.system(sid)?;
            if s.has_wacn_sysid(wacn, sysid) {
                out.push(s);
            }
        }
        self.systems.borrow_mut().insert(key, out.clone());
        Ok(out)
    }

    pub fn system(&self, sid: u32) -> Result<System, RrError> {
        if let Some(s) = self.details.borrow().get(&sid) {
            return Ok(s.clone());
        }
        let s: System = self.call("getTrsDetails", &[("sid", sid.to_string())], |body| {
            let trs = find(body, "return").or_else(|| with_children(body, &["sName"]).into_iter().next()).unwrap_or(body);
            let sysids = trs
                .children()
                .find(|c| c.is_element() && local(c) == "sysid")
                .map(|arr| {
                    with_children(arr, &["sysid"])
                        .into_iter()
                        .filter(|item| *item != arr)
                        .map(|item| SysidEntry {
                            wacn: [text(item, "wacn"), text(item, "WACN")].into_iter().find(|w| !w.is_empty()).unwrap_or_default(),
                            sysid: text(item, "sysid"),
                        })
                        .filter(|e| !e.sysid.is_empty())
                        .collect()
                })
                .unwrap_or_default();
            Ok(System { sid, name: text(trs, "sName"), sysids })
        })?;
        self.details.borrow_mut().insert(sid, s.clone());
        Ok(s)
    }

    pub fn sites(&self, sid: u32) -> Result<Vec<Site>, RrError> {
        if let Some(s) = self.sites.borrow().get(&sid) {
            return Ok(s.clone());
        }
        let sites: Vec<Site> = self.call("getTrsSites", &[("sid", sid.to_string())], |body| {
            Ok(with_children(body, &["rfss", "siteNumber"])
                .into_iter()
                .map(|site| {
                    let frequencies = site
                        .children()
                        .find(|c| c.is_element() && local(c) == "siteFreqs")
                        .map(|arr| {
                            with_children(arr, &["freq"])
                                .into_iter()
                                .filter_map(|f| Some(SiteFreq { freq_hz: (decimal(f, "freq")? * 1e6).round() as u64, use_code: text(f, "use") }))
                                .collect()
                        })
                        .unwrap_or_default();
                    let licenses = site
                        .children()
                        .find(|c| c.is_element() && local(c) == "siteLicenses")
                        .map(|arr| with_children(arr, &["license"]).into_iter().map(|l| text(l, "license")).filter(|l| !l.is_empty()).collect())
                        .unwrap_or_default();
                    Site {
                        rfss: int(site, "rfss"),
                        site_number: int(site, "siteNumber"),
                        nac: text(site, "nac"),
                        description: text(site, "siteDescr"),
                        location: text(site, "siteLocation"),
                        county: text(site, "siteCt"),
                        lat: decimal(site, "lat"),
                        lon: decimal(site, "lon"),
                        frequencies,
                        licenses,
                    }
                })
                .collect())
        })?;
        self.sites.borrow_mut().insert(sid, sites.clone());
        Ok(sites)
    }
}

/// What a getUserData expiry says of the subscription.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Subscription {
    Active(String),
    /// "Never" (lifetime, admin).
    Lifetime,
    Expired(String),
    /// Missing, unreadable, or 01-01-1970 (a PHP timestamp of 0: no date
    /// stored, seen on accounts whose lookups work).
    Unknown(String),
}

/// RR renders the expiry MM-DD-YYYY (ISO YYYY-MM-DD also taken). `today`:
/// (year, month, day).
pub fn subscription(expiry: &str, today: (i32, u32, u32)) -> Subscription {
    let s = expiry.trim();
    if s.is_empty() {
        return Subscription::Unknown("RR gave no subscription expiry".into());
    }
    if s.to_ascii_lowercase().starts_with("never") {
        return Subscription::Lifetime;
    }
    let digits: Vec<&str> = s.split(|c: char| !c.is_ascii_digit()).filter(|p| !p.is_empty()).collect();
    let date = match digits.as_slice() {
        [y, m, d, ..] if y.len() == 4 => (y.parse().ok(), m.parse().ok(), d.parse().ok()),
        [m, d, y, ..] if y.len() == 4 => (y.parse().ok(), m.parse().ok(), d.parse().ok()),
        _ => (None, None, None),
    };
    let (Some(y), Some(m), Some(d)) = date else {
        return Subscription::Unknown(format!("couldn't read the expiry {s:?}"));
    };
    let iso = format!("{y:04}-{m:02}-{d:02}");
    if (y, m, d) == (1970, 1, 1) {
        Subscription::Unknown(format!("expiry {iso} (none stored)"))
    } else if (y, m, d) < today {
        Subscription::Expired(iso)
    } else {
        Subscription::Active(iso)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// Answers each operation with a canned body; `getTrsDetails` by sid.
    pub struct Canned(pub Vec<(&'static str, Option<&'static str>, String)>);

    impl Transport for Canned {
        fn post(&self, op: &str, envelope: &str) -> Result<String, RrError> {
            self.0
                .iter()
                .find(|(o, sid, _)| *o == op && sid.is_none_or(|s| envelope.contains(&format!("<sid>{s}</sid>"))))
                .map(|(_, _, body)| format!(r#"<soap:Envelope xmlns:soap="{SOAP_NS}"><soap:Body>{body}</soap:Body></soap:Envelope>"#))
                .ok_or_else(|| err(format!("unexpected {op}")))
        }
    }

    pub fn client(answers: Vec<(&'static str, Option<&'static str>, String)>) -> Client {
        Client::new("u", "p", Box::new(Canned(answers))).unwrap()
    }

    #[test]
    fn user() {
        let c = client(vec![(
            "getUserData",
            None,
            "<getUserDataReturn><username>radiouser</username><subExpireDate>2027-01-01T00:00:00</subExpireDate></getUserDataReturn>".into(),
        )]);
        assert_eq!(c.user().unwrap(), User { username: "radiouser".into(), sub_expire_date: "2027-01-01T00:00:00".into() });
    }

    #[test]
    fn a_fault_about_the_login_is_an_auth_error() {
        let c = client(vec![("getUserData", None, "<soap:Fault><faultcode>x</faultcode><faultstring>Invalid login</faultstring></soap:Fault>".into())]);
        let e = c.user().unwrap_err();
        assert!(e.auth, "{e}");
    }

    #[test]
    fn systems_filtered_by_wacn() {
        let c = client(vec![
            ("getTrsBySysid", None, "<getTrsBySysidReturn><TrsListDef><sid>100</sid></TrsListDef><TrsListDef><sid>200</sid></TrsListDef></getTrsBySysidReturn>".into()),
            ("getTrsDetails", Some("100"), "<getTrsDetailsReturn><sName>Other System</sName><sysid><TrsSysid><wacn>123AB</wacn><sysid>1A4</sysid></TrsSysid></sysid></getTrsDetailsReturn>".into()),
            ("getTrsDetails", Some("200"), "<getTrsDetailsReturn><sName>Our System</sName><sysid><TrsSysid><wacn>BEE00</wacn><sysid>1A4</sysid></TrsSysid></sysid></getTrsDetailsReturn>".into()),
        ]);
        let s = c.systems("BEE00", "1A4").unwrap();
        assert_eq!(s.len(), 1);
        assert_eq!((s[0].sid, s[0].name.as_str()), (200, "Our System"));
        // Cached: a second ask makes no calls (the canned transport would fail on none).
        assert_eq!(c.systems("bee00", "01a4").unwrap().len(), 1);
    }

    #[test]
    fn sites_and_frequencies() {
        let c = client(vec![(
            "getTrsSites",
            None,
            r#"<return><item><siteId>9</siteId><siteNumber>7</siteNumber><rfss>1</rfss><nac>293</nac><siteDescr>Downtown</siteDescr>
               <siteLocation>Main St</siteLocation><siteCt>Dallas</siteCt><lat>32.78</lat><lon>-96.80</lon>
               <siteFreqs><item><freq>851.0125</freq><use>d</use></item><item><freq>852.5</freq><use></use></item></siteFreqs>
               <siteLicenses><item><license>WQAB123</license></item></siteLicenses></item></return>"#
                .into(),
        )]);
        let s = c.sites(42).unwrap();
        assert_eq!(s.len(), 1);
        assert_eq!((s[0].rfss, s[0].site_number, s[0].nac.as_str(), s[0].county.as_str()), (1, 7, "293", "Dallas"));
        assert_eq!(s[0].control_freqs().collect::<Vec<_>>(), vec![851_012_500]);
        assert_eq!(s[0].frequencies.len(), 2);
        assert_eq!(s[0].licenses, vec!["WQAB123".to_string()]);
    }

    #[test]
    fn subscriptions() {
        let today = (2026, 11, 15);
        assert_eq!(subscription("10-03-2018", today), Subscription::Expired("2018-10-03".into()));
        assert_eq!(subscription("12-31-2027", today), Subscription::Active("2027-12-31".into()));
        assert_eq!(subscription("2027-01-01T00:00:00", today), Subscription::Active("2027-01-01".into()));
        assert_eq!(subscription("Never - Admin", today), Subscription::Lifetime);
        assert!(matches!(subscription("01-01-1970", today), Subscription::Unknown(_)));
        assert!(matches!(subscription("", today), Subscription::Unknown(_)));
    }

    #[test]
    fn hex_compares_without_leading_zeros() {
        let s = System { sid: 1, name: "x".into(), sysids: vec![SysidEntry { wacn: "BEE00".into(), sysid: "01A4".into() }] };
        assert!(s.has_wacn_sysid("bee00", "1a4"));
        assert!(!s.has_wacn_sysid("BEE00", "1A5"));
    }
}
