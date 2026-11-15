//! Where samples come from: SDRs (Airspy, HydraSDR, RTL-SDR, HackRF) and
//! SigMF recordings. Everything here is I/O; what is done with the samples is
//! `survey-core`'s.
//!
//! Each radio's own library (libairspy, libhydrasdr, librtlsdr, libhackrf)
//! is loaded when that radio is opened, so the program needs only the one in
//! use, and builds without any.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, TrySendError, sync_channel};
use std::time::Duration;

use num_complex::Complex32;

pub mod airspy;
pub mod hackrf;
pub mod rtlsdr;
pub mod sigmf;

/// A radio could not do what was asked.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RadioError(pub String);

impl std::fmt::Display for RadioError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for RadioError {}

/// How a radio's gain is set.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Gain {
    /// The radio's own AGC.
    Auto,
    /// Airspy / HydraSDR linearity preset, 0..=21.
    Linearity(u8),
    /// Airspy / HydraSDR sensitivity preset, 0..=21.
    Sensitivity(u8),
    /// dB: an RTL-SDR's tuner gain (the nearest it has); a HackRF's LNA gain
    /// (0-40, 8 dB steps).
    Db(f32),
}

/// A tunable source of complex samples.
pub trait Radio: Send {
    /// "airspy", "hydrasdr", "rtlsdr", "hackrf".
    fn kind(&self) -> &'static str;
    fn rate_hz(&self) -> f64;
    fn center_hz(&self) -> f64;
    /// Retune. The first tune starts the samples. Samples already queued are
    /// dropped; the survey also drops a settling time after each retune.
    fn tune(&mut self, hz: f64) -> Result<(), RadioError>;
    fn set_gain(&mut self, gain: Gain) -> Result<(), RadioError>;
    /// The next samples (replacing `out`): their count, or 0 if none came
    /// within `timeout`.
    fn read(&mut self, out: &mut Vec<Complex32>, timeout: Duration) -> Result<usize, RadioError>;
    /// Samples lost so far (the reader didn't keep up, or USB).
    fn dropped(&self) -> u64;
    /// What `--gain N` means on this radio (and each gain of a sweep).
    fn gain_from(&self, g: f32) -> Gain;
    /// The gains a sweep tries.
    fn sweep_gains(&self) -> Vec<f32>;
    /// A spike at the tuned centre (the LO leaking through): the survey keeps
    /// channels off it.
    fn dc_spike(&self) -> bool;
}

/// Blocks queued between a driver's own thread and the reader.
const QUEUE_BLOCKS: usize = 64;

/// The queue a driver's callback thread fills and [`Radio::read`] empties.
/// A full queue drops the block and counts it: better to lose a block than
/// stall the USB transfers.
pub(crate) struct Stream {
    tx: SyncSender<Vec<Complex32>>,
    dropped: AtomicU64,
}

impl Stream {
    pub(crate) fn new() -> (Stream, Receiver<Vec<Complex32>>) {
        let (tx, rx) = sync_channel(QUEUE_BLOCKS);
        (Stream { tx, dropped: AtomicU64::new(0) }, rx)
    }

    pub(crate) fn send(&self, block: Vec<Complex32>) {
        let n = block.len() as u64;
        if let Err(TrySendError::Full(_) | TrySendError::Disconnected(_)) = self.tx.try_send(block) {
            self.dropped.fetch_add(n, Ordering::Relaxed);
        }
    }

    pub(crate) fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

/// The next block, and what else is waiting (so the reader keeps up).
pub(crate) fn take(rx: &Receiver<Vec<Complex32>>, out: &mut Vec<Complex32>, timeout: Duration, what: &str) -> Result<usize, RadioError> {
    out.clear();
    match rx.recv_timeout(timeout) {
        Ok(block) => {
            *out = block;
            while out.len() < 1 << 18 {
                match rx.try_recv() {
                    Ok(b) => out.extend_from_slice(&b),
                    Err(_) => break,
                }
            }
            Ok(out.len())
        }
        Err(RecvTimeoutError::Timeout) => Ok(0),
        Err(RecvTimeoutError::Disconnected) => Err(RadioError(format!("{what}: stopped streaming"))),
    }
}

/// Empty the queue (after a retune: what's in it is the old frequency).
pub(crate) fn drain(rx: &Receiver<Vec<Complex32>>) {
    while rx.try_recv().is_ok() {}
}

/// Takes off a radio's DC offset: a one-pole high-pass with its notch a few
/// hertz wide, so a channel centred there loses nothing that matters.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct DcBlock {
    mean: Complex32,
}

impl DcBlock {
    const ALPHA: f32 = 1e-5;

    pub(crate) fn apply(&mut self, x: Complex32) -> Complex32 {
        self.mean += (x - self.mean) * Self::ALPHA;
        x - self.mean
    }
}

/// Load the first of `names` that loads.
pub(crate) fn load(names: &[&str], what: &str, install: &str) -> Result<libloading::Library, RadioError> {
    names
        .iter()
        .find_map(|n| unsafe { libloading::Library::new(n) }.ok())
        .ok_or_else(|| RadioError(format!("{what} isn't installed (looked for {}). {install}", names.join(", "))))
}

/// Options for opening a radio.
#[derive(Clone, Debug, Default)]
pub struct Options {
    /// Highest sample rate wanted, S/s (the radio's default if None).
    pub max_rate_hz: Option<f64>,
    /// HackRF: baseband (VGA) gain, dB (0-62).
    pub vga_db: Option<u32>,
    /// HackRF: the RF amplifier (14 dB).
    pub amp: bool,
}

/// Open a radio: `airspy`, `hydrasdr`, `rtlsdr`, `hackrf`, each optionally
/// `:SERIAL` (as `list` gives it).
pub fn open(spec: &str, opts: &Options) -> Result<Box<dyn Radio>, RadioError> {
    let (kind, serial) = spec.split_once(':').map_or((spec, None), |(k, s)| (k, Some(s)));
    let hex = |s: &str| u64::from_str_radix(s.trim_start_matches("0x"), 16).map_err(|_| RadioError(format!("{kind}: serial {s}?")));
    Ok(match kind {
        "airspy" => Box::new(airspy::Spy::open(airspy::AIRSPY, serial.map(hex).transpose()?, opts.max_rate_hz.unwrap_or(6e6))?),
        "hydrasdr" => Box::new(airspy::Spy::open(airspy::HYDRASDR, serial.map(hex).transpose()?, opts.max_rate_hz.unwrap_or(10e6))?),
        "rtlsdr" | "rtl" => Box::new(rtlsdr::Rtl::open(serial, opts.max_rate_hz.unwrap_or(2.4e6))?),
        "hackrf" => Box::new(hackrf::HackRf::open(serial, opts.max_rate_hz.unwrap_or(9.6e6), opts.vga_db.unwrap_or(24), opts.amp)?),
        _ => return Err(RadioError(format!("{kind}? (airspy, hydrasdr, rtlsdr, hackrf)"))),
    })
}

/// The radios plugged in, as `kind:serial` (or why a kind can't be listed).
pub fn list() -> Vec<Result<String, RadioError>> {
    let mut out = Vec::new();
    for fam in [airspy::AIRSPY, airspy::HYDRASDR] {
        match airspy::list(fam) {
            Ok(s) => out.extend(s.into_iter().map(|s| Ok(format!("{}:{s:016X}", fam.kind)))),
            Err(e) => out.push(Err(e)),
        }
    }
    match rtlsdr::list() {
        Ok(s) => out.extend(s.into_iter().map(|(serial, name)| Ok(format!("rtlsdr:{serial}  ({name})")))),
        Err(e) => out.push(Err(e)),
    }
    match hackrf::list() {
        Ok(s) => out.extend(s.into_iter().map(|s| Ok(format!("hackrf:{s}")))),
        Err(e) => out.push(Err(e)),
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dc_block_takes_off_an_offset_and_keeps_a_tone() {
        let mut d = DcBlock::default();
        let fs = 2.4e6;
        let mut last = Complex32::new(0.0, 0.0);
        for i in 0..2_000_000 {
            let ph = ((2.0 * std::f64::consts::PI * 3125.0 * i as f64 / fs) % (2.0 * std::f64::consts::PI)) as f32;
            last = d.apply(Complex32::from_polar(0.5, ph) + Complex32::new(0.2, -0.1));
        }
        assert!((d.mean - Complex32::new(0.2, -0.1)).norm() < 0.01, "{:?}", d.mean);
        assert!((last.norm() - 0.5).abs() < 0.02);
    }
}
