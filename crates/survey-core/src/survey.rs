//! The survey, as a state machine with no I/O. For each tuning ("hop"):
//!
//! ```text
//! Tune → (caller retunes, says tuned) → Settle: drop samples while the radio
//! settles → Look: find the channels that stay on → Dwell: decode every
//! candidate at once, each until it is complete, isn't P25, or runs out of
//! time → a Record per candidate → next hop
//! ```
//!
//! All time is counted in samples, so a recording replayed faster than real
//! time behaves exactly as the air did.

use num_complex::Complex32;

use crate::dsp::Channelizer;
use crate::dsp::HeadId;
use crate::dsp::detect::{DetectConfig, Detector, Found};
use crate::p25::SiteInfo;
use crate::p25::framer::Stats;
use crate::p25::nid::duid;
use crate::p25::receiver::{ControlChannel, Kind, Tsbk};

#[derive(Clone, Debug)]
pub struct SurveyConfig {
    pub rate_hz: f64,
    /// Tuning centres, in order.
    pub hops: Vec<f64>,
    /// Only channels in this range (Hz).
    pub range: (f64, f64),
    /// Share of the sample rate searched (the edges are the radio's roll-off).
    pub usable: f64,
    /// Samples dropped after each retune, seconds.
    pub settle_s: f64,
    /// How long to look for carriers, seconds.
    pub look_s: f64,
    pub detect: DetectConfig,
    /// No P25 frame by then: not P25. No TSBK: P25 but not a control channel.
    pub confirm_s: f64,
    /// Complete once identity and band plan are in, every channel number
    /// resolves, and nothing new has come for this long.
    pub quiet_s: f64,
    pub max_dwell_s: f64,
    /// Gain sweep: after each hop's dwell, its control channels are measured
    /// at each of these gains (the radio's own units, e.g. an Airspy
    /// linearity preset). Empty: no sweep.
    pub gains: Vec<f32>,
    /// How long each gain is measured, seconds.
    pub sweep_s: f64,
    /// Channels already surveyed (a resumed survey): not decoded again.
    pub done_hz: Vec<f64>,
    /// Only look for carriers: report each hop's candidates, decode none.
    pub detect_only: bool,
}

impl SurveyConfig {
    /// Defaults for samples at `rate_hz` over `range`, hops planned to cover it.
    pub fn new(rate_hz: f64, range: (f64, f64)) -> SurveyConfig {
        let usable = 0.85;
        SurveyConfig {
            rate_hz,
            hops: plan_hops(range, rate_hz, usable),
            range,
            usable,
            settle_s: 0.2,
            look_s: 0.35,
            detect: DetectConfig::default(),
            confirm_s: 2.0,
            quiet_s: 3.0,
            max_dwell_s: 12.0,
            gains: Vec::new(),
            sweep_s: 3.0,
            done_hz: Vec::new(),
            detect_only: false,
        }
    }
}

/// Centres that cover `range` with `usable` of `rate_hz` each.
pub fn plan_hops(range: (f64, f64), rate_hz: f64, usable: f64) -> Vec<f64> {
    let width = rate_hz * usable;
    let span = range.1 - range.0;
    let n = (span / width).ceil().max(1.0) as usize;
    // Spread evenly, so the overlap is shared.
    let step = span / n as f64;
    (0..n).map(|i| ((range.0 + step * (i as f64 + 0.5)) / 1000.0).round() * 1000.0).collect()
}

/// The channels that stood out in a hop, within the range and the usable band.
fn candidates(cfg: &SurveyConfig, center: f64, det: &Detector) -> Vec<Found> {
    let half = cfg.rate_hz / 2.0 * cfg.usable;
    let lo = (center - half).max(cfg.range.0);
    let hi = (center + half).min(cfg.range.1);
    det.find(center, lo, hi, &cfg.detect)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Command {
    /// Retune the radio to this centre, then call [`Survey::tuned`].
    Tune(f64),
    /// Set the radio's gain (None: back to the gain the survey started
    /// with), then call [`Survey::gain_applied`].
    Gain(Option<f32>),
}

/// One gain of a sweep: TSBKs each control channel decoded, and the share of
/// samples near full scale.
#[derive(Clone, Debug)]
pub struct GainStep {
    pub gain: f32,
    pub tsbks: Vec<u32>,
    pub clipped: f64,
}

/// A hop's gain sweep: its control channels, measured at each gain.
#[derive(Clone, Debug)]
pub struct GainSweep {
    pub hop: usize,
    pub center_hz: f64,
    pub freqs: Vec<f64>,
    pub steps: Vec<GainStep>,
    /// Seconds each gain was measured.
    pub seconds: f64,
}

/// Within this share of the best score, the lower gain is taken.
const GAIN_MARGIN: f64 = 0.02;

/// Score each gain: per channel, TSBKs decoded as a share of that channel's
/// best gain's (so a strong channel doesn't drown out a weak one), averaged
/// over the channels. `tsbks[g][c]`: gain g, channel c. A frame never synced
/// counts against a gain here, as it wouldn't in a CRC pass rate.
pub fn gain_scores(tsbks: &[Vec<u32>]) -> Vec<f64> {
    let channels = tsbks.first().map_or(0, Vec::len);
    let best: Vec<u32> = (0..channels).map(|c| tsbks.iter().map(|g| g[c]).max().unwrap_or(0)).collect();
    let used: Vec<usize> = (0..channels).filter(|&c| best[c] > 0).collect();
    tsbks.iter().map(|g| if used.is_empty() { 0.0 } else { used.iter().map(|&c| g[c] as f64 / best[c] as f64).sum::<f64>() / used.len() as f64 }).collect()
}

/// The gain to use: the lowest within 2 % of the best score (less gain is
/// more headroom for strong signals). `gains` ascending or not.
pub fn best_gain(gains: &[f32], scores: &[f64]) -> Option<f32> {
    let top = scores.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    if top.is_nan() || top <= 0.0 {
        return None;
    }
    gains.iter().zip(scores).filter(|&(_, &s)| s >= top - GAIN_MARGIN).map(|(&g, _)| g).min_by(f32::total_cmp)
}

impl GainSweep {
    pub fn scores(&self) -> Vec<f64> {
        gain_scores(&self.steps.iter().map(|s| s.tsbks.clone()).collect::<Vec<_>>())
    }

    pub fn best(&self) -> Option<f32> {
        best_gain(&self.steps.iter().map(|s| s.gain).collect::<Vec<_>>(), &self.scores())
    }
}

/// How a candidate ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// Identity, band plan and its own channel; then every channel number
    /// resolved and nothing new for a while (or the dwell's end).
    Complete,
    /// A control channel, but the dwell ran out first.
    Partial,
    /// P25 frames, but no TSBKs: voice, data or conventional.
    NotControl,
    /// No P25 frame.
    NotP25,
}

/// Announced within this of where it was heard, a channel is that one, found
/// a raster step or two off.
const SAME_CHANNEL_HZ: f64 = 12_500.0;

/// What a candidate came to.
#[derive(Clone, Debug)]
pub struct Record {
    /// The channel: where it was found, or where it announces it is if that
    /// is within a raster step or two ([`Record::heard_hz`] keeps the other).
    pub freq_hz: f64,
    pub heard_hz: f64,
    /// dB over the local floor in the look.
    pub level_db: f64,
    pub hop: usize,
    pub verdict: Verdict,
    pub dwell_s: f64,
    pub site: SiteInfo,
    pub c4fm: Stats,
    pub cqpsk: Stats,
    /// TSBKs that passed their CRC (either receiver, once).
    pub tsbks: u32,
    /// Carrier offset the CQPSK receiver settled on, Hz.
    pub offset_hz: f64,
    /// Not a control channel of its own but a copy of the one at this
    /// frequency ([`mark_copies`]).
    pub copy_of: Option<f64>,
}

impl Record {
    pub fn is_control(&self) -> bool {
        matches!(self.verdict, Verdict::Complete | Verdict::Partial)
    }

    /// NAC, system, RFSS and site, if all were heard.
    fn identity(&self) -> Option<(u16, u16, u8, u8)> {
        Some((self.site.nac?, self.site.sysid?, self.site.rfss?, self.site.site?))
    }

    /// The site announces another frequency than the one it was heard on: a
    /// copy (an image, intermodulation) of a control channel elsewhere.
    pub fn announced_elsewhere(&self) -> Option<u64> {
        self.site.announced_hz().filter(|&a| (a as f64 - self.freq_hz).abs() > 3000.0)
    }

    /// It announces the frequency it was heard on: a control channel of its
    /// own, whatever else is about.
    pub fn announces_itself(&self) -> bool {
        self.site.announced_hz().is_some_and(|a| (a as f64 - self.freq_hz).abs() <= 3000.0)
    }

    /// Share of TSBKs attempted that passed, by the better receiver.
    pub fn decode_rate(&self) -> Option<f64> {
        [&self.c4fm, &self.cqpsk].iter().filter(|s| s.tsbk_ok + s.tsbk_bad > 0).map(|s| s.tsbk_ok as f64 / (s.tsbk_ok + s.tsbk_bad) as f64).reduce(f64::max)
    }
}

/// What the survey reports as it goes.
#[derive(Clone, Debug)]
pub enum Output {
    /// Looking at hop `index`, tuned to `center_hz`.
    Hop {
        index: usize,
        center_hz: f64,
    },
    /// The channels that stood out there.
    Candidates {
        index: usize,
        found: Vec<Found>,
    },
    Record(Box<Record>),
    GainSweep(Box<GainSweep>),
    Done,
}

struct Dwelling {
    found: Found,
    head: HeadId,
    cc: ControlChannel,
    iq: Vec<Complex32>,
    tsbks: Vec<Tsbk>,
    /// Samples (wideband) when its site last told us something new.
    last_new: u64,
    seen_new: u32,
}

enum State {
    /// Waiting for the caller to say it has tuned.
    Tuning,
    Settle {
        left: u64,
    },
    Look {
        det: Box<Detector>,
        left: u64,
    },
    Dwell {
        chz: Box<Channelizer>,
        chans: Vec<Dwelling>,
        t: u64,
    },
    Sweep(Box<Sweep>),
    Done,
}

enum SweepPhase {
    /// Waiting for the caller to say the gain is set.
    Waiting,
    Settle,
    Measure,
}

struct Sweep {
    chz: Channelizer,
    /// (head, receivers) per control channel, in `result.freqs` order.
    chans: Vec<(HeadId, ControlChannel)>,
    phase: SweepPhase,
    left: u64,
    hot: u64,
    total: u64,
    iq: Vec<Complex32>,
    tsbks: Vec<Tsbk>,
    result: GainSweep,
}

pub struct Survey {
    cfg: SurveyConfig,
    /// Control channels already complete (an earlier hop overlapping this one).
    complete: Vec<f64>,
    hop: usize,
    center: f64,
    state: State,
    pending: std::collections::VecDeque<Command>,
    /// The hop's control channels, for its gain sweep.
    hop_controls: Vec<f64>,
}

impl Survey {
    pub fn new(cfg: SurveyConfig) -> Survey {
        let first = cfg.hops.first().copied();
        Survey {
            complete: cfg.done_hz.clone(),
            hop: 0,
            center: first.unwrap_or(0.0),
            state: if first.is_some() { State::Tuning } else { State::Done },
            pending: first.map(Command::Tune).into_iter().collect(),
            hop_controls: Vec::new(),
            cfg,
        }
    }

    pub fn config(&self) -> &SurveyConfig {
        &self.cfg
    }

    /// What the radio should do next, in order (each taken once).
    pub fn command(&mut self) -> Option<Command> {
        self.pending.pop_front()
    }

    /// The radio has the gain last asked for.
    pub fn gain_applied(&mut self) {
        if let State::Sweep(sw) = &mut self.state
            && matches!(sw.phase, SweepPhase::Waiting)
        {
            sw.phase = SweepPhase::Settle;
            sw.left = (self.cfg.settle_s * self.cfg.rate_hz) as u64;
        }
    }

    pub fn is_done(&self) -> bool {
        matches!(self.state, State::Done)
    }

    /// The radio is tuned to `center_hz`; samples from now on are from there.
    pub fn tuned(&mut self, center_hz: f64, out: &mut Vec<Output>) {
        if matches!(self.state, State::Tuning) {
            self.center = center_hz;
            out.push(Output::Hop { index: self.hop, center_hz });
            self.state = State::Settle { left: (self.cfg.settle_s * self.cfg.rate_hz) as u64 };
        }
    }

    /// Take samples from the radio.
    pub fn push(&mut self, mut x: &[Complex32], out: &mut Vec<Output>) {
        while !x.is_empty() {
            match &mut self.state {
                State::Tuning | State::Done => return,
                State::Settle { left } => {
                    let n = (*left as usize).min(x.len());
                    *left -= n as u64;
                    x = &x[n..];
                    if *left == 0 {
                        self.state = State::Look { det: Box::new(Detector::new(self.cfg.rate_hz)), left: (self.cfg.look_s * self.cfg.rate_hz) as u64 };
                    }
                }
                State::Look { det, left } => {
                    let n = (*left as usize).min(x.len());
                    det.push(&x[..n]);
                    *left -= n as u64;
                    x = &x[n..];
                    if *left == 0 {
                        let found = candidates(&self.cfg, self.center, det);
                        self.start_dwell(found, out);
                    }
                }
                State::Dwell { chz, chans, t } => {
                    chz.process(x);
                    *t += x.len() as u64;
                    x = &[];
                    for d in chans.iter_mut() {
                        chz.take(d.head, &mut d.iq);
                        d.tsbks.clear();
                        d.cc.push(&d.iq, &mut d.tsbks);
                    }
                    self.conclude(false, out);
                }
                State::Sweep(sw) => match sw.phase {
                    SweepPhase::Waiting => return,
                    SweepPhase::Settle => {
                        let n = (sw.left as usize).min(x.len());
                        sw.chz.process(&x[..n]);
                        sw.left -= n as u64;
                        x = &x[n..];
                        if sw.left == 0 {
                            // Fresh receivers: only this gain's air counts.
                            let (rate, sps) = (sw.chz.output_rate(), sw.chz.samples_per_symbol());
                            for (head, cc) in sw.chans.iter_mut() {
                                sw.chz.take(*head, &mut sw.iq);
                                *cc = ControlChannel::new(rate, sps);
                            }
                            sw.phase = SweepPhase::Measure;
                            sw.left = (self.cfg.sweep_s * self.cfg.rate_hz) as u64;
                            (sw.hot, sw.total) = (0, 0);
                        }
                    }
                    SweepPhase::Measure => {
                        let n = (sw.left as usize).min(x.len());
                        let part = &x[..n];
                        sw.hot += part.iter().filter(|s| s.re.abs() > 0.95 || s.im.abs() > 0.95).count() as u64;
                        sw.total += n as u64;
                        sw.chz.process(part);
                        for (head, cc) in sw.chans.iter_mut() {
                            sw.chz.take(*head, &mut sw.iq);
                            sw.tsbks.clear();
                            cc.push(&sw.iq, &mut sw.tsbks);
                        }
                        sw.left -= n as u64;
                        x = &x[n..];
                        if sw.left == 0 {
                            self.sweep_step_done(out);
                        }
                    }
                },
            }
        }
    }

    /// Start the hop's gain sweep, or go on to the next hop.
    fn after_dwell(&mut self, out: &mut Vec<Output>) {
        let freqs = std::mem::take(&mut self.hop_controls);
        let Some(&first) = self.cfg.gains.first() else {
            return self.next_hop(out);
        };
        let Ok(mut chz) = Channelizer::new(self.cfg.rate_hz) else {
            return self.next_hop(out);
        };
        if freqs.is_empty() {
            return self.next_hop(out);
        }
        let chans = freqs.iter().map(|&f| (chz.add(f - self.center), ControlChannel::new(chz.output_rate(), chz.samples_per_symbol()))).collect();
        self.state = State::Sweep(Box::new(Sweep {
            chz,
            chans,
            phase: SweepPhase::Waiting,
            left: 0,
            hot: 0,
            total: 0,
            iq: Vec::new(),
            tsbks: Vec::new(),
            result: GainSweep { hop: self.hop, center_hz: self.center, freqs, steps: Vec::new(), seconds: self.cfg.sweep_s },
        }));
        self.pending.push_back(Command::Gain(Some(first)));
    }

    fn sweep_step_done(&mut self, out: &mut Vec<Output>) {
        let State::Sweep(sw) = &mut self.state else { return };
        let gain = self.cfg.gains[sw.result.steps.len()];
        let tsbks = sw.chans.iter().map(|(_, cc)| cc.tsbks).collect();
        sw.result.steps.push(GainStep { gain, tsbks, clipped: sw.hot as f64 / sw.total.max(1) as f64 });
        match self.cfg.gains.get(sw.result.steps.len()) {
            Some(&next) => {
                sw.phase = SweepPhase::Waiting;
                self.pending.push_back(Command::Gain(Some(next)));
            }
            None => {
                out.push(Output::GainSweep(Box::new(sw.result.clone())));
                self.pending.push_back(Command::Gain(None));
                self.next_hop(out);
            }
        }
    }

    /// Stop now (the user asked): the hop's candidates are concluded with
    /// what they have, and the survey is done.
    pub fn stop(&mut self, out: &mut Vec<Output>) {
        if matches!(self.state, State::Done) {
            return;
        }
        if matches!(self.state, State::Dwell { .. }) {
            // Concluding the last candidate would move on to the next hop.
            self.cfg.hops.truncate(self.hop + 1);
            self.cfg.gains.clear();
            self.conclude(true, out);
        }
        if !matches!(self.state, State::Done) {
            self.state = State::Done;
            out.push(Output::Done);
        }
        self.pending.clear();
    }

    /// The samples have run out (a recording ended): finish the hop with
    /// what there is, and go on to the next.
    pub fn end_of_samples(&mut self, out: &mut Vec<Output>) {
        match self.state {
            State::Dwell { .. } => self.conclude(true, out),
            State::Done | State::Tuning => {}
            State::Sweep(_) => {
                self.pending.push_back(Command::Gain(None));
                self.next_hop(out);
            }
            _ => self.next_hop(out),
        }
    }

    fn start_dwell(&mut self, mut found: Vec<Found>, out: &mut Vec<Output>) {
        found.retain(|f| !self.complete.iter().any(|&c| (c - f.freq_hz).abs() <= SAME_CHANNEL_HZ));
        out.push(Output::Candidates { index: self.hop, found: found.clone() });
        self.hop_controls.clear();
        if found.is_empty() || self.cfg.detect_only {
            self.next_hop(out);
            return;
        }
        let mut chz = match Channelizer::new(self.cfg.rate_hz) {
            Ok(c) => Box::new(c),
            Err(_) => {
                self.next_hop(out);
                return;
            }
        };
        let chans = found
            .into_iter()
            .map(|f| Dwelling {
                head: chz.add(f.freq_hz - self.center),
                cc: ControlChannel::new(chz.output_rate(), chz.samples_per_symbol()),
                found: f,
                iq: Vec::new(),
                tsbks: Vec::new(),
                last_new: 0,
                seen_new: 0,
            })
            .collect();
        self.state = State::Dwell { chz, chans, t: 0 };
    }

    /// Send off every candidate that has its answer (all of them if `all`);
    /// when none is left, the next hop.
    fn conclude(&mut self, all: bool, out: &mut Vec<Output>) {
        let State::Dwell { chz, chans, t } = &mut self.state else { return };
        let rate = self.cfg.rate_hz;
        let secs = *t as f64 / rate;
        let mut i = 0;
        while i < chans.len() {
            let d = &mut chans[i];
            let site = &d.cc.site;
            if site.new != d.seen_new {
                d.seen_new = site.new;
                d.last_new = *t;
            }
            let (c4fm, cqpsk) = (d.cc.stats(Kind::C4fm), d.cc.stats(Kind::Cqpsk));
            let frames = c4fm.frames_total() + cqpsk.frames_total();
            let tsdus = c4fm.frames[duid::TSDU as usize] + cqpsk.frames[duid::TSDU as usize];
            let quiet = (*t - d.last_new) as f64 / rate;
            // Some sites name channels whose band plan entry they never send:
            // those wait for twice the quiet.
            let settled = quiet >= self.cfg.quiet_s && (site.unresolved() == 0 || quiet >= 2.0 * self.cfg.quiet_s);
            let known = site.has_identity() && !site.plan.is_empty() && site.announced_hz().is_some();
            // At the dwell's end, everything heard resolving is complete too:
            // some sites take longer than a dwell to cycle their broadcasts.
            let complete = known && (settled || (secs >= self.cfg.max_dwell_s && site.unresolved() == 0));
            let verdict = if complete {
                Some(Verdict::Complete)
            } else if secs >= self.cfg.confirm_s && frames == 0 {
                Some(Verdict::NotP25)
            } else if secs >= self.cfg.confirm_s && d.cc.tsbks == 0 && tsdus == 0 {
                // Voice, data or conventional. (TSDUs that haven't passed a CRC
                // yet are a weak control channel: it stays.)
                Some(Verdict::NotControl)
            } else if secs >= self.cfg.max_dwell_s || all {
                Some(if d.cc.tsbks > 0 {
                    Verdict::Partial
                } else if frames > 0 {
                    Verdict::NotControl
                } else {
                    Verdict::NotP25
                })
            } else {
                None
            };
            if let Some(verdict) = verdict {
                let d = chans.swap_remove(i);
                chz.remove(d.head);
                let heard = d.found.freq_hz;
                let freq_hz = d.cc.site.announced_hz().map(|a| a as f64).filter(|a| (a - heard).abs() <= SAME_CHANNEL_HZ).unwrap_or(heard);
                if verdict == Verdict::Complete {
                    self.complete.push(freq_hz);
                }
                let announces_itself = d.cc.site.announced_hz().is_some_and(|a| (a as f64 - freq_hz).abs() <= 3000.0);
                if matches!(verdict, Verdict::Complete | Verdict::Partial) && announces_itself {
                    self.hop_controls.push(freq_hz);
                }
                out.push(Output::Record(Box::new(Record {
                    freq_hz,
                    heard_hz: heard,
                    level_db: d.found.level_db,
                    hop: self.hop,
                    verdict,
                    dwell_s: secs,
                    c4fm: d.cc.stats(Kind::C4fm).clone(),
                    cqpsk: d.cc.stats(Kind::Cqpsk).clone(),
                    tsbks: d.cc.tsbks,
                    offset_hz: d.cc.offset_hz(),
                    copy_of: None,
                    site: d.cc.site,
                })));
            } else {
                i += 1;
            }
        }
        if chans.is_empty() {
            self.after_dwell(out);
        }
    }

    fn next_hop(&mut self, out: &mut Vec<Output>) {
        self.hop += 1;
        match self.cfg.hops.get(self.hop) {
            Some(&c) => {
                self.state = State::Tuning;
                self.pending.push_back(Command::Tune(c));
            }
            None => {
                self.state = State::Done;
                out.push(Output::Done);
            }
        }
    }
}

/// Mark the control channels that are copies of others: it announces
/// another's frequency; or it has the same NAC, system, RFSS and site as one
/// that decodes better; or it sits at the mirror image (about the tuning
/// centre) of one at least 10 dB stronger with the same NAC, in the same hop:
/// the radio's own image. One that announces its own frequency is never a
/// copy. `hops` are the tuning centres the records' `hop` indexes.
pub fn mark_copies(records: &mut [Record], hops: &[f64]) {
    let marks: Vec<Option<f64>> = (0..records.len())
        .map(|i| {
            let r = &records[i];
            if !r.is_control() {
                return None;
            }
            if let Some(a) = r.announced_elsewhere() {
                return Some(a as f64);
            }
            if r.announces_itself() {
                return None;
            }
            records.iter().enumerate().filter(|&(j, g)| j != i && g.is_control() && g.announced_elsewhere().is_none()).find_map(|(_, g)| {
                let same = r.identity().is_some() && r.identity() == g.identity() && (g.tsbks, g.level_db) > (r.tsbks, r.level_db);
                let mirror =
                    g.hop == r.hop && hops.get(r.hop).is_some_and(|&c| (r.freq_hz - (2.0 * c - g.freq_hz)).abs() < 3000.0) && g.level_db >= r.level_db + 10.0;
                (same || mirror).then_some(g.freq_hz)
            })
        })
        .collect();
    for (r, m) in records.iter_mut().zip(marks) {
        r.copy_of = m;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hops_cover_the_range() {
        let h = plan_hops((851e6, 869e6), 6e6, 0.85);
        assert_eq!(h.len(), 4);
        let w = 6e6 * 0.85 / 2.0;
        assert!(h[0] - w <= 851e6 && h[3] + w >= 869e6);
        assert!(h.windows(2).all(|p| p[1] - p[0] <= 2.0 * w));
        assert_eq!(plan_hops((769e6, 775e6), 6e6, 0.85).len(), 2);
    }

    #[test]
    fn the_gain_that_decodes_most_everywhere_lowest_first() {
        // Two channels: a strong one fine at any gain, a weak one that needs
        // gain until the front end overloads.
        let tsbks = vec![vec![300, 0], vec![300, 40], vec![300, 100], vec![299, 101], vec![250, 60]];
        let gains = [4.0, 8.0, 12.0, 16.0, 20.0];
        let s = gain_scores(&tsbks);
        assert_eq!(best_gain(&gains, &s), Some(12.0));
        assert_eq!(best_gain(&gains, &gain_scores(&[vec![0], vec![0]])), None);
    }

    #[test]
    fn stopping_ends_it() {
        let mut cfg = SurveyConfig::new(2.4e6, (851e6, 853e6));
        cfg.hops = vec![851.5e6, 852.5e6];
        let mut s = Survey::new(cfg);
        let mut out = Vec::new();
        let Some(Command::Tune(c)) = s.command() else { panic!() };
        s.tuned(c, &mut out);
        s.stop(&mut out);
        assert!(s.is_done() && s.command().is_none());
        assert!(matches!(out.last(), Some(Output::Done)));
        s.stop(&mut out);
        assert_eq!(out.iter().filter(|o| matches!(o, Output::Done)).count(), 1);
    }

    #[test]
    fn silence_ends_each_hop_with_nothing() {
        let mut cfg = SurveyConfig::new(2.4e6, (851e6, 853e6));
        cfg.hops = vec![851.5e6, 852.5e6];
        let mut s = Survey::new(cfg);
        let mut out = Vec::new();
        let zeros = vec![Complex32::new(0.0, 0.0); 1 << 16];
        let mut hops = 0;
        while !s.is_done() {
            if let Some(Command::Tune(c)) = s.command() {
                s.tuned(c, &mut out);
                hops += 1;
            }
            s.push(&zeros, &mut out);
        }
        assert_eq!(hops, 2);
        assert!(matches!(out.last(), Some(Output::Done)));
        assert!(!out.iter().any(|o| matches!(o, Output::Record(_))));
    }
}
