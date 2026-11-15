//! One narrowband channel cut out of the wideband samples: mix it to 0 Hz,
//! then decimate in stages to a rate that is a whole number of samples per
//! P25 symbol. (A shared FFT channelizer replaces this for many channels.)

use num_complex::{Complex32, Complex64};

use super::filter::{Decimator, lowpass, taps_for};

/// P25 symbol rate.
pub const SYMBOL_RATE: f64 = 4800.0;
/// One-sided bandwidth kept: a 12.5 kHz channel, plus room for the radio's
/// frequency error.
pub(crate) const PASS_HZ: f64 = 7250.0;
/// The last filter's transition, Hz (the adjacent 12.5 kHz channel starts at
/// 6.25 kHz from its centre, so its energy is mostly past 9.5 kHz from ours).
const CHANNEL_TRANSITION_HZ: f64 = 2500.0;

pub struct Channel {
    step: Complex64,
    phasor: Complex64,
    since_norm: u32,
    stages: Vec<Decimator>,
    out_rate: f64,
    mixed: Vec<Complex32>,
    tmp: Vec<Complex32>,
}

/// Decimation stages (each 2..=8) for `total`, largest first; None if `total`
/// has a prime factor over 8.
fn stages(mut total: usize) -> Option<Vec<usize>> {
    let mut f = Vec::new();
    for p in [8, 7, 6, 5, 4, 3, 2] {
        while total % p == 0 && total > 1 {
            f.push(p);
            total /= p;
        }
    }
    (total == 1).then_some(f)
}

/// The channel rate for wideband samples at `rate_hz`, and the decimation
/// to it: 4800 × (10 down to 5) samples a second if one divides the rate;
/// otherwise the decimation whose rate is nearest a whole number of samples
/// per symbol (within 1 %: 10 MS/s ÷ 208 is 10.016 a symbol), which the
/// framer, timing every symbol from its frame sync, takes as it is.
pub(crate) fn decimation(rate_hz: f64) -> Result<(f64, usize), String> {
    let exact = (5..=10).rev().find_map(|sps| {
        let out = SYMBOL_RATE * sps as f64;
        let d = rate_hz / out;
        (d.fract() == 0.0 && d >= 1.0).then_some((out, d as usize))
    });
    if let Some(e) = exact {
        return Ok(e);
    }
    let lo = (rate_hz / (SYMBOL_RATE * 10.5)).ceil().max(1.0) as usize;
    let hi = (rate_hz / (SYMBOL_RATE * 4.5)).floor() as usize;
    (lo..=hi)
        .map(|d| {
            let sps = rate_hz / d as f64 / SYMBOL_RATE;
            (d, sps, (sps - sps.round()).abs() / sps)
        })
        .filter(|&(_, _, off)| off <= 0.01)
        // The most samples a symbol (finer timing), then the nearest whole.
        .min_by(|a, b| b.1.round().total_cmp(&a.1.round()).then(a.2.total_cmp(&b.2)))
        .map(|(d, _, _)| (rate_hz / d as f64, d))
        .ok_or_else(|| format!("no decimation from {rate_hz} S/s to about a whole number of samples per symbol"))
}

/// The channel filter proper, at the channel rate.
pub(crate) fn channel_filter(rate_hz: f64) -> Vec<f32> {
    lowpass(taps_for(CHANNEL_TRANSITION_HZ / rate_hz), (PASS_HZ + CHANNEL_TRANSITION_HZ / 2.0) / rate_hz)
}

impl Channel {
    /// A channel `offset_hz` from the centre of samples at `rate_hz`, at the
    /// rate [`decimation`] picks, decimated in small stages.
    pub fn new(rate_hz: f64, offset_hz: f64) -> Result<Channel, String> {
        let (out_rate, d) = decimation(rate_hz)?;
        let factors = stages(d).ok_or_else(|| format!("decimation by {d} isn't in stages of 8 or less"))?;
        let mut stages = Vec::new();
        let mut r = rate_hz;
        for &d in &factors {
            let out = r / d as f64;
            // Keep what will alias onto ±PASS_HZ out: pass to PASS_HZ, stop by out − PASS_HZ.
            let width = ((out - 2.0 * PASS_HZ) / r).max(0.02);
            stages.push(Decimator::new(lowpass(taps_for(width), 0.5 / d as f64), d));
            r = out;
        }
        stages.push(Decimator::new(channel_filter(r), 1));
        let w = -2.0 * std::f64::consts::PI * offset_hz / rate_hz;
        Ok(Channel {
            step: Complex64::from_polar(1.0, w),
            phasor: Complex64::new(1.0, 0.0),
            since_norm: 0,
            stages,
            out_rate,
            mixed: Vec::new(),
            tmp: Vec::new(),
        })
    }

    pub fn output_rate(&self) -> f64 {
        self.out_rate
    }

    pub fn samples_per_symbol(&self) -> f64 {
        self.out_rate / SYMBOL_RATE
    }

    /// Take wideband samples; append the channel's to `out`.
    pub fn process(&mut self, input: &[Complex32], out: &mut Vec<Complex32>) {
        self.mixed.clear();
        for &x in input {
            let p = Complex32::new(self.phasor.re as f32, self.phasor.im as f32);
            self.mixed.push(x * p);
            self.phasor *= self.step;
            self.since_norm += 1;
            if self.since_norm == 4096 {
                self.phasor /= self.phasor.norm();
                self.since_norm = 0;
            }
        }
        let n = self.stages.len();
        let mut src = std::mem::take(&mut self.mixed);
        for (i, st) in self.stages.iter_mut().enumerate() {
            if i + 1 == n {
                st.process(&src, out);
            } else {
                self.tmp.clear();
                st.process(&src, &mut self.tmp);
                std::mem::swap(&mut src, &mut self.tmp);
            }
        }
        self.mixed = src;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rates() {
        assert_eq!(Channel::new(6e6, 0.0).unwrap().output_rate(), 48_000.0);
        assert_eq!(Channel::new(2.4e6, 0.0).unwrap().output_rate(), 48_000.0);
        assert_eq!(Channel::new(3e6, 0.0).unwrap().output_rate(), 24_000.0);
        // Not a whole number: the nearest (10 MS/s ÷ 208 = 48 077 S/s, 10.016 a symbol).
        assert_eq!(decimation(10e6).unwrap().1, 208);
        assert_eq!(decimation(2.5e6).unwrap().1, 52);
        // 2.048 MS/s ÷ 43 is near enough, but 43 isn't in stages for Channel.
        assert!(Channel::new(2.048e6, 0.0).is_err());
    }

    #[test]
    fn picks_its_channel_and_rejects_the_next() {
        let fs = 2.4e6;
        let tone = |f: f64| -> Vec<Complex32> {
            (0..240_000).map(|i| Complex32::from_polar(1.0, ((2.0 * std::f64::consts::PI * f * i as f64 / fs) % (2.0 * std::f64::consts::PI)) as f32)).collect()
        };
        let power = |x: &[Complex32]| x[200..].iter().map(|s| s.norm_sqr()).sum::<f32>() / (x.len() - 200) as f32;
        // 1 kHz into a channel at +300 kHz.
        let mut ch = Channel::new(fs, 300_000.0).unwrap();
        let mut out = Vec::new();
        ch.process(&tone(301_000.0), &mut out);
        assert!((power(&out) - 1.0).abs() < 0.02, "in channel {}", power(&out));
        // The next 12.5 kHz channel's centre is 40 dB down at least.
        let mut ch = Channel::new(fs, 300_000.0).unwrap();
        out.clear();
        ch.process(&tone(312_500.0), &mut out);
        assert!(power(&out) < 1e-4, "adjacent {}", power(&out));
    }
}
