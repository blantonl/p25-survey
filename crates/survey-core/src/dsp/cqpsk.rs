//! The CQPSK receiver (for simulcast LSM, and fine on C4FM too): the phase
//! each symbol moves the carrier by, ±45° or ±135°.
//!
//! Frequency tracking → root-raised-cosine matched filter (α 0.2) → the
//! phase difference across one symbol, scaled to Hz (135° in a symbol at
//! 4800 baud = 1800 Hz) so its output reads like the C4FM receiver's and the
//! same framer takes either.
//!
//! The frequency tracking matters here and not for C4FM: a phase step is
//! only known modulo 360°, and a radio 600 Hz off pushes ±135° past ±180°.

use std::f64::consts::PI;

use num_complex::{Complex32, Complex64};

use super::channel::SYMBOL_RATE;
use super::filter::Decimator;

/// RRC roll-off of P25's LSM / CQPSK pulse.
const ALPHA: f64 = 0.2;
/// Matched filter length, symbols.
const SPAN_SYMBOLS: usize = 8;
/// Frequency tracking's time constant, seconds.
const AFC_S: f64 = 0.1;

/// Root-raised-cosine taps: `sps` samples a symbol, `span` symbols long.
pub fn rrc(sps: usize, span: usize, alpha: f64) -> Vec<f32> {
    let n = sps * span + 1;
    let mid = (n / 2) as f64;
    let h: Vec<f64> = (0..n)
        .map(|i| {
            let t = (i as f64 - mid) / sps as f64;
            if t == 0.0 {
                1.0 - alpha + 4.0 * alpha / PI
            } else if (t.abs() - 1.0 / (4.0 * alpha)).abs() < 1e-9 {
                alpha / 2f64.sqrt() * ((1.0 + 2.0 / PI) * (PI / (4.0 * alpha)).sin() + (1.0 - 2.0 / PI) * (PI / (4.0 * alpha)).cos())
            } else {
                ((PI * t * (1.0 - alpha)).sin() + 4.0 * alpha * t * (PI * t * (1.0 + alpha)).cos()) / (PI * t * (1.0 - (4.0 * alpha * t).powi(2)))
            }
        })
        .collect();
    let sum: f64 = h.iter().sum();
    h.iter().map(|v| (v / sum) as f32).collect()
}

pub struct Cqpsk {
    rate: f64,
    sps: usize,
    /// Frequency tracking: the carrier's offset (Hz), and the NCO taking it off.
    offset_hz: f64,
    phase: f64,
    last: Complex32,
    gain: f64,
    filter: Decimator,
    /// Matched-filter output, the last `sps` of it (for the one-symbol difference).
    history: Vec<Complex32>,
    at: usize,
    filtered: Vec<Complex32>,
    tuned: Vec<Complex32>,
}

impl Cqpsk {
    pub fn new(rate_hz: f64, samples_per_symbol: usize) -> Cqpsk {
        let sps = samples_per_symbol.max(2);
        Cqpsk {
            rate: rate_hz,
            sps,
            offset_hz: 0.0,
            phase: 0.0,
            last: Complex32::new(1.0, 0.0),
            gain: 1.0 / (AFC_S * rate_hz),
            filter: Decimator::new(rrc(sps, SPAN_SYMBOLS, ALPHA), 1),
            history: vec![Complex32::new(0.0, 0.0); sps],
            at: 0,
            filtered: Vec::new(),
            tuned: Vec::new(),
        }
    }

    /// The carrier offset it is tracking, Hz.
    pub fn offset_hz(&self) -> f64 {
        self.offset_hz
    }

    /// Samples the matched filter delays the output by.
    pub fn delay(&self) -> usize {
        self.sps * SPAN_SYMBOLS / 2
    }

    /// Append each sample's phase step over the last symbol (Hz) to `out`.
    pub fn process(&mut self, input: &[Complex32], out: &mut Vec<f32>) {
        self.tuned.clear();
        let hz_per_rad = self.rate / (2.0 * PI);
        for &x in input {
            let y = x * {
                let r = Complex64::from_polar(1.0, -self.phase);
                Complex32::new(r.re as f32, r.im as f32)
            };
            self.phase = (self.phase + 2.0 * PI * self.offset_hz / self.rate) % (2.0 * PI);
            // What's left of the carrier after the NCO, averaged slowly.
            let f = (y * self.last.conj()).arg() as f64 * hz_per_rad;
            self.last = y;
            self.offset_hz += self.gain * f;
            self.tuned.push(y);
        }
        self.filtered.clear();
        self.filter.process(&self.tuned, &mut self.filtered);
        let scale = (SYMBOL_RATE / (2.0 * PI)) as f32;
        for &z in &self.filtered {
            let before = self.history[self.at];
            self.history[self.at] = z;
            self.at = (self.at + 1) % self.sps;
            out.push((z * before.conj()).arg() * scale);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rrc_is_symmetric_and_peaks_in_the_middle() {
        let h = rrc(10, 8, ALPHA);
        assert_eq!(h.len(), 81);
        for i in 0..40 {
            assert!((h[i] - h[80 - i]).abs() < 1e-6);
        }
        assert!(h.iter().all(|&v| v <= h[40]));
    }

    #[test]
    fn tracks_a_carrier_offset() {
        // A constant-envelope carrier 400 Hz off, with ±1800 Hz steps on it.
        let fs = 48_000.0;
        let mut phase = 0.0f64;
        let x: Vec<Complex32> = (0..fs as usize)
            .map(|i| {
                let dev = if (i / 10) % 2 == 0 { 1800.0 } else { -1800.0 };
                phase += 2.0 * PI * (400.0 + dev) / fs;
                Complex32::from_polar(1.0, phase as f32)
            })
            .collect();
        let mut c = Cqpsk::new(fs, 10);
        let mut out = Vec::new();
        c.process(&x, &mut out);
        assert!((c.offset_hz() - 400.0).abs() < 25.0, "offset {}", c.offset_hz());
    }
}
