//! The second look at the advised gain. On hqz's 700 MHz antenna, at the
//! survey's default gain (Airspy linearity 14), KaufTxWarn's control channel
//! on 771.20625 is a carrier just over the noise that decodes nothing; at the
//! gain the radio's owner runs it at, it is a complete control channel. Two
//! recordings of the same 10 s of air, one at each gain, play the radio:
//! the first at the survey's gain, the second at any gain above it. The
//! gain sweep must advise the higher gain, and the look at it must find the
//! channel. Skipped (passes) when the recordings aren't in corpus/raw.

use std::path::PathBuf;

use num_complex::Complex32;
use survey_core::survey::{Command, Output, Survey, SurveyConfig, Verdict};
use survey_radio::sigmf::{Reader, Recording};

const KAUFTXWARN_HZ: f64 = 771_206_250.0;

fn recording(name: &str) -> Option<Recording> {
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../corpus/raw").join(name);
    Recording::open(&base).ok()
}

/// The same air at two gains, played from where the survey has got to.
struct Air {
    low: Recording,
    high: Recording,
    readers: (Reader, Reader),
    blocks: (Vec<Complex32>, Vec<Complex32>),
}

impl Air {
    fn read(&mut self, high: bool, n: usize) -> &[Complex32] {
        let got = (self.readers.0.read(&mut self.blocks.0, n).unwrap(), self.readers.1.read(&mut self.blocks.1, n).unwrap());
        if got.0 < n || got.1 < n {
            // Round again, both from the start: the two stay the same air.
            self.readers = (self.low.reader().unwrap(), self.high.reader().unwrap());
            self.readers.0.read(&mut self.blocks.0, n).unwrap();
            self.readers.1.read(&mut self.blocks.1, n).unwrap();
        }
        if high { &self.blocks.1 } else { &self.blocks.0 }
    }
}

#[test]
fn the_advised_gain_finds_what_the_survey_gain_missed() {
    let (Some(low), Some(high)) = (recording("hqz-770.5-lin14"), recording("hqz-770.5-stages")) else {
        eprintln!("hqz recordings not here, skipped");
        return;
    };
    let mut cfg = SurveyConfig::new(low.rate_hz, (769e6, 773e6));
    cfg.hops = vec![low.center_hz];
    cfg.gains = vec![14.0, 21.0];
    cfg.start_gain = Some(14.0);
    let readers = (low.reader().unwrap(), high.reader().unwrap());
    let mut air = Air { low, high, readers, blocks: (Vec::new(), Vec::new()) };

    let mut survey = Survey::new(cfg);
    let (mut out, mut first, mut second) = (Vec::new(), Vec::new(), Vec::new());
    let (mut gain_high, mut relooked_at) = (false, None);
    let mut seconds = 0.0;
    while !survey.is_done() {
        while let Some(c) = survey.command() {
            match c {
                Command::Tune(hz) => survey.tuned(hz, &mut out),
                Command::Gain(g) => {
                    gain_high = g.is_some_and(|g| g > 14.5);
                    survey.gain_applied();
                }
            }
        }
        let block = air.read(gain_high, 1 << 16);
        seconds += block.len() as f64 / 6e6;
        assert!(seconds < 120.0, "the survey never ended");
        survey.push(block, &mut out);
        for o in out.drain(..) {
            match o {
                Output::Relook { gain, .. } => relooked_at = Some(gain),
                Output::Record(r) if relooked_at.is_some() => second.push(*r),
                Output::Record(r) => first.push(*r),
                _ => {}
            }
        }
    }

    let near = |r: &&survey_core::survey::Record| (r.freq_hz - KAUFTXWARN_HZ).abs() < 4000.0;
    assert!(!first.iter().filter(near).any(|r| r.is_control()), "decoded at the survey's gain: the recordings don't show the miss");
    assert_eq!(relooked_at, Some(21.0), "the sweep should advise the higher gain, and the hop be looked at again");
    let r = second.iter().find(near).expect("771.20625 not a candidate at the advised gain");
    assert_eq!(r.verdict, Verdict::Complete);
    assert_eq!((r.site.wacn, r.site.sysid, r.site.nac), (Some(0xBEE00), Some(0x1A7), Some(0x1A0)));
    // What decoded as a control channel the first time isn't decoded again.
    assert!(!second.iter().any(|s| first.iter().any(|f| f.verdict == Verdict::Complete && (f.freq_hz - s.freq_hz).abs() < 12_500.0)));
}
