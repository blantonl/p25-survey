//! FM discrimination: the instantaneous frequency of a channel, in Hz, then
//! averaged over one symbol (C4FM's integrate-and-dump).

use num_complex::Complex32;

pub struct Discriminator {
    last: Complex32,
    hz_per_rad: f32,
    /// Moving average over one symbol.
    window: Vec<f32>,
    at: usize,
    sum: f32,
}

impl Discriminator {
    pub fn new(rate_hz: f64, samples_per_symbol: usize) -> Discriminator {
        Discriminator {
            last: Complex32::new(1.0, 0.0),
            hz_per_rad: (rate_hz / (2.0 * std::f64::consts::PI)) as f32,
            window: vec![0.0; samples_per_symbol.max(1)],
            at: 0,
            sum: 0.0,
        }
    }

    /// Append the averaged frequency (Hz) of each input sample to `out`.
    pub fn process(&mut self, input: &[Complex32], out: &mut Vec<f32>) {
        let n = self.window.len() as f32;
        for &x in input {
            let f = (x * self.last.conj()).arg() * self.hz_per_rad;
            self.last = x;
            self.sum += f - self.window[self.at];
            self.window[self.at] = f;
            self.at = (self.at + 1) % self.window.len();
            out.push(self.sum / n);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_tone_reads_as_its_frequency() {
        let fs = 48_000.0;
        let x: Vec<Complex32> = (0..1000).map(|i| Complex32::from_polar(1.0, (2.0 * std::f64::consts::PI * 1800.0 * i as f64 / fs) as f32)).collect();
        let mut d = Discriminator::new(fs, 10);
        let mut out = Vec::new();
        d.process(&x, &mut out);
        assert!(out[20..].iter().all(|&f| (f - 1800.0).abs() < 0.5));
    }
}
