//! p25-survey. For now, tools on recordings:
//!   p25-survey info <recording>
//!   p25-survey decode <recording> --freq MHz[,MHz…] [--seconds S]

mod enrich;
mod record;
mod report;
mod rows;
mod rr;
mod scan;

use std::process::ExitCode;
use std::time::Instant;

use survey_core::dsp::{Channelizer, HeadId};
use survey_core::p25::nid::duid;
use survey_core::p25::receiver::{ControlChannel, KINDS, Kind};
use survey_radio::sigmf::Recording;

const USAGE: &str = "usage:
  p25-survey info <recording>
  p25-survey decode <recording> --freq MHz[,MHz...] [--seconds S] [--json]
  p25-survey detect <recording> [--at S] [--look S] [--threshold dB] [--levels MHz,MHz]
  p25-survey scan --radio KIND[:SERIAL] --start MHz --stop MHz [--gain N|auto] [--rate MS/s]
                  [scan options]
      KIND: airspy, hydrasdr (--gain: linearity preset 0-21; --sensitivity 0-21)
            rtlsdr (--gain: tuner dB; default AGC)
            hackrf (--gain: LNA dB 0-40; --vga 0-62, default 24; --amp: the 14 dB amplifier)
  p25-survey scan <recording>... [--start MHz] [--stop MHz] [scan options]
      scan options: [--threshold dB] [--max-dwell S] [--output survey.json [--resume]] [--hide-no-cc]
                    [--ppm N]  (the radio's error, as the report gives it; a radio only)
                    [--auto-gain [--gain-sweep 6,9,12,15,18]]  (a radio only)
                    [--rr [--rr-stdin]]  (look each control channel up in RadioReference;
                    --rr-stdin: username and password on standard input, not asked)
  p25-survey report <survey.json> [--rr [--rr-stdin]]   (the reports again; --rr: look it up now)
  p25-survey radios
(a recording is a SigMF .sigmf-meta / .sigmf-data, or its name without either)";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let r = match args.first().map(String::as_str) {
        Some("info") if args.len() == 2 => info(&args[1]),
        Some("decode") if args.len() >= 2 => decode(&args[1], &args[2..]),
        Some("detect") if args.len() >= 2 => detect(&args[1], &args[2..]),
        Some("scan") => scan::scan(&args[1..]),
        Some("radios") => scan::radios(),
        Some("gains") => match opt(&args[1..], "--radio").map(str::to_string).map_or_else(scan::first_radio, Ok) {
            Ok(spec) => scan::list_gains(&spec, &args[1..]),
            Err(e) => Err(e),
        },
        Some("report") if args.len() >= 2 => report_cmd(&args[1], &args[2..]),
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("p25-survey: {e}");
            ExitCode::FAILURE
        }
    }
}

/// `--name value` from `rest`.
pub(crate) fn opt<'a>(rest: &'a [String], name: &str) -> Option<&'a str> {
    rest.iter().position(|a| a == name).and_then(|i| rest.get(i + 1)).map(String::as_str)
}

/// Metadata, then RMS and peak level (dBFS) and the share of samples near
/// full scale, per second.
fn info(path: &str) -> Result<(), String> {
    let r = Recording::open(path).map_err(|e| e.to_string())?;
    println!("{}", r.data.display());
    println!("  {} at {:.3} MS/s, centre {:.6} MHz, {:.2} s ({} samples)", r.datatype.as_str(), r.rate_hz / 1e6, r.center_hz / 1e6, r.seconds(), r.samples);
    for (k, v) in [("recorded", &r.datetime), ("hardware", &r.hw), ("about", &r.description)] {
        if let Some(v) = v {
            println!("  {k}: {v}");
        }
    }
    println!("  second   rms dBFS   peak dBFS   >-1 dBFS");
    let per = r.rate_hz.round() as usize;
    let mut rd = r.reader().map_err(|e| e.to_string())?;
    let mut block = Vec::new();
    let mut second = 0;
    while rd.read(&mut block, per).map_err(|e| e.to_string())? > 0 {
        let (mut power, mut peak, mut hot) = (0f64, 0f32, 0usize);
        for s in &block {
            power += s.norm_sqr() as f64;
            peak = peak.max(s.re.abs()).max(s.im.abs());
            hot += (s.re.abs() > 0.891 || s.im.abs() > 0.891) as usize;
        }
        let rms = (power / block.len() as f64).sqrt();
        println!("  {second:>6}   {:>8.1}   {:>9.1}   {:>7.3}%", 20.0 * rms.log10(), 20.0 * peak.log10(), 100.0 * hot as f64 / block.len() as f64);
        second += 1;
    }
    Ok(())
}

/// Log in to RadioReference: asked for every time, never stored. With
/// `--rr-stdin` the username and password are the first two lines of
/// standard input instead (for scripts: nothing on the command line).
pub(crate) fn rr_login(rest: &[String]) -> Result<rr::Client, String> {
    use std::io::{BufRead, Write};
    let (user, password) = if rest.iter().any(|a| a == "--rr-stdin") {
        let mut lines = std::io::stdin().lock().lines();
        let mut next = || lines.next().transpose().map_err(|e| e.to_string()).map(Option::unwrap_or_default);
        (next()?, next()?)
    } else {
        println!("RadioReference login (not stored)");
        print!("  Username: ");
        std::io::stdout().flush().map_err(|e| e.to_string())?;
        let mut user = String::new();
        std::io::stdin().read_line(&mut user).map_err(|e| e.to_string())?;
        (user, rpassword::prompt_password("  Password: ").map_err(|e| e.to_string())?)
    };
    let client = rr::Client::new(user.trim(), password.trim_end_matches(['\r', '\n']), Box::new(rr::Https::new())).map_err(|e| e.to_string())?;
    match client.user() {
        Ok(u) => match rr::subscription(&u.sub_expire_date, record::today()) {
            rr::Subscription::Expired(on) => Err(format!(
                "RadioReference Premium subscription required for --rr (expired {on}).\n  Account {} logged in, but the subscription isn't current.\n  Renew at https://www.radioreference.com/apps/content/?cid=3, or run without --rr.",
                u.username
            )),
            rr::Subscription::Unknown(why) => {
                println!("RadioReference: logged in as {} (warning: {why}; lookups may fail if the subscription isn't current)", u.username);
                Ok(client)
            }
            rr::Subscription::Lifetime => {
                println!("RadioReference: logged in as {} (no expiry)", u.username);
                Ok(client)
            }
            rr::Subscription::Active(until) => {
                println!("RadioReference: logged in as {} (subscription until {until})", u.username);
                Ok(client)
            }
        },
        Err(e) if e.auth => Err(format!("RadioReference refused the login: {e}")),
        Err(e) => Err(format!("RadioReference: {e}")),
    }
}

/// Where a survey's reports go: beside it, .txt and -submissions.md.
pub(crate) fn report_paths(survey: &std::path::Path) -> (std::path::PathBuf, std::path::PathBuf) {
    let stem = survey.with_extension("");
    (stem.with_extension("txt"), std::path::PathBuf::from(format!("{}-submissions.md", stem.display())))
}

/// Write the text report, and the submissions report if anything was looked up.
pub(crate) fn write_reports(survey: &std::path::Path, rows: &[rows::Row]) -> Result<(), String> {
    let (txt, md) = report_paths(survey);
    std::fs::write(&txt, report::text(rows)).map_err(|e| format!("{}: {e}", txt.display()))?;
    println!("Report: {}", txt.display());
    if rows.iter().any(|r| r.rr.is_some()) {
        std::fs::write(&md, report::submissions(rows)).map_err(|e| format!("{}: {e}", md.display()))?;
        println!("Submissions: {}", md.display());
    }
    Ok(())
}

/// `p25-survey report survey.json [--rr]`.
fn report_cmd(path: &str, rest: &[String]) -> Result<(), String> {
    let path = std::path::Path::new(path);
    let mut rows = rows::read(path)?;
    rows::keep_latest(&mut rows);
    if rest.iter().any(|a| a == "--rr") {
        let client = rr_login(rest)?;
        let mut n = 0;
        let all = rows.clone();
        for r in rows.iter_mut().filter(|r| r.is_control()) {
            r.rr = Some(enrich::enrich_in(r, &all, &client));
            n += 1;
        }
        rows::write(path, &rows)?;
        println!("Looked up {n} control channel(s); {} updated.", path.display());
    }
    write_reports(path, &rows)
}

/// The channels that stand out in a look at a recording (the scan's first step).
fn detect(path: &str, rest: &[String]) -> Result<(), String> {
    use survey_core::dsp::detect::{DetectConfig, Detector};
    let r = Recording::open(path).map_err(|e| e.to_string())?;
    let num = |name: &str, default: f64| opt(rest, name).map_or(Ok(default), |v| v.parse::<f64>().map_err(|_| format!("{name}: a number")));
    let (at, look) = (num("--at", 0.5)?, num("--look", 0.35)?);
    let cfg = DetectConfig { threshold_db: num("--threshold", DetectConfig::default().threshold_db)?, ..Default::default() };
    let mut rd = r.reader().map_err(|e| e.to_string())?;
    let mut block = Vec::new();
    let mut skip = (at * r.rate_hz) as usize;
    while skip > 0 {
        let n = rd.read(&mut block, skip.min(1 << 18)).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("the recording ends first".into());
        }
        skip -= n;
    }
    let mut det = Detector::new(r.rate_hz);
    rd.read(&mut block, (look * r.rate_hz) as usize).map_err(|e| e.to_string())?;
    det.push(&block);
    let half = r.rate_hz / 2.0 * 0.85;
    if let Some(range) = opt(rest, "--levels") {
        let (a, b) = range.split_once(',').ok_or("--levels: MHz,MHz")?;
        let (a, b): (f64, f64) = (a.parse().map_err(|_| "--levels: MHz,MHz")?, b.parse().map_err(|_| "--levels: MHz,MHz")?);
        for (f, l, p, fl) in det.levels(r.center_hz, a * 1e6, b * 1e6, &cfg) {
            println!("  {:.5}  level {l:6.1}  power {p:6.1}  floor {fl:6.1}", f / 1e6);
        }
        return Ok(());
    }
    let found = det.find(r.center_hz, r.center_hz - half, r.center_hz + half, &cfg);
    println!("{} channel(s) {:.1} dB or more over the floor, {:.2} s from {:.2} s:", found.len(), cfg.threshold_db, look, at);
    for f in &found {
        println!("  {:.5} MHz  {:5.1} dB", f.freq_hz / 1e6, f.level_db);
    }
    Ok(())
}

/// One frequency being decoded.
struct Rx {
    freq_hz: f64,
    head: HeadId,
    cc: ControlChannel,
    iq: Vec<num_complex::Complex32>,
    tsbks: Vec<survey_core::p25::receiver::Tsbk>,
}

/// Decode the control channels at `--freq` from a recording, with both receivers.
fn decode(path: &str, rest: &[String]) -> Result<(), String> {
    let r = Recording::open(path).map_err(|e| e.to_string())?;
    let freqs: Vec<f64> = opt(rest, "--freq")
        .ok_or("--freq is needed")?
        .split(',')
        .map(|f| f.trim().parse::<f64>().map(|m| (m * 1e6).round()).map_err(|_| format!("--freq: {f}?")))
        .collect::<Result<_, _>>()?;
    let seconds: f64 = opt(rest, "--seconds").map_or(Ok(f64::INFINITY), |s| s.parse().map_err(|_| "--seconds: a number".to_string()))?;
    let mut chz = Channelizer::new(r.rate_hz)?;
    let mut rxs: Vec<Rx> = freqs
        .iter()
        .map(|&f| Rx {
            freq_hz: f,
            head: chz.add(f - r.center_hz),
            cc: ControlChannel::new(chz.output_rate(), chz.samples_per_symbol()),
            iq: Vec::new(),
            tsbks: Vec::new(),
        })
        .collect();
    let t0 = Instant::now();
    let mut rd = r.reader().map_err(|e| e.to_string())?;
    let mut block = Vec::new();
    let limit = (seconds * r.rate_hz).min(r.samples as f64) as u64;
    let mut done = 0u64;
    while done < limit && rd.read(&mut block, (1 << 18).min((limit - done) as usize)).map_err(|e| e.to_string())? > 0 {
        done += block.len() as u64;
        chz.process(&block);
        for rx in &mut rxs {
            chz.take(rx.head, &mut rx.iq);
            rx.tsbks.clear();
            rx.cc.push(&rx.iq, &mut rx.tsbks);
        }
    }
    let air = done as f64 / r.rate_hz;
    if rest.iter().any(|a| a == "--json") {
        for rx in &rxs {
            println!("{}", json(rx, air));
        }
        return Ok(());
    }
    println!("{:.1} s of air in {:.1} s", air, t0.elapsed().as_secs_f64());
    for rx in &rxs {
        report(rx);
    }
    Ok(())
}

/// One line per channel, for comparing runs (corpus/compare.py).
fn json(rx: &Rx, air_s: f64) -> serde_json::Value {
    let site = &rx.cc.site;
    let per = |k: Kind| {
        let s = rx.cc.stats(k);
        serde_json::json!({"syncs": s.syncs, "frames": s.frames_total(), "tsbk_ok": s.tsbk_ok, "tsbk_bad": s.tsbk_bad, "first": rx.cc.first[k as usize]})
    };
    serde_json::json!({
        "freq_hz": rx.freq_hz as u64,
        "air_s": air_s,
        "tsbks": rx.cc.tsbks,
        "c4fm": per(Kind::C4fm),
        "cqpsk": per(Kind::Cqpsk),
        "offset_hz": rx.cc.offset_hz(),
        "broadcasts": site.broadcasts,
        "nac": site.nac.map(|v| format!("{v:03X}")),
        "wacn": site.wacn.map(|v| format!("{v:05X}")),
        "sysid": site.sysid.map(|v| format!("{v:03X}")),
        "rfss_id": site.rfss,
        "site_id": site.site,
        "announced_hz": site.announced_hz(),
        "idens": site.plan.len(),
        "neighbors": site.neighbors().filter_map(|(n, f)| Some(serde_json::json!({"freq_hz": f?, "rfss_id": n.rfss, "site_id": n.site}))).collect::<Vec<_>>(),
        "secondary_cc": site.secondary().filter_map(|(_, f)| f).collect::<Vec<_>>(),
    })
}

fn report(rx: &Rx) {
    let site = &rx.cc.site;
    println!("\n{:.5} MHz  (carrier {:+.0} Hz)", rx.freq_hz / 1e6, rx.cc.offset_hz());
    for k in KINDS {
        let s = rx.cc.stats(k);
        let total = s.tsbk_ok + s.tsbk_bad;
        println!(
            "  {:<5}  frames {:>5} (TSDU {:>5})  NID errors/frame {:.2}  TSBKs {:>5} of {:>5} ({:5.1} %), first {:>5}, trellis errors/block {:.1}",
            format!("{k:?}"),
            s.frames_total(),
            s.frames[duid::TSDU as usize],
            s.nid_errors as f64 / s.frames_total().max(1) as f64,
            s.tsbk_ok,
            total,
            100.0 * s.tsbk_ok as f64 / total.max(1) as f64,
            rx.cc.first[k as usize],
            s.trellis_errors as f64 / total.max(1) as f64,
        );
    }
    println!("  TSBKs {}, site broadcasts {}", rx.cc.tsbks, site.broadcasts);
    let votes: Vec<String> = site.nac_votes().map(|(n, v)| format!("{n:03X} x{v}")).collect();
    println!("  NACs heard: {}", votes.join(", "));
    let hex = |v: Option<u32>, w: usize| v.map_or("-".to_string(), |v| format!("{v:0w$X}"));
    println!(
        "  NAC {}  WACN {}  system {}  RFSS {}  site {}",
        hex(site.nac.map(u32::from), 3),
        hex(site.wacn, 5),
        hex(site.sysid.map(u32::from), 3),
        site.rfss.map_or("-".into(), |v| v.to_string()),
        site.site.map_or("-".into(), |v| v.to_string())
    );
    if let Some(a) = site.announced_hz() {
        let note = if a as f64 == rx.freq_hz { String::new() } else { "  (not where it was heard)".into() };
        println!("  announces {:.5} MHz{note}", a as f64 / 1e6);
    }
    for i in site.plan.entries() {
        println!(
            "  iden {:>2}: {:.5} MHz, {} kHz steps, offset {:+.4} MHz{}",
            i.iden,
            i.base_hz as f64 / 1e6,
            i.step_hz as f64 / 1e3,
            i.offset_hz as f64 / 1e6,
            if i.tdma { format!(", TDMA x{}", i.slots) } else { String::new() }
        );
    }
    for (n, f) in site.neighbors() {
        println!("  neighbour {}-{} (system {:03X}): {}", n.rfss, n.site, n.sysid, f.map_or("?".into(), |f| format!("{:.5} MHz", f as f64 / 1e6)));
    }
    for (_, f) in site.secondary() {
        println!("  secondary: {}", f.map_or("?".into(), |f| format!("{:.5} MHz", f as f64 / 1e6)));
    }
}
