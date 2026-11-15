//! p25-survey. For now, tools on recordings:
//!   p25-survey info <recording>
//!   p25-survey decode <recording> --freq MHz[,MHz…] [--seconds S]

use std::process::ExitCode;
use std::time::Instant;

use survey_core::dsp::{Channel, Discriminator};
use survey_core::p25::framer::{Event, Framer};
use survey_core::p25::nid::duid;
use survey_core::p25::{SiteInfo, parse_tsbk};
use survey_radio::sigmf::Recording;

const USAGE: &str = "usage:
  p25-survey info <recording>
  p25-survey decode <recording> --freq MHz[,MHz...] [--seconds S] [--json]
(a recording is a SigMF .sigmf-meta / .sigmf-data, or its name without either)";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let r = match args.first().map(String::as_str) {
        Some("info") if args.len() == 2 => info(&args[1]),
        Some("decode") if args.len() >= 2 => decode(&args[1], &args[2..]),
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
fn opt<'a>(rest: &'a [String], name: &str) -> Option<&'a str> {
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

/// One frequency being decoded.
struct Rx {
    freq_hz: f64,
    channel: Channel,
    disc: Discriminator,
    framer: Framer,
    site: SiteInfo,
    iq: Vec<num_complex::Complex32>,
    hz: Vec<f32>,
    events: Vec<Event>,
}

/// Decode the control channels at `--freq` from a recording: C4FM.
fn decode(path: &str, rest: &[String]) -> Result<(), String> {
    let r = Recording::open(path).map_err(|e| e.to_string())?;
    let freqs: Vec<f64> = opt(rest, "--freq")
        .ok_or("--freq is needed")?
        .split(',')
        .map(|f| f.trim().parse::<f64>().map(|m| (m * 1e6).round()).map_err(|_| format!("--freq: {f}?")))
        .collect::<Result<_, _>>()?;
    let seconds: f64 = opt(rest, "--seconds").map_or(Ok(f64::INFINITY), |s| s.parse().map_err(|_| "--seconds: a number".to_string()))?;
    let mut rxs = freqs
        .iter()
        .map(|&f| {
            let channel = Channel::new(r.rate_hz, f - r.center_hz)?;
            let sps = channel.samples_per_symbol();
            Ok(Rx {
                freq_hz: f,
                disc: Discriminator::new(channel.output_rate(), sps.round() as usize),
                framer: Framer::new(sps),
                channel,
                site: SiteInfo::default(),
                iq: Vec::new(),
                hz: Vec::new(),
                events: Vec::new(),
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    let t0 = Instant::now();
    let mut rd = r.reader().map_err(|e| e.to_string())?;
    let mut block = Vec::new();
    let limit = (seconds * r.rate_hz).min(r.samples as f64) as u64;
    let mut done = 0u64;
    while done < limit && rd.read(&mut block, (1 << 18).min((limit - done) as usize)).map_err(|e| e.to_string())? > 0 {
        done += block.len() as u64;
        for rx in &mut rxs {
            rx.iq.clear();
            rx.channel.process(&block, &mut rx.iq);
            rx.hz.clear();
            rx.disc.process(&rx.iq, &mut rx.hz);
            rx.events.clear();
            rx.framer.push(&rx.hz, &mut rx.events);
            for e in &rx.events {
                match *e {
                    Event::Frame { nid, .. } => {
                        rx.site.nac_heard(nid.nac);
                    }
                    Event::Tsbk { octets, .. } => {
                        if let Some(b) = parse_tsbk(&octets) {
                            rx.site.push(b);
                        }
                    }
                }
            }
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
    let s = &rx.framer.stats;
    let site = &rx.site;
    serde_json::json!({
        "freq_hz": rx.freq_hz as u64,
        "air_s": air_s,
        "syncs": s.syncs,
        "frames": s.frames_total(),
        "tsdu": s.frames[duid::TSDU as usize],
        "tsbk_ok": s.tsbk_ok,
        "tsbk_bad": s.tsbk_bad,
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
    let s = &rx.framer.stats;
    let site = &rx.site;
    let total = s.tsbk_ok + s.tsbk_bad;
    println!("\n{:.5} MHz", rx.freq_hz / 1e6);
    println!(
        "  syncs {}  frames {} (TSDU {}, voice {}, other {})  NID errors/frame {:.2}",
        s.syncs,
        s.frames_total(),
        s.frames[duid::TSDU as usize],
        s.frames[duid::LDU1 as usize] + s.frames[duid::LDU2 as usize] + s.frames[duid::HDU as usize],
        s.frames_total() - s.frames[duid::TSDU as usize] - s.frames[duid::LDU1 as usize] - s.frames[duid::LDU2 as usize] - s.frames[duid::HDU as usize],
        s.nid_errors as f64 / s.frames_total().max(1) as f64
    );
    println!(
        "  TSBKs {} of {} passed CRC ({:.1} %), trellis errors/block {:.1}, site broadcasts {}",
        s.tsbk_ok,
        total,
        100.0 * s.tsbk_ok as f64 / total.max(1) as f64,
        s.trellis_errors as f64 / total.max(1) as f64,
        site.broadcasts
    );
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
