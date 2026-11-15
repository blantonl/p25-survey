//! Many channels from one stream of wideband samples, for the cost of one
//! FFT: an overlap-save filter bank.
//!
//! Each block of N samples (overlapping the last by P - 1, the prefilter's
//! length less one) is transformed once. A channel ("head") takes the M = N/D
//! bins around its frequency, weighted by the prefilter's response, and an
//! M-point inverse FFT gives it the block's samples already moved to 0 Hz and
//! decimated by D. A phase correction per block keeps the move continuous,
//! and a small NCO takes off the rest of the offset (the head is centred on
//! the nearest bin). The prefilter only has to stop aliasing at the output
//! rate (and be nothing past ±out_rate/2); each head's own channel filter
//! then runs at that low rate.

use std::f64::consts::PI;
use std::sync::Arc;

use num_complex::{Complex32, Complex64};
use rustfft::{Fft, FftPlanner};

use super::channel::{PASS_HZ, channel_filter, decimation};
use super::filter::{Decimator, lowpass};

pub type HeadId = usize;

struct Head {
    /// The bin the channel is centred on (signed: below the centre is negative).
    bin: i64,
    /// NCO for the rest of the offset, at the output rate.
    step: Complex64,
    nco: Complex64,
    since_norm: u32,
    filter: Decimator,
    raw: Vec<Complex32>,
    out: Vec<Complex32>,
}

pub struct Channelizer {
    rate: f64,
    n: usize,
    m: usize,
    d: usize,
    /// P - 1: the samples each block shares with the last.
    overlap: usize,
    /// Samples each block moves on by.
    hop: usize,
    out_rate: f64,
    fwd: Arc<dyn Fft<f32>>,
    inv: Arc<dyn Fft<f32>>,
    /// The prefilter's N-point response, divided by N.
    response: Vec<Complex32>,
    pending: Vec<Complex32>,
    block: u64,
    spectrum: Vec<Complex32>,
    bins: Vec<Complex32>,
    heads: Vec<Option<Head>>,
}

impl Channelizer {
    /// For samples at `rate_hz`; heads put out 4800 × (5..=10) samples a
    /// second, as [`super::Channel`] does.
    pub fn new(rate_hz: f64) -> Result<Channelizer, String> {
        let (out_rate, d) = decimation(rate_hz)?;
        // Prefilter: pass ±(PASS_HZ + a little) and stop by ±out_rate/2. A
        // head keeps only the bins inside ±out_rate/2, so the response must
        // be nothing outside them, or the head's filter isn't the FIR and the
        // block edges show.
        let pass = PASS_HZ + 1250.0;
        let width = ((out_rate / 2.0 - pass) / rate_hz).max(1e-4);
        let taps = (5.5 / width).ceil() as usize;
        // P - 1 a multiple of D; P odd.
        let overlap = taps.div_ceil(d) * d;
        let overlap = if overlap % 2 == 1 { overlap + d } else { overlap };
        // N = D·M with M a power of two and N at least 4 (P - 1).
        let mut m = 16;
        while d * m < 4 * overlap {
            m *= 2;
        }
        let n = d * m;
        let h = lowpass(overlap + 1, (pass + out_rate / 2.0) / 2.0 / rate_hz);
        let mut planner = FftPlanner::new();
        let fwd = planner.plan_fft_forward(n);
        let inv = planner.plan_fft_inverse(m);
        let mut response: Vec<Complex32> = h.iter().map(|&v| Complex32::new(v / n as f32, 0.0)).collect();
        response.resize(n, Complex32::new(0.0, 0.0));
        fwd.process(&mut response);
        Ok(Channelizer {
            rate: rate_hz,
            n,
            m,
            d,
            overlap,
            hop: n - overlap,
            out_rate,
            fwd,
            inv,
            response,
            pending: vec![Complex32::new(0.0, 0.0); overlap],
            block: 0,
            spectrum: vec![Complex32::new(0.0, 0.0); n],
            bins: vec![Complex32::new(0.0, 0.0); m],
            heads: Vec::new(),
        })
    }

    pub fn output_rate(&self) -> f64 {
        self.out_rate
    }

    pub fn samples_per_symbol(&self) -> f64 {
        self.out_rate / super::SYMBOL_RATE
    }

    /// FFT size and bin width (Hz).
    pub fn fft(&self) -> (usize, f64) {
        (self.n, self.rate / self.n as f64)
    }

    /// A channel `offset_hz` from the centre. Its samples come from the next
    /// block on.
    pub fn add(&mut self, offset_hz: f64) -> HeadId {
        let bin_hz = self.rate / self.n as f64;
        let bin = (offset_hz / bin_hz).round() as i64;
        let rest = offset_hz - bin as f64 * bin_hz;
        let head = Head {
            bin,
            step: Complex64::from_polar(1.0, -2.0 * PI * rest / self.out_rate),
            nco: Complex64::new(1.0, 0.0),
            since_norm: 0,
            filter: Decimator::new(channel_filter(self.out_rate), 1),
            raw: Vec::new(),
            out: Vec::new(),
        };
        match self.heads.iter().position(Option::is_none) {
            Some(i) => {
                self.heads[i] = Some(head);
                i
            }
            None => {
                self.heads.push(Some(head));
                self.heads.len() - 1
            }
        }
    }

    pub fn remove(&mut self, id: HeadId) {
        if let Some(h) = self.heads.get_mut(id) {
            *h = None;
        }
    }

    pub fn heads(&self) -> usize {
        self.heads.iter().flatten().count()
    }

    /// Take wideband samples; each head's output collects until [`Self::take`].
    pub fn process(&mut self, input: &[Complex32]) {
        self.pending.extend_from_slice(input);
        let skip = self.overlap / self.d;
        let mut start = 0;
        while start + self.n <= self.pending.len() {
            self.spectrum.copy_from_slice(&self.pending[start..start + self.n]);
            self.fwd.process(&mut self.spectrum);
            // The block's first sample, absolute (the stream starts after
            // `overlap` zeros): for the per-block phase correction.
            let base = (self.block as i128 * self.hop as i128 - self.overlap as i128).rem_euclid(self.n as i128) as i64;
            let n = self.n as i64;
            let m = self.m as i64;
            for head in self.heads.iter_mut().flatten() {
                for i in 0..m {
                    let j = if i < m / 2 { i } else { i - m };
                    let k = (head.bin + j).rem_euclid(n) as usize;
                    self.bins[i as usize] = self.spectrum[k] * self.response[j.rem_euclid(n) as usize];
                }
                self.inv.process(&mut self.bins);
                let turns = (head.bin.rem_euclid(n) * base) % n;
                let c = Complex64::from_polar(1.0, -2.0 * PI * turns as f64 / n as f64);
                let c = Complex32::new(c.re as f32, c.im as f32);
                head.raw.clear();
                for &y in &self.bins[skip..] {
                    let p = Complex32::new(head.nco.re as f32, head.nco.im as f32);
                    head.raw.push(y * c * p);
                    head.nco *= head.step;
                    head.since_norm += 1;
                    if head.since_norm == 4096 {
                        head.nco /= head.nco.norm();
                        head.since_norm = 0;
                    }
                }
                head.filter.process(&head.raw, &mut head.out);
            }
            self.block += 1;
            start += self.hop;
        }
        self.pending.drain(..start);
    }

    /// Move a head's samples so far into `out` (replacing what was there).
    pub fn take(&mut self, id: HeadId, out: &mut Vec<Complex32>) {
        out.clear();
        if let Some(Some(h)) = self.heads.get_mut(id) {
            std::mem::swap(out, &mut h.out);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(fs: f64, f: f64, n: usize) -> Vec<Complex32> {
        // Phase wrapped in f64 first: as f32, millions of radians lose the tone.
        (0..n).map(|i| Complex32::from_polar(1.0, ((2.0 * PI * f * i as f64 / fs) % (2.0 * PI)) as f32)).collect()
    }

    #[test]
    fn sizes() {
        let c = Channelizer::new(6e6).unwrap();
        assert_eq!(c.output_rate(), 48_000.0);
        assert_eq!(c.n % 125, 0);
        assert_eq!(c.overlap % 125, 0);
        assert!(c.hop * 4 >= c.n * 3);
    }

    /// A tone just off a channel comes out as that small offset, at full
    /// level, with no phase jump at the block edges; the next channel over is
    /// gone.
    #[test]
    fn a_tone_comes_out_continuous() {
        let fs = 2.4e6;
        let mut c = Channelizer::new(fs).unwrap();
        let a = c.add(312_345.0);
        let b = c.add(312_345.0 + 12_500.0);
        let x = tone(fs, 312_345.0 + 300.0, 2_400_000);
        for chunk in x.chunks(10_007) {
            c.process(chunk);
        }
        let (mut ya, mut yb) = (Vec::new(), Vec::new());
        c.take(a, &mut ya);
        c.take(b, &mut yb);
        let fo = c.output_rate();
        let settled = &ya[2000..];
        let power = settled.iter().map(|s| s.norm_sqr()).sum::<f32>() / settled.len() as f32;
        assert!((power - 1.0).abs() < 0.02, "power {power}");
        for w in settled.windows(2) {
            let f = (w[1] * w[0].conj()).arg() as f64 * fo / (2.0 * PI);
            assert!((f - 300.0).abs() < 2.0, "frequency {f}");
        }
        let leak = yb[2000..].iter().map(|s| s.norm_sqr()).sum::<f32>() / (yb.len() - 2000) as f32;
        assert!(leak < 1e-4, "adjacent {leak}");
    }
}
