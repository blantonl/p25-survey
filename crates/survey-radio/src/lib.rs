//! Where samples come from: SDRs (Airspy now; RTL-SDR and HackRF next) and
//! SigMF recordings. Everything here is I/O; what is done with the samples is
//! `survey-core`'s.

use std::time::Duration;

use num_complex::Complex32;

pub mod airspy;
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
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Gain {
    /// The radio's own AGC.
    Auto,
    /// Airspy linearity preset, 0..=21.
    Linearity(u8),
    /// Airspy sensitivity preset, 0..=21.
    Sensitivity(u8),
}

/// A tunable source of complex samples.
pub trait Radio {
    fn rate_hz(&self) -> f64;
    fn center_hz(&self) -> f64;
    /// Retune. The first tune starts the samples. Samples already queued are
    /// from before: the survey drops a settling time after each retune.
    fn tune(&mut self, hz: f64) -> Result<(), RadioError>;
    fn set_gain(&mut self, gain: Gain) -> Result<(), RadioError>;
    /// The next samples (replacing `out`): their count, or 0 if none came
    /// within `timeout`.
    fn read(&mut self, out: &mut Vec<Complex32>, timeout: Duration) -> Result<usize, RadioError>;
    /// Samples lost so far (the reader didn't keep up, or USB).
    fn dropped(&self) -> u64;
}
