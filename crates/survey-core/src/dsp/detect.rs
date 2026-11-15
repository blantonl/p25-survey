//! Finding control channels by their power: a control channel never keys
//! down, so over a look of a few hundred milliseconds its power stays up
//! while voice channels and data come and go.
//!
//! Short FFTs (about 1 kHz bins) over the look; per bin, a low percentile of
//! the power over time (a carrier that dropped out at all reads low). Then
//! per channel on the raster (6.25 kHz; 2.5 kHz on VHF): its power against
//! the local noise floor (the median over a few hundred kHz). A candidate is
//! a channel enough above the floor, the strongest within two raster steps
//! either side (a 12.5 kHz channel covers its neighbours on a 6.25 kHz
//! raster), and not part of something wide.

use std::sync::Arc;

use num_complex::Complex32;
use rustfft::{Fft, FftPlanner};

/// Detection settings.
#[derive(Clone, Debug)]
pub struct DetectConfig {
    /// dB a channel must be over the local floor.
    pub threshold_db: f64,
    /// The percentile of each bin's power over the look (0.2 = 20th).
    pub percentile: f64,
    /// A channel's power: the bins within this of it, Hz.
    pub channel_half_hz: f64,
    /// The floor: the median within this of it, Hz.
    pub floor_half_hz: f64,
    /// Something up without a break this far either side is wide, not a
    /// channel, Hz.
    pub wide_hz: f64,
}

impl Default for DetectConfig {
    fn default() -> Self {
        DetectConfig { threshold_db: 4.5, percentile: 0.2, channel_half_hz: 4000.0, floor_half_hz: 150_000.0, wide_hz: 25_000.0 }
    }
}

/// A channel that stood out.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Found {
    pub freq_hz: f64,
    /// dB over the local floor.
    pub level_db: f64,
}

/// The channel raster at a frequency: 2.5 kHz on VHF high band, else 6.25 kHz.
pub fn raster_hz(freq_hz: f64) -> f64 {
    if (136e6..=174e6).contains(&freq_hz) { 2500.0 } else { 6250.0 }
}

pub struct Detector {
    rate: f64,
    n: usize,
    fft: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
    pending: Vec<Complex32>,
    /// Power per bin per frame, bins in frequency order (-rate/2 first).
    frames: Vec<Vec<f32>>,
    buf: Vec<Complex32>,
}

impl Detector {
    /// For samples at `rate_hz`, bins of about 1 kHz.
    pub fn new(rate_hz: f64) -> Detector {
        let n = ((rate_hz / 1000.0) as usize).next_power_of_two().max(64);
        let m = (n - 1) as f32;
        let window = (0..n)
            .map(|i| {
                let x = 2.0 * std::f32::consts::PI * i as f32 / m;
                0.42 - 0.5 * x.cos() + 0.08 * (2.0 * x).cos()
            })
            .collect();
        Detector {
            rate: rate_hz,
            n,
            fft: FftPlanner::new().plan_fft_forward(n),
            window,
            pending: Vec::new(),
            frames: Vec::new(),
            buf: vec![Complex32::new(0.0, 0.0); n],
        }
    }

    pub fn bin_hz(&self) -> f64 {
        self.rate / self.n as f64
    }

    /// Seconds looked at so far.
    pub fn seconds(&self) -> f64 {
        (self.frames.len() * self.n) as f64 / self.rate
    }

    pub fn push(&mut self, x: &[Complex32]) {
        self.pending.extend_from_slice(x);
        let mut at = 0;
        while at + self.n <= self.pending.len() {
            for (i, (b, &w)) in self.buf.iter_mut().zip(&self.window).enumerate() {
                *b = self.pending[at + i] * w;
            }
            self.fft.process(&mut self.buf);
            let half = self.n / 2;
            let frame: Vec<f32> = (0..self.n).map(|k| self.buf[(k + half) % self.n].norm_sqr()).collect();
            self.frames.push(frame);
            at += self.n;
        }
        self.pending.drain(..at);
    }

    /// The look's spectrum: each bin's percentile power, dB, bins in
    /// frequency order (bin k is (k - n/2) × bin_hz from the centre).
    pub fn spectrum(&self, percentile: f64) -> Vec<f32> {
        let f = self.frames.len();
        if f == 0 {
            return vec![f32::NEG_INFINITY; self.n];
        }
        let nth = ((f - 1) as f64 * percentile).round() as usize;
        let mut col = vec![0f32; f];
        (0..self.n)
            .map(|k| {
                for (c, fr) in col.iter_mut().zip(&self.frames) {
                    *c = fr[k];
                }
                let (_, v, _) = col.select_nth_unstable_by(nth, |a, b| a.total_cmp(b));
                10.0 * v.max(1e-20).log10()
            })
            .collect()
    }

    /// The channels that stood out, between `lo_hz` and `hi_hz` (absolute),
    /// for a tuning centred on `center_hz`.
    pub fn find(&self, center_hz: f64, lo_hz: f64, hi_hz: f64, cfg: &DetectConfig) -> Vec<Found> {
        let spec = self.spectrum(cfg.percentile);
        let bin = self.bin_hz();
        let half = (self.n / 2) as f64;
        // Bins whose centres are within `w` Hz of `f`.
        let bins = |f: f64, w: f64| {
            let k = (f - center_hz) / bin + half;
            let lo = (k - w / bin).ceil().max(0.0) as usize;
            let hi = ((k + w / bin).floor() as usize).min(self.n - 1);
            lo..=hi
        };
        let power = |f: f64| {
            let r = bins(f, cfg.channel_half_hz);
            let lin: f64 = r.clone().map(|k| 10f64.powf(spec[k] as f64 / 10.0)).sum();
            10.0 * (lin / r.count().max(1) as f64).log10()
        };
        let floor = |f: f64| {
            let mut v: Vec<f32> = bins(f, cfg.floor_half_hz).map(|k| spec[k]).collect();
            let mid = v.len() / 2;
            *v.select_nth_unstable_by(mid, |a, b| a.total_cmp(b)).1 as f64
        };
        let mut out = Vec::new();
        if hi_hz <= lo_hz {
            return out;
        }
        let r = raster_hz((lo_hz + hi_hz) / 2.0);
        let first = (lo_hz / r).ceil() as i64;
        let last = (hi_hz / r).floor() as i64;
        let levels: Vec<(f64, f64)> = (first - 4..=last + 4).map(|i| (i as f64 * r, power(i as f64 * r) - floor(i as f64 * r))).collect();
        let reach = (cfg.wide_hz / r).round() as usize;
        for i in 4..levels.len() - 4 {
            let (f, l) = levels[i];
            if l < cfg.threshold_db || f < lo_hz || f > hi_hz {
                continue;
            }
            // The strongest within two raster steps (ties to the lower).
            let near = 2;
            if (1..=near).any(|d| levels[i - d].1 > l || levels[i + d].1 >= l) {
                continue;
            }
            // Wide: up without a break for `wide_hz` on both sides. (Two
            // channels 25 kHz apart have a gap between them; something wide
            // doesn't.)
            let up = |j: usize| levels.get(j).is_some_and(|&(_, v)| v >= cfg.threshold_db - 3.0);
            if i >= reach && (i - reach..=i).all(up) && (i..=i + reach).all(up) {
                continue;
            }
            out.push(Found { freq_hz: f, level_db: l });
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Noise, plus carriers: one always on, one on only a quarter of the time.
    #[test]
    fn finds_carriers_that_stay_on() {
        let fs = 2.4e6;
        let mut seed = 12345u64;
        let mut noise = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((seed >> 40) as f32 / (1u64 << 24) as f32 - 0.5) * 0.2
        };
        let n = (0.35 * fs) as usize;
        let on = 300_000.0 + 12_500.0 * 3.0; // a raster channel
        let bursty = -500_000.0;
        let x: Vec<Complex32> = (0..n)
            .map(|i| {
                let t = i as f64 / fs;
                let mut s = Complex32::new(noise(), noise());
                // A wobbling carrier, a few kHz wide.
                let ph = 2.0 * std::f64::consts::PI * (on * t + 2000.0 * (2.0 * std::f64::consts::PI * 700.0 * t).sin() / 700.0);
                s += Complex32::from_polar(0.3, (ph % (2.0 * std::f64::consts::PI)) as f32);
                if i < n / 4 {
                    s += Complex32::from_polar(0.3, ((2.0 * std::f64::consts::PI * bursty * t) % (2.0 * std::f64::consts::PI)) as f32);
                }
                s
            })
            .collect();
        let mut d = Detector::new(fs);
        d.push(&x);
        let center = 851e6;
        let found = d.find(center, center - 1e6, center + 1e6, &DetectConfig::default());
        assert_eq!(found.len(), 1, "{found:?}");
        assert!((found[0].freq_hz - (center + on)).abs() <= 6250.0, "{found:?}");
        assert!(found[0].level_db > 10.0);
    }

    #[test]
    fn rasters() {
        assert_eq!(raster_hz(851e6), 6250.0);
        assert_eq!(raster_hz(155e6), 2500.0);
    }
}
