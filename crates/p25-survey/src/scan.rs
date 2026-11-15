//! `p25-survey scan <recording>...`: the survey, on recordings. Each
//! recording is a hop; when the survey asks to tune to a centre, the
//! recording made there is played. The four 800 MHz recordings in the corpus
//! make a sweep of 851-869 MHz.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::time::Instant;

use survey_core::survey::{Command, Output, Record, Survey, SurveyConfig, Verdict, mark_copies};
use survey_radio::sigmf::{Reader, Recording};

use crate::record::{to_json, verdict_str};

pub fn scan(args: &[String]) -> Result<(), String> {
    let paths: Vec<&String> = args.iter().take_while(|a| !a.starts_with("--")).collect();
    let rest = &args[paths.len()..];
    if paths.is_empty() {
        return Err("scan: which recordings?".into());
    }
    let mut recs: Vec<Recording> = paths.iter().map(|p| Recording::open(p).map_err(|e| e.to_string())).collect::<Result<_, _>>()?;
    recs.sort_by(|a, b| a.center_hz.total_cmp(&b.center_hz));
    let rate = recs[0].rate_hz;
    if recs.iter().any(|r| r.rate_hz != rate) {
        return Err("scan: the recordings' sample rates differ".into());
    }
    let num = |name: &str| crate::opt(rest, name).map(|v| v.parse::<f64>().map_err(|_| format!("{name}: a number"))).transpose();
    let usable = 0.85;
    let span = (recs[0].center_hz - rate * usable / 2.0, recs[recs.len() - 1].center_hz + rate * usable / 2.0);
    let range = (num("--start")?.map_or(span.0, |m| m * 1e6), num("--stop")?.map_or(span.1, |m| m * 1e6));
    let mut cfg = SurveyConfig::new(rate, range);
    cfg.usable = usable;
    cfg.hops = recs.iter().map(|r| r.center_hz).collect();
    if let Some(t) = num("--threshold")? {
        cfg.detect.threshold_db = t;
    }
    if let Some(t) = num("--max-dwell")? {
        cfg.max_dwell_s = t;
    }
    let mut out_file = match crate::opt(rest, "--output") {
        Some(p) => Some(BufWriter::new(File::create(p).map_err(|e| format!("{p}: {e}"))?)),
        None => None,
    };
    let hide = rest.iter().any(|a| a == "--hide-no-cc");

    println!(
        "Scanning {:.4}-{:.4} MHz: {} recording(s) at {:.1} MS/s, threshold {:.1} dB",
        range.0 / 1e6,
        range.1 / 1e6,
        recs.len(),
        rate / 1e6,
        cfg.detect.threshold_db
    );
    let t0 = Instant::now();
    let mut survey = Survey::new(cfg);
    let mut reader: Option<Reader> = None;
    let mut block = Vec::new();
    let mut out = Vec::new();
    let mut air = 0u64;
    let mut records: Vec<Record> = Vec::new();
    // A hop's records are written when the hop ends: a copy can't be told
    // from the channel it copies until both are in.
    let mut written = 0;
    let hops = survey.config().hops.clone();
    loop {
        if let Some(Command::Tune(c)) = survey.command() {
            let r = recs.iter().find(|r| r.center_hz == c).ok_or("no recording at a hop's centre")?;
            reader = Some(r.reader().map_err(|e| e.to_string())?);
            survey.tuned(c, &mut out);
        }
        if survey.is_done() {
            break;
        }
        let n = match reader.as_mut() {
            Some(rd) => rd.read(&mut block, 1 << 18).map_err(|e| e.to_string())?,
            None => 0,
        };
        if n == 0 {
            reader = None;
            survey.end_of_samples(&mut out);
        } else {
            air += n as u64;
            survey.push(&block, &mut out);
        }
        for o in out.drain(..) {
            match o {
                Output::Hop { index, center_hz } => {
                    flush(&mut records, &mut written, &hops, hide, out_file.as_mut())?;
                    println!("\nhop {} at {:.4} MHz", index + 1, center_hz / 1e6);
                }
                Output::Candidates { found, .. } => println!("  {} candidate(s)", found.len()),
                Output::Record(r) => records.push(*r),
                Output::Done => flush(&mut records, &mut written, &hops, hide, out_file.as_mut())?,
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
    Ok(())
}

/// Mark copies over every record so far, then print and write the new ones.
fn flush(records: &mut [Record], written: &mut usize, hops: &[f64], hide: bool, file: Option<&mut BufWriter<File>>) -> Result<(), String> {
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
            writeln!(f, "{}", to_json(r, "file")).map_err(|e| e.to_string())?;
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
    if matches!(r.verdict, Verdict::Complete | Verdict::Partial) {
        line += &format!("  idens {} nbrs {} sec {}  {:.1} s", s.plan.len(), s.neighbors().count(), s.secondary().count(), r.dwell_s);
    }
    if let Some(c) = r.copy_of {
        line += &format!("  COPY of {:.5}", c / 1e6);
    }
    line
}
