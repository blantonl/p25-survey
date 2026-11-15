//! `p25-survey scan`: the survey, on a radio or on recordings.
//!
//! On recordings each one is a hop: when the survey asks to tune to a
//! centre, the recording made there is played (the four 800 MHz recordings in
//! the corpus make a sweep of 851-869 MHz). On a radio the hops are planned
//! to cover --start to --stop.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::time::{Duration, Instant};

use num_complex::Complex32;
use survey_core::survey::{Command, GainSweep, Output, Record, Survey, SurveyConfig, Verdict, best_gain, gain_scores, mark_copies};
use survey_radio::airspy::Airspy;
use survey_radio::sigmf::{Reader, Recording};
use survey_radio::{Gain, Radio};

use crate::record::{to_json, verdict_str};

/// Where the samples come from.
enum Source {
    Recordings { recs: Vec<Recording>, reader: Option<Reader> },
    Radio(Box<dyn Radio>),
}

/// The next samples, or None when they've run out (a recording ended).
enum Read {
    Samples(usize),
    Nothing,
    Ended,
}

impl Source {
    fn rate_hz(&self) -> f64 {
        match self {
            Source::Recordings { recs, .. } => recs[0].rate_hz,
            Source::Radio(r) => r.rate_hz(),
        }
    }

    fn name(&self) -> &'static str {
        match self {
            Source::Recordings { .. } => "file",
            Source::Radio(_) => "airspy",
        }
    }

    fn tune(&mut self, hz: f64) -> Result<(), String> {
        match self {
            Source::Recordings { recs, reader } => {
                let r = recs.iter().find(|r| r.center_hz == hz).ok_or("no recording at a hop's centre")?;
                *reader = Some(r.reader().map_err(|e| e.to_string())?);
                Ok(())
            }
            Source::Radio(r) => r.tune(hz).map_err(|e| e.to_string()),
        }
    }

    fn set_gain(&mut self, g: Gain) -> Result<(), String> {
        match self {
            Source::Recordings { .. } => Ok(()),
            Source::Radio(r) => r.set_gain(g).map_err(|e| e.to_string()),
        }
    }

    fn read(&mut self, block: &mut Vec<Complex32>) -> Result<Read, String> {
        match self {
            Source::Recordings { reader, .. } => match reader.as_mut() {
                Some(rd) => match rd.read(block, 1 << 18).map_err(|e| e.to_string())? {
                    0 => {
                        *reader = None;
                        Ok(Read::Ended)
                    }
                    n => Ok(Read::Samples(n)),
                },
                None => Ok(Read::Ended),
            },
            Source::Radio(r) => match r.read(block, Duration::from_secs(2)).map_err(|e| e.to_string())? {
                0 => Ok(Read::Nothing),
                n => Ok(Read::Samples(n)),
            },
        }
    }
}

/// `--gain`: Airspy linearity preset 0-21 (or `auto`); `--sensitivity N`
/// for the sensitivity presets instead.
fn gain(rest: &[String]) -> Result<Gain, String> {
    if let Some(s) = crate::opt(rest, "--sensitivity") {
        return s.parse().map(Gain::Sensitivity).map_err(|_| "--sensitivity: 0-21".into());
    }
    match crate::opt(rest, "--gain") {
        None => Ok(Gain::Linearity(14)),
        Some("auto") => Ok(Gain::Auto),
        Some(g) => g.parse().map(Gain::Linearity).map_err(|_| "--gain: 0-21 (Airspy linearity preset) or auto".into()),
    }
}

/// The gain the records were made at, as a number (Python's sdr_gain_db).
fn gain_number(source: &Source, g: Gain) -> Option<f64> {
    match (source, g) {
        (Source::Radio(_), Gain::Linearity(v) | Gain::Sensitivity(v)) => Some(v as f64),
        _ => None,
    }
}

/// `--radio airspy` or `airspy:SERIAL` (hex, as `p25-survey radios` lists).
fn open_radio(spec: &str, rest: &[String]) -> Result<Box<dyn Radio>, String> {
    let (kind, serial) = spec.split_once(':').map_or((spec, None), |(k, s)| (k, Some(s)));
    let serial = serial.map(|s| u64::from_str_radix(s.trim_start_matches("0x"), 16).map_err(|_| format!("--radio: serial {s}?"))).transpose()?;
    let max_rate = crate::opt(rest, "--rate").map_or(Ok(6e6), |r| r.parse::<f64>().map(|m| m * 1e6).map_err(|_| "--rate: MS/s".to_string()))?;
    match kind {
        "airspy" => Ok(Box::new(Airspy::open(serial, max_rate, gain(rest)?).map_err(|e| e.to_string())?)),
        _ => Err(format!("--radio: {kind}? (airspy)")),
    }
}

pub fn scan(args: &[String]) -> Result<(), String> {
    let paths: Vec<&String> = args.iter().take_while(|a| !a.starts_with("--")).collect();
    let rest = &args[paths.len()..];
    let num = |name: &str| crate::opt(rest, name).map(|v| v.parse::<f64>().map_err(|_| format!("{name}: a number"))).transpose();
    let usable = 0.85;
    let (mut source, range, hops) = match (crate::opt(rest, "--radio"), paths.is_empty()) {
        (Some(spec), true) => {
            let (Some(start), Some(stop)) = (num("--start")?, num("--stop")?) else {
                return Err("scan: --start and --stop (MHz) with a radio".into());
            };
            (Source::Radio(open_radio(spec, rest)?), (start * 1e6, stop * 1e6), None)
        }
        (None, false) => {
            let mut recs: Vec<Recording> = paths.iter().map(|p| Recording::open(p).map_err(|e| e.to_string())).collect::<Result<_, _>>()?;
            recs.sort_by(|a, b| a.center_hz.total_cmp(&b.center_hz));
            let rate = recs[0].rate_hz;
            if recs.iter().any(|r| r.rate_hz != rate) {
                return Err("scan: the recordings' sample rates differ".into());
            }
            let span = (recs[0].center_hz - rate * usable / 2.0, recs[recs.len() - 1].center_hz + rate * usable / 2.0);
            let range = (num("--start")?.map_or(span.0, |m| m * 1e6), num("--stop")?.map_or(span.1, |m| m * 1e6));
            let hops = recs.iter().map(|r| r.center_hz).collect();
            (Source::Recordings { recs, reader: None }, range, Some(hops))
        }
        _ => return Err("scan: recordings, or --radio, not both".into()),
    };
    let rate = source.rate_hz();
    let mut cfg = SurveyConfig::new(rate, range);
    cfg.usable = usable;
    match hops {
        Some(h) => cfg.hops = h,
        // A live radio's queue and USB transfers still hold the old frequency.
        None => cfg.settle_s = 0.25,
    }
    if let Some(t) = num("--threshold")? {
        cfg.detect.threshold_db = t;
    }
    if let Some(t) = num("--max-dwell")? {
        cfg.max_dwell_s = t;
    }
    let user_gain = gain(rest)?;
    if rest.iter().any(|a| a == "--auto-gain") {
        if matches!(source, Source::Recordings { .. }) {
            return Err("scan: --auto-gain needs a radio".into());
        }
        cfg.gains = match crate::opt(rest, "--gain-sweep") {
            Some(g) => g.split(',').map(|v| v.trim().parse::<f32>().map_err(|_| format!("--gain-sweep: {v}?"))).collect::<Result<_, _>>()?,
            // Airspy linearity presets: the low end rarely decodes anything.
            None => vec![6.0, 9.0, 12.0, 15.0, 18.0],
        };
        if cfg.gains.len() < 2 {
            return Err("--gain-sweep: at least two gains".into());
        }
    }
    let mut out_file = match crate::opt(rest, "--output") {
        Some(p) => Some(BufWriter::new(File::create(p).map_err(|e| format!("{p}: {e}"))?)),
        None => None,
    };
    let hide = rest.iter().any(|a| a == "--hide-no-cc");

    println!(
        "Scanning {:.4}-{:.4} MHz in {} hop(s), {} at {:.1} MS/s, threshold {:.1} dB",
        range.0 / 1e6,
        range.1 / 1e6,
        cfg.hops.len(),
        match &source {
            Source::Recordings { recs, .. } => format!("{} recording(s)", recs.len()),
            Source::Radio(_) => "airspy".into(),
        },
        rate / 1e6,
        cfg.detect.threshold_db
    );
    let t0 = Instant::now();
    let mut survey = Survey::new(cfg);
    let hops = survey.config().hops.clone();
    let mut block = Vec::new();
    let mut out = Vec::new();
    let mut air = 0u64;
    let mut records: Vec<Record> = Vec::new();
    // A hop's records are written when the hop ends: a copy can't be told
    // from the channel it copies until both are in.
    let mut written = 0;
    let mut sweeps: Vec<GainSweep> = Vec::new();
    loop {
        while let Some(c) = survey.command() {
            match c {
                Command::Tune(hz) => {
                    source.tune(hz)?;
                    survey.tuned(hz, &mut out);
                }
                Command::Gain(g) => {
                    source.set_gain(g.map_or(user_gain, |g| Gain::Linearity(g.round().clamp(0.0, 21.0) as u8)))?;
                    survey.gain_applied();
                }
            }
        }
        if survey.is_done() {
            break;
        }
        match source.read(&mut block)? {
            Read::Samples(n) => {
                air += n as u64;
                survey.push(&block, &mut out);
            }
            Read::Nothing => eprintln!("p25-survey: no samples from the radio for 2 s"),
            Read::Ended => survey.end_of_samples(&mut out),
        }
        for o in out.drain(..) {
            match o {
                Output::Hop { index, center_hz } => {
                    flush(&mut records, &mut written, &hops, hide, (source.name(), gain_number(&source, user_gain)), out_file.as_mut())?;
                    println!("\nhop {} of {} at {:.4} MHz", index + 1, hops.len(), center_hz / 1e6);
                }
                Output::Candidates { found, .. } => println!("  {} candidate(s)", found.len()),
                Output::Record(r) => records.push(*r),
                Output::GainSweep(sw) => {
                    flush(&mut records, &mut written, &hops, hide, (source.name(), gain_number(&source, user_gain)), out_file.as_mut())?;
                    print_sweep(&sw);
                    sweeps.push(*sw);
                }
                Output::Done => flush(&mut records, &mut written, &hops, hide, (source.name(), gain_number(&source, user_gain)), out_file.as_mut())?,
            }
        }
    }
    let ccs: Vec<&Record> = records.iter().filter(|r| r.is_control()).collect();
    let copies = ccs.iter().filter(|r| r.copy_of.is_some()).count();
    println!(
        "\n{} candidate(s): {} control channel(s) ({} complete, {} a copy of another), {} other P25, {} not P25. {:.1} s of air in {:.1} s.",
        records.len(),
        ccs.len() - copies,
        ccs.iter().filter(|r| r.verdict == Verdict::Complete && r.copy_of.is_none()).count(),
        copies,
        records.iter().filter(|r| r.verdict == Verdict::NotControl).count(),
        records.iter().filter(|r| r.verdict == Verdict::NotP25).count(),
        air as f64 / rate,
        t0.elapsed().as_secs_f64()
    );
    if !sweeps.is_empty() {
        print_bands(&sweeps);
    }
    if let Source::Radio(r) = &source {
        let lost = r.dropped();
        if lost > 0 {
            println!("{lost} samples lost ({:.2} % of the air): the computer didn't keep up.", 100.0 * lost as f64 / (air + lost) as f64);
        }
    }
    Ok(())
}

/// The band a frequency is in, for the gain advice.
fn band(hz: f64) -> &'static str {
    match hz / 1e6 {
        f if (136.0..174.0).contains(&f) => "VHF",
        f if (380.0..512.0).contains(&f) => "UHF",
        f if (758.0..806.0).contains(&f) => "700 MHz",
        f if (806.0..870.0).contains(&f) => "800 MHz",
        f if (896.0..941.0).contains(&f) => "900 MHz",
        _ => "other",
    }
}

/// A hop's sweep: TSBKs per channel at each gain, and the score.
fn print_sweep(sw: &GainSweep) {
    let scores = sw.scores();
    println!("  gain sweep, {} control channel(s), {:.0} s each:", sw.freqs.len(), sw.seconds);
    print!("    {:>10}", "gain");
    for st in &sw.steps {
        print!("  {:>6}", st.gain);
    }
    println!();
    for (c, f) in sw.freqs.iter().enumerate() {
        print!("    {:>10.5}", f / 1e6);
        for st in &sw.steps {
            print!("  {:>6}", st.tsbks[c]);
        }
        println!();
    }
    print!("    {:>10}", "score");
    for sc in &scores {
        print!("  {:>5.0}%", 100.0 * sc);
    }
    println!();
    print!("    {:>10}", "clipped");
    for st in &sw.steps {
        print!("  {:>5.2}%", 100.0 * st.clipped);
    }
    println!();
    match sw.best() {
        Some(g) => println!("    best here: {g}"),
        None => println!("    no gain decoded anything"),
    }
}

/// Per band: every hop's channels together, scored per gain.
fn print_bands(sweeps: &[GainSweep]) {
    let mut bands: Vec<&str> = sweeps.iter().map(|s| band(s.center_hz)).collect();
    bands.dedup();
    println!("\nGain advice (Airspy linearity preset; decoded TSBKs per channel, each against its best):");
    for b in bands {
        let these: Vec<&GainSweep> = sweeps.iter().filter(|s| band(s.center_hz) == b).collect();
        let gains: Vec<f32> = these[0].steps.iter().map(|s| s.gain).collect();
        let tsbks: Vec<Vec<u32>> = (0..gains.len()).map(|g| these.iter().flat_map(|s| s.steps[g].tsbks.iter().copied()).collect()).collect();
        let scores = gain_scores(&tsbks);
        let channels = tsbks.first().map_or(0, Vec::len);
        let table: Vec<String> = gains.iter().zip(&scores).map(|(g, s)| format!("{g}: {:.0}%", 100.0 * s)).collect();
        match best_gain(&gains, &scores) {
            Some(g) => println!("  {b}: --gain {g}  ({channels} control channel(s); {})", table.join(", ")),
            None => println!("  {b}: nothing decoded at any gain"),
        }
    }
}

/// `p25-survey radios`: the radios plugged in.
pub fn radios() -> Result<(), String> {
    match survey_radio::airspy::list() {
        Ok(s) if s.is_empty() => println!("No Airspy plugged in."),
        Ok(s) => {
            for serial in s {
                println!("airspy:{serial:016X}");
            }
        }
        Err(e) => println!("Airspy: {e}"),
    }
    Ok(())
}

/// Mark copies over every record so far, then print and write the new ones.
fn flush(
    records: &mut [Record],
    written: &mut usize,
    hops: &[f64],
    hide: bool,
    (driver, gain_db): (&str, Option<f64>),
    file: Option<&mut BufWriter<File>>,
) -> Result<(), String> {
    mark_copies(records, hops);
    let new = &records[*written..];
    let mut sorted: Vec<&Record> = new.iter().collect();
    sorted.sort_by(|a, b| a.freq_hz.total_cmp(&b.freq_hz));
    for r in &sorted {
        if !(hide && !r.is_control()) {
            println!("{}", row(r));
        }
    }
    if let Some(f) = file {
        for r in new {
            writeln!(f, "{}", to_json(r, driver, gain_db)).map_err(|e| e.to_string())?;
        }
        f.flush().map_err(|e| e.to_string())?;
    }
    *written = records.len();
    Ok(())
}

/// One line per candidate.
fn row(r: &Record) -> String {
    let s = &r.site;
    let hex = |v: Option<u32>, w: usize| v.map_or("-".repeat(w), |v| format!("{v:0w$X}"));
    let mut line = format!(
        "  {:.5}  {:<15}  {:>5.1} dB  NAC {}  WACN {}  SYS {}  site {}  {}",
        r.freq_hz / 1e6,
        verdict_str(r.verdict),
        r.level_db,
        hex(s.nac.map(u32::from), 3),
        hex(s.wacn, 5),
        hex(s.sysid.map(u32::from), 3),
        match (s.rfss, s.site) {
            (Some(a), Some(b)) => format!("{a}-{b}"),
            _ => "-".into(),
        },
        r.decode_rate().map_or(String::new(), |d| format!("decode {:.0} %", 100.0 * d)),
    );
    if r.is_control() {
        line += &format!("  idens {} nbrs {} sec {}  {:.1} s", s.plan.len(), s.neighbors().count(), s.secondary().count(), r.dwell_s);
    }
    if let Some(c) = r.copy_of {
        line += &format!("  COPY of {:.5}", c / 1e6);
    }
    line
}
