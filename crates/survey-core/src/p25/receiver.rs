//! A control channel's receivers: C4FM (averaged FM discriminator) and
//! CQPSK (matched filter, phase step per symbol), each with its own framer,
//! side by side. A TSBK either decodes counts once: diversity, so neither
//! receiver has to be right about the whole channel, only one of them about
//! each frame.

use std::collections::VecDeque;

use num_complex::Complex32;

use super::framer::{Event, Framer, Stats};
use super::{SiteInfo, TSBK_LEN, parse_mbt, parse_tsbk};
use crate::dsp::Discriminator;
use crate::dsp::cqpsk::Cqpsk;

/// Two receivers' copies of a frame are the same frame within this many
/// symbols (frames are 57 or more symbols apart).
const SAME_FRAME_SYMBOLS: f64 = 15.0;

/// The receivers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    C4fm,
    Cqpsk,
}

pub const KINDS: [Kind; 2] = [Kind::C4fm, Kind::Cqpsk];

/// A TSBK that passed its CRC, from either receiver.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tsbk {
    /// Channel sample index of its frame's sync (C4FM's timing).
    pub at: u64,
    pub nac: u16,
    pub octets: [u8; TSBK_LEN],
    pub by: Kind,
}

pub struct ControlChannel {
    sps: f64,
    disc: Discriminator,
    cqpsk: Cqpsk,
    framers: [Framer; 2],
    /// TSBKs already taken: (frame time, block in frame), recent ones.
    taken: VecDeque<(u64, u8)>,
    hz: Vec<f32>,
    events: Vec<Event>,
    pub site: SiteInfo,
    /// TSBKs taken from each receiver (first to decode it).
    pub first: [u32; 2],
    pub tsbks: u32,
    /// PDUs (MBT, data) that passed their CRCs, each once.
    pub pdus: u32,
}

/// The block number a PDU is noted under (a TSDU's blocks are 0..3).
const PDU_BLOCK: u8 = 0xff;

impl ControlChannel {
    pub fn new(rate_hz: f64, samples_per_symbol: f64) -> ControlChannel {
        let sps = samples_per_symbol.round() as usize;
        ControlChannel {
            sps: samples_per_symbol,
            disc: Discriminator::new(rate_hz, sps),
            cqpsk: Cqpsk::new(rate_hz, sps),
            framers: [Framer::new(samples_per_symbol), Framer::new(samples_per_symbol)],
            taken: VecDeque::new(),
            hz: Vec::new(),
            events: Vec::new(),
            site: SiteInfo::default(),
            first: [0; 2],
            tsbks: 0,
            pdus: 0,
        }
    }

    pub fn stats(&self, k: Kind) -> &Stats {
        &self.framers[k as usize].stats
    }

    /// The carrier offset the CQPSK receiver is tracking, Hz.
    pub fn offset_hz(&self) -> f64 {
        self.cqpsk.offset_hz()
    }

    /// Take channel samples; append the TSBKs decoded to `out` (and to `site`).
    pub fn push(&mut self, iq: &[Complex32], out: &mut Vec<Tsbk>) {
        for k in KINDS {
            self.hz.clear();
            let delay = match k {
                Kind::C4fm => {
                    self.disc.process(iq, &mut self.hz);
                    0
                }
                Kind::Cqpsk => {
                    self.cqpsk.process(iq, &mut self.hz);
                    self.cqpsk.delay() as u64
                }
            };
            self.events.clear();
            self.framers[k as usize].push(&self.hz, &mut self.events);
            for e in std::mem::take(&mut self.events) {
                match e {
                    Event::Frame { nid, .. } => {
                        self.site.nac_heard(nid.nac);
                    }
                    Event::Tsbk { at, block, nac, octets } => {
                        let at = at.saturating_sub(delay);
                        if self.take(at, block) {
                            self.first[k as usize] += 1;
                            self.tsbks += 1;
                            if let Some(b) = parse_tsbk(&octets) {
                                self.site.push(b);
                            }
                            out.push(Tsbk { at, nac, octets, by: k });
                        }
                    }
                    Event::Pdu { at, header, data, .. } => {
                        // A PDU is one per frame: its "block" can't be a TSBK's.
                        let at = at.saturating_sub(delay);
                        if self.take(at, PDU_BLOCK) {
                            self.pdus += 1;
                            if let Some(b) = parse_mbt(&header, &data) {
                                self.site.push(b);
                            }
                        }
                    }
                }
            }
        }
    }

    /// Whether the TSBK in `block` of the frame at `at` is new (and note it).
    fn take(&mut self, at: u64, block: u8) -> bool {
        let near = (SAME_FRAME_SYMBOLS * self.sps) as u64;
        if self.taken.iter().any(|&(t, b)| b == block && t.abs_diff(at) <= near) {
            return false;
        }
        self.taken.push_back((at, block));
        // Keep a few seconds' worth: the receivers are never further apart.
        while self.taken.len() > 512 {
            self.taken.pop_front();
        }
        true
    }
}
