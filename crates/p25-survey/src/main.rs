//! p25-survey. For now: `p25-survey info <recording>`, a recording's
//! metadata and level over time.

use std::process::ExitCode;

use survey_radio::sigmf::Recording;

const USAGE: &str = "usage: p25-survey info <recording>   (a SigMF .sigmf-meta / .sigmf-data, or the name without either)";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["info", path] => match info(path) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("p25-survey: {e}");
                ExitCode::FAILURE
            }
        },
        _ => {
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}

/// Metadata, then RMS and peak level (dBFS) and the share of samples near
/// full scale, per second.
fn info(path: &str) -> std::io::Result<()> {
    let r = Recording::open(path)?;
    println!("{}", r.data.display());
    println!("  {} at {:.3} MS/s, centre {:.6} MHz, {:.2} s ({} samples)", r.datatype.as_str(), r.rate_hz / 1e6, r.center_hz / 1e6, r.seconds(), r.samples);
    for (k, v) in [("recorded", &r.datetime), ("hardware", &r.hw), ("about", &r.description)] {
        if let Some(v) = v {
            println!("  {k}: {v}");
        }
    }
    println!("  second   rms dBFS   peak dBFS   >-1 dBFS");
    let per = r.rate_hz.round() as usize;
    let mut rd = r.reader()?;
    let mut block = Vec::new();
    let mut second = 0;
    while rd.read(&mut block, per)? > 0 {
        let (mut power, mut peak, mut hot) = (0f64, 0f32, 0usize);
        for s in &block {
            let p = s.norm_sqr();
            power += p as f64;
            peak = peak.max(s.re.abs()).max(s.im.abs());
            hot += (s.re.abs() > 0.891 || s.im.abs() > 0.891) as usize;
        }
        let rms = (power / block.len() as f64).sqrt();
        println!("  {second:>6}   {:>8.1}   {:>9.1}   {:>7.3}%", 20.0 * rms.log10(), 20.0 * peak.log10(), 100.0 * hot as f64 / block.len() as f64);
        second += 1;
    }
    Ok(())
}
