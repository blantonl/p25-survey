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
use survey_core::dsp::detect::raster_hz;
use survey_core::survey::{Command, GainSweep, Output, Record, Survey, SurveyConfig, Verdict, best_gain, gain_scores, mark_copies};
use survey_radio::sigmf::{Reader, Recording};
use survey_radio::{Gain, Options, Radio};

use crate::record::{to_json, verdict_str};
use crate::rows::Row;

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
            Source::Radio(r) => r.kind(),
        }
    }

    /// A sweep's gain on this radio.
    fn gain_from(&self, g: f32) -> Gain {
        match self {
            Source::Recordings { .. } => Gain::Db(g),
            Source::Radio(r) => r.gain_from(g),
        }
    }

    /// Tune to `hz`; a radio `ppm` off is asked for `hz` corrected by it.
    fn tune(&mut self, hz: f64, ppm: f64) -> Result<(), String> {
        match self {
            Source::Recordings { recs, reader } => {
                let r = recs.iter().find(|r| r.center_hz == hz).ok_or("no recording at a hop's centre")?;
                *reader = Some(r.reader().map_err(|e| e.to_string())?);
                Ok(())
            }
            Source::Radio(r) => r.tune(hz * (1.0 + ppm * 1e-6)).map_err(|e| e.to_string()),
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

/// The gain asked for: `--gain N` as the radio takes it (Airspy and
/// HydraSDR: linearity preset 0-21; RTL-SDR: tuner dB; HackRF: LNA dB 0-40),
/// `--gain auto`, or `--sensitivity N` (Airspy, HydraSDR). Otherwise the
/// radio's default: linearity 14, the RTL-SDR's AGC, HackRF LNA 16 dB.
fn gain(rest: &[String], radio: &dyn Radio) -> Result<Gain, String> {
    if let Some(s) = crate::opt(rest, "--sensitivity") {
        return s.parse().map(Gain::Sensitivity).map_err(|_| "--sensitivity: 0-21".into());
    }
    match crate::opt(rest, "--gain") {
        Some("auto") => Ok(Gain::Auto),
        Some(g) => g.parse::<f32>().map(|g| radio.gain_from(g)).map_err(|_| "--gain: a number, or auto".into()),
        None => Ok(match radio.kind() {
            "rtlsdr" => Gain::Auto,
            "hackrf" => Gain::Db(16.0),
            _ => Gain::Linearity(14),
        }),
    }
}

/// The gain the records were made at, as a number (Python's sdr_gain_db).
fn gain_number(source: &Source, g: Gain) -> Option<f64> {
    match (source, g) {
        (Source::Radio(_), Gain::Linearity(v) | Gain::Sensitivity(v)) => Some(v as f64),
        (Source::Radio(_), Gain::Db(d)) => Some(d as f64),
        _ => None,
    }
}

/// `--radio KIND[:SERIAL]`: airspy, hydrasdr, rtlsdr, hackrf.
fn open_radio(spec: &str, rest: &[String]) -> Result<Box<dyn Radio>, String> {
    let max_rate_hz = crate::opt(rest, "--rate").map(|r| r.parse::<f64>().map(|m| m * 1e6).map_err(|_| "--rate: MS/s".to_string())).transpose()?;
    let vga_db = crate::opt(rest, "--vga").map(|v| v.parse::<u32>().map_err(|_| "--vga: 0-62 dB".to_string())).transpose()?;
    let opts = Options { max_rate_hz, vga_db, amp: rest.iter().any(|a| a == "--amp") };
    survey_radio::open(spec, &opts).map_err(|e| e.to_string())
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
    if let Source::Radio(r) = &source
        && r.dc_spike()
    {
        // Half a raster step off: no channel sits on the radio's DC spike.
        for c in cfg.hops.iter_mut() {
            *c += raster_hz(*c) / 2.0;
        }
    }
    if let Some(t) = num("--threshold")? {
        cfg.detect.threshold_db = t;
    }
    if let Some(t) = num("--max-dwell")? {
        cfg.max_dwell_s = t;
    }
    let user_gain = match &mut source {
        Source::Radio(r) => {
            let g = gain(rest, r.as_ref())?;
            r.set_gain(g).map_err(|e| e.to_string())?;
            g
        }
        Source::Recordings { .. } => Gain::Auto,
    };
    if rest.iter().any(|a| a == "--auto-gain") {
        if matches!(source, Source::Recordings { .. }) {
            return Err("scan: --auto-gain needs a radio".into());
        }
        cfg.gains = match (crate::opt(rest, "--gain-sweep"), &source) {
            (Some(g), _) => g.split(',').map(|v| v.trim().parse::<f32>().map_err(|_| format!("--gain-sweep: {v}?"))).collect::<Result<_, _>>()?,
            (None, Source::Radio(r)) => r.sweep_gains(),
            (None, Source::Recordings { .. }) => unreachable!(),
        };
        if cfg.gains.len() < 2 {
            return Err("--gain-sweep: at least two gains".into());
        }
    }
    // --ppm: the radio's error, as the report gives it (+0.83: it reads 0.83
    // ppm high); every tuning is corrected by it.
    let ppm = num("--ppm")?.unwrap_or(0.0);
    if ppm != 0.0 && matches!(source, Source::Recordings { .. }) {
        return Err("scan: --ppm is for a radio (a recording can't be retuned)".into());
    }
    let out_path = crate::opt(rest, "--output").map(std::path::PathBuf::from);
    // --resume: the records already in --output stay; what they cover isn't
    // surveyed again (except partial control channels: another chance).
    let resume = rest.iter().any(|a| a == "--resume");
    let mut rows: Vec<Row> = match (&out_path, resume) {
        (Some(p), true) if p.exists() => crate::rows::read(p)?,
        (None, true) => return Err("scan: --resume needs --output".into()),
        _ => Vec::new(),
    };
    if resume {
        cfg.done_hz = rows.iter().filter(|r| !(r.is_control() && !r.complete)).map(|r| r.freq_hz as f64).collect();
        println!("Resuming: {} record(s) already in {}", rows.len(), out_path.as_ref().unwrap().display());
    }
    // Log in before scanning, so a wrong password doesn't cost a scan.
    let rr = if rest.iter().any(|a| a == "--rr") { Some(crate::rr_login(rest)?) } else { None };
    // RR is looked up on a thread of its own: the radio doesn't wait for HTTP.
    let (rr_tx, rr_worker) = match rr {
        Some(client) => {
            // A hop's rows at a time (with their index in `rows`), so a
            // weak channel can borrow its WACN from the hop's others; one
            // that still can't waits for the whole survey.
            let (tx, rx) = std::sync::mpsc::channel::<Vec<(usize, Row)>>();
            let worker = std::thread::spawn(move || {
                let mut seen: Vec<Row> = Vec::new();
                let mut done = Vec::new();
                let mut later = Vec::new();
                let look = |i: usize, row: &Row, seen: &[Row], done: &mut Vec<(usize, crate::enrich::Enrichment)>| {
                    let e = crate::enrich::enrich_in(row, seen, &client);
                    println!("    {}  {}", crate::rows::mhz(row.freq_hz), rr_summary(&e));
                    done.push((i, e));
                };
                for batch in rx {
                    seen.extend(batch.iter().map(|(_, r)| r.clone()));
                    // (Records resumed with RR's answer already keep it.)
                    for (i, row) in batch.into_iter().filter(|(_, r)| r.is_control() && r.rr.is_none()) {
                        let borrowable = row.wacn.is_some() || seen.iter().any(|o| o.sysid == row.sysid && o.wacn.is_some());
                        if borrowable || row.sysid.is_none() {
                            look(i, &row, &seen, &mut done);
                        } else {
                            later.push((i, row));
                        }
                    }
                }
                for (i, row) in later {
                    look(i, &row, &seen, &mut done);
                }
                done
            });
            if !rows.is_empty() {
                let _ = tx.send(rows.iter().cloned().enumerate().collect());
            }
            (Some(tx), Some(worker))
        }
        None => (None, None),
    };
    let mut out_file = match &out_path {
        Some(p) => {
            let f = if resume { std::fs::OpenOptions::new().create(true).append(true).open(p) } else { File::create(p) };
            Some(BufWriter::new(f.map_err(|e| format!("{}: {e}", p.display()))?))
        }
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
            Source::Radio(r) => r.kind().into(),
        },
        rate / 1e6,
        cfg.detect.threshold_db
    );
    // Ctrl-C once: finish this hop with what it has, then the reports.
    // Twice: stop at once (the records so far are on disk already).
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    {
        let stop = stop.clone();
        let _ = ctrlc::set_handler(move || {
            if stop.swap(true, std::sync::atomic::Ordering::SeqCst) {
                eprintln!("\np25-survey: stopped");
                std::process::exit(130);
            }
            eprintln!("\np25-survey: stopping after this hop (Ctrl-C again to stop now)");
        });
    }
    let mut stopped = false;
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
                    source.tune(hz, ppm)?;
                    survey.tuned(hz, &mut out);
                }
                Command::Gain(g) => {
                    let g = g.map_or(user_gain, |g| source.gain_from(g));
                    source.set_gain(g)?;
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
        if !stopped && stop.load(std::sync::atomic::Ordering::SeqCst) {
            stopped = true;
            survey.stop(&mut out);
        }
        for o in out.drain(..) {
            match o {
                Output::Hop { index, center_hz } => {
                    flush(
                        &mut records,
                        &mut written,
                        &hops,
                        hide,
                        (source.name(), gain_number(&source, user_gain), ppm),
                        rr_tx.as_ref(),
                        &mut rows,
                        out_file.as_mut(),
                    )?;
                    println!("\nhop {} of {} at {:.4} MHz", index + 1, hops.len(), center_hz / 1e6);
                }
                Output::Candidates { found, .. } => println!("  {} candidate(s)", found.len()),
                Output::Record(r) => records.push(*r),
                Output::GainSweep(sw) => {
                    flush(
                        &mut records,
                        &mut written,
                        &hops,
                        hide,
                        (source.name(), gain_number(&source, user_gain), ppm),
                        rr_tx.as_ref(),
                        &mut rows,
                        out_file.as_mut(),
                    )?;
                    print_sweep(&sw);
                    sweeps.push(*sw);
                }
                Output::Done => flush(
                    &mut records,
                    &mut written,
                    &hops,
                    hide,
                    (source.name(), gain_number(&source, user_gain), ppm),
                    rr_tx.as_ref(),
                    &mut rows,
                    out_file.as_mut(),
                )?,
            }
        }
    }
    // The survey is over: let the radio go, and count what it lost while
    // the survey ran (not while RR answers come in).
    let lost = match &source {
        Source::Radio(r) => r.dropped(),
        Source::Recordings { .. } => 0,
    };
    let unit = match source.name() {
        "airspy" | "hydrasdr" => "linearity preset",
        "rtlsdr" => "tuner gain, dB",
        "hackrf" => "LNA gain, dB",
        _ => "gain",
    };
    drop(source);
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
    if lost > 0 {
        println!("{lost} samples lost ({:.2} % of the air): the computer didn't keep up.", 100.0 * lost as f64 / (air + lost) as f64);
    }
    if !sweeps.is_empty() {
        print_bands(&sweeps, unit);
    }
    let mut rewrite = false;
    if let Some(worker) = rr_worker {
        drop(rr_tx);
        println!("Waiting for the RadioReference lookups...");
        for (i, e) in worker.join().map_err(|_| "the RadioReference lookups failed")? {
            rows[i].rr = Some(e);
        }
        rewrite = true;
    }
    if resume {
        // A channel surveyed again keeps only its newest record.
        crate::rows::keep_latest(&mut rows);
        rewrite = true;
    }
    if let (true, Some(p)) = (rewrite, &out_path) {
        drop(out_file.take());
        crate::rows::write(p, &rows)?;
    }
    if resume {
        println!("In all, with the records resumed: {} control channel(s).", rows.iter().filter(|r| r.is_control()).count());
    }
    if let Some(c) = crate::report::clock(&rows) {
        println!("Radio clock: {:+.2} ppm ({} control channels, consistency {})", c.median_ppm, c.n, c.consistency);
    }
    if let Some(p) = &out_path {
        crate::write_reports(p, &rows)?;
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
fn print_bands(sweeps: &[GainSweep], unit: &str) {
    let mut bands: Vec<&str> = sweeps.iter().map(|s| band(s.center_hz)).collect();
    bands.dedup();
    println!("\nGain advice ({unit}; decoded TSBKs per channel, each against its best):");
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

/// `p25-survey radios`: the radios plugged in (and the kinds whose library
/// isn't installed).
pub fn radios() -> Result<(), String> {
    let all = survey_radio::list();
    let found: Vec<&String> = all.iter().filter_map(|r| r.as_ref().ok()).collect();
    if found.is_empty() {
        println!("No radio plugged in.");
    }
    for r in found {
        println!("{r}");
    }
    for e in all.iter().filter_map(|r| r.as_ref().err()) {
        println!("  ({e})");
    }
    Ok(())
}

/// Mark copies over every record so far, then print the new ones, write
/// them, and hand them to the RR lookups (written again with RR's answers
/// at the end).
#[allow(clippy::too_many_arguments)]
fn flush(
    records: &mut [Record],
    written: &mut usize,
    hops: &[f64],
    hide: bool,
    (driver, gain_db, ppm): (&str, Option<f64>, f64),
    rr: Option<&std::sync::mpsc::Sender<Vec<(usize, Row)>>>,
    rows: &mut Vec<Row>,
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
    let new_rows: Vec<Row> =
        new.iter().map(|r| serde_json::from_value(to_json(r, driver, gain_db, ppm)).map_err(|e| e.to_string())).collect::<Result<_, _>>()?;
    if let Some(tx) = rr {
        let _ = tx.send(new_rows.iter().cloned().enumerate().map(|(k, r)| (rows.len() + k, r)).collect());
    }
    if let Some(f) = file {
        for r in &new_rows {
            writeln!(f, "{}", serde_json::to_string(r).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        }
        f.flush().map_err(|e| e.to_string())?;
        // On the disk, not just in the OS: a crash keeps every hop done.
        f.get_ref().sync_data().map_err(|e| e.to_string())?;
    }
    rows.extend(new_rows);
    *written = records.len();
    Ok(())
}

/// What RR made of a control channel, in a few words.
fn rr_summary(e: &crate::enrich::Enrichment) -> String {
    if !e.system_match {
        return e.notes.first().cloned().unwrap_or_else(|| "not in RR".into());
    }
    let name = e.rr_system_name.clone().unwrap_or_default();
    if !e.site_match {
        return format!("RR: {name}, {}", if e.ambiguous_site { "site ambiguous" } else { "NEW SITE" });
    }
    let mut s = format!("RR: {name} / {}", e.rr_site_description.clone().unwrap_or_default());
    if !e.cc_freq_in_db {
        s += ", FREQUENCY NOT LISTED";
    }
    if !e.neighbors_decoded_not_in_rr.is_empty() {
        s += &format!(", {} neighbour(s) not in RR", e.neighbors_decoded_not_in_rr.len());
    }
    s
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
