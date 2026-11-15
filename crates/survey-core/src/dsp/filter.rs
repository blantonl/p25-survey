//! FIR filters: windowed-sinc design and a decimating filter.

use num_complex::Complex32;

/// A low-pass FIR: Blackman-windowed sinc, `taps` long (made odd), cutoff
/// as a fraction of the sample rate, unity gain at DC.
pub fn lowpass(taps: usize, cutoff: f64) -> Vec<f32> {
    let n = taps | 1;
    let m = (n - 1) as f64;
    let h: Vec<f64> = (0..n)
        .map(|i| {
            let x = i as f64 - m / 2.0;
            let sinc = if x == 0.0 { 2.0 * cutoff } else { (2.0 * std::f64::consts::PI * cutoff * x).sin() / (std::f64::consts::PI * x) };
            let w = 0.42 - 0.5 * (2.0 * std::f64::consts::PI * i as f64 / m).cos() + 0.08 * (4.0 * std::f64::consts::PI * i as f64 / m).cos();
            sinc * w
        })
        .collect();
    let sum: f64 = h.iter().sum();
    h.iter().map(|v| (v / sum) as f32).collect()
}

/// Taps for a Blackman low-pass with transition width `width` (fraction of
/// the sample rate).
pub fn taps_for(width: f64) -> usize {
    ((5.5 / width).ceil() as usize).max(3) | 1
}

/// A FIR that keeps every `d`th output.
pub struct Decimator {
    taps: Vec<f32>,
    d: usize,
    buf: Vec<Complex32>,
    /// Where in `buf` the next output's window starts.
    next: usize,
}

impl Decimator {
    pub fn new(taps: Vec<f32>, d: usize) -> Decimator {
        Decimator { taps, d: d.max(1), buf: Vec::new(), next: 0 }
    }

    /// Filter `input`, appending the outputs to `out`.
    pub fn process(&mut self, input: &[Complex32], out: &mut Vec<Complex32>) {
        self.buf.extend_from_slice(input);
        let l = self.taps.len();
        while self.next + l <= self.buf.len() {
            let w = &self.buf[self.next..self.next + l];
            let (mut re, mut im) = (0f32, 0f32);
            for (x, &t) in w.iter().zip(&self.taps) {
                re += x.re * t;
                im += x.im * t;
            }
            out.push(Complex32::new(re, im));
            self.next += self.d;
        }
        let used = self.next.min(self.buf.len());
        self.buf.drain(..used);
        self.next -= used;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(f: f64, n: usize) -> Vec<Complex32> {
        (0..n).map(|i| Complex32::from_polar(1.0, (2.0 * std::f64::consts::PI * f * i as f64) as f32)).collect()
    }

    fn power(x: &[Complex32]) -> f32 {
        x.iter().map(|s| s.norm_sqr()).sum::<f32>() / x.len() as f32
    }

    #[test]
    fn passes_in_band_and_stops_out_of_band() {
        let h = lowpass(taps_for(0.05), 0.1);
        let mut d = Decimator::new(h, 4);
        let mut out = Vec::new();
        d.process(&tone(0.02, 4000), &mut out);
        assert!((power(&out[50..]) - 1.0).abs() < 0.01, "passband {}", power(&out[50..]));
        out.clear();
        let mut d = Decimator::new(lowpass(taps_for(0.05), 0.1), 4);
        d.process(&tone(0.2, 4000), &mut out);
        assert!(power(&out[50..]) < 1e-6, "stopband {}", power(&out[50..]));
    }

    #[test]
    fn blocks_make_no_difference() {
        let x = tone(0.03, 1000);
        let (mut a, mut b) = (Vec::new(), Vec::new());
        Decimator::new(lowpass(31, 0.1), 3).process(&x, &mut a);
        let mut d = Decimator::new(lowpass(31, 0.1), 3);
        for c in x.chunks(7) {
            d.process(c, &mut b);
        }
        assert_eq!(a, b);
    }
}
