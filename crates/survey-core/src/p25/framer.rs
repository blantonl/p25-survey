//! Frames from a C4FM channel's symbol-averaged frequency: find the frame
//! sync, take the symbol timing and the four levels from it, read the NID,
//! and for a TSDU decode its TSBKs.
//!
//! Timing is taken afresh at every frame sync. A control channel sends one
//! frame after another, each a few hundred symbols, so the drift of any
//! radio's clock within a frame is a tiny fraction of a symbol.

use super::TSBK_LEN;
use super::crc::{crc32, tsbk_ok};
use super::nid::{self, Nid, duid};
use super::trellis::{BLOCK_DIBITS, decode_half_rate, deinterleave};

/// Frame sync: 48 bits, 24 dibits, all of them outer symbols.
const FRAME_SYNC: u64 = 0x5575_F5FF_77FF;
const SYNC_SYMBOLS: usize = 24;
/// NID dibits.
const NID_DIBITS: usize = 32;
/// Most TSBKs in a TSDU.
const MAX_BLOCKS: usize = 3;
/// Data blocks of a PDU read (a site broadcast MBT has one to three).
const MAX_PDU_BLOCKS: usize = 3;
/// Symbols a frame can span, status symbols included: a PDU's header and
/// three data blocks (a TSDU of three blocks is shorter).
const MAX_FRAME_SYMBOLS: usize = 470;
/// Normalised correlation with the sync pattern that starts a look at a NID.
const SYNC_THRESHOLD: f32 = 0.75;

/// Sync symbol k as +1 / -1 (dibit 01 = +3, 11 = -3).
fn sync_sign(k: usize) -> f32 {
    if FRAME_SYNC >> (46 - 2 * k) & 3 == 1 { 1.0 } else { -1.0 }
}

/// Every 36th symbol of a frame (counting the sync) is a status symbol, not data.
fn is_status(symbol: usize) -> bool {
    (symbol + 1).is_multiple_of(36)
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    /// Sync correlations strong enough to look for a NID after.
    pub syncs: u32,
    /// Frames with a NID that decoded, by DUID.
    pub frames: [u32; 16],
    /// NID bit errors corrected, summed.
    pub nid_errors: u32,
    pub tsbk_ok: u32,
    pub tsbk_bad: u32,
    /// Trellis bit errors corrected over every block, summed.
    pub trellis_errors: u32,
    /// PDUs whose header and data passed their CRCs, and those that didn't.
    pub pdu_ok: u32,
    pub pdu_bad: u32,
}

impl Stats {
    pub fn frames_total(&self) -> u32 {
        self.frames.iter().sum()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Event {
    /// A frame: where its sync starts (channel sample index) and its NID.
    Frame { at: u64, nid: Nid },
    /// A TSBK that passed its CRC: its frame's sync, which block of the
    /// frame it is (0..3), and its message octets.
    Tsbk { at: u64, block: u8, nac: u16, octets: [u8; TSBK_LEN] },
    /// A PDU (unconfirmed or MBT) whose header and data passed their CRCs:
    /// the header's 10 octets, and the data's octets without the CRC-32.
    Pdu { at: u64, nac: u16, header: [u8; TSBK_LEN], data: Vec<u8> },
}

pub struct Framer {
    sps: f64,
    /// Sample offsets of the sync symbols from the first.
    sync_at: [usize; SYNC_SYMBOLS],
    sync_mean: f32,
    sync_norm: f32,
    /// Channel samples (Hz, averaged over a symbol) not yet looked at.
    buf: Vec<f32>,
    /// Channel sample index of buf[0].
    start: u64,
    /// Where in buf the search for a sync goes on from.
    i: usize,
    /// The NAC heard, tried first at each NID.
    nac: Option<u16>,
    pub stats: Stats,
}

impl Framer {
    pub fn new(samples_per_symbol: f64) -> Framer {
        let sync_at = std::array::from_fn(|k| (k as f64 * samples_per_symbol).round() as usize);
        let mean = (0..SYNC_SYMBOLS).map(sync_sign).sum::<f32>() / SYNC_SYMBOLS as f32;
        let norm = (0..SYNC_SYMBOLS).map(|k| (sync_sign(k) - mean).powi(2)).sum::<f32>().sqrt();
        Framer { sps: samples_per_symbol, sync_at, sync_mean: mean, sync_norm: norm, buf: Vec::new(), start: 0, i: 0, nac: None, stats: Stats::default() }
    }

    fn at(&self, symbol: usize) -> usize {
        (symbol as f64 * self.sps).round() as usize
    }

    /// Normalised correlation of the samples from `i` with the sync pattern
    /// (its sign says the polarity).
    fn correlation(&self, i: usize) -> f32 {
        let y = |k: usize| self.buf[i + self.sync_at[k]];
        let mean = (0..SYNC_SYMBOLS).map(y).sum::<f32>() / SYNC_SYMBOLS as f32;
        let (mut num, mut var) = (0f32, 0f32);
        for k in 0..SYNC_SYMBOLS {
            let d = y(k) - mean;
            num += (sync_sign(k) - self.sync_mean) * d;
            var += d * d;
        }
        if var <= 0.0 { 0.0 } else { num / (var.sqrt() * self.sync_norm) }
    }

    /// Take symbol-averaged samples; append what was decoded to `out`.
    pub fn push(&mut self, samples: &[f32], out: &mut Vec<Event>) {
        self.buf.extend_from_slice(samples);
        let need = self.at(MAX_FRAME_SYMBOLS) + 2;
        while self.i + need <= self.buf.len() {
            let r = self.correlation(self.i);
            if r.abs() < SYNC_THRESHOLD {
                self.i += 1;
                continue;
            }
            // The best timing within a symbol of where it first passed.
            let j = (self.i..self.i + self.sps.ceil() as usize)
                .filter(|&j| j + need <= self.buf.len())
                .map(|j| (j, self.correlation(j).abs()))
                .max_by(|a, b| a.1.total_cmp(&b.1))
                .map_or(self.i, |(j, _)| j);
            self.stats.syncs += 1;
            self.i = match self.frame(j, out) {
                Some(end) => end,
                None => j + 1,
            };
        }
        // Keep what a sync starting at i could still need.
        let drop = self.i.min(self.buf.len());
        self.buf.drain(..drop);
        self.start += drop as u64;
        self.i -= drop;
    }

    /// Read the frame whose sync starts at `j`; the sample after it, or None
    /// if there's no NID there.
    fn frame(&mut self, j: usize, out: &mut Vec<Event>) -> Option<usize> {
        // Levels from the sync: its +3 and -3 symbols.
        let (mut plus, mut minus, mut np, mut nm) = (0f32, 0f32, 0f32, 0f32);
        for k in 0..SYNC_SYMBOLS {
            let y = self.buf[j + self.sync_at[k]];
            if sync_sign(k) > 0.0 {
                plus += y;
                np += 1.0;
            } else {
                minus += y;
                nm += 1.0;
            }
        }
        let (plus, minus) = (plus / np, minus / nm);
        // Outer level and centre; an inverted channel has a negative outer level.
        let outer = (plus - minus) / 2.0;
        let centre = (plus + minus) / 2.0;
        if outer.abs() < 1e-3 {
            return None;
        }
        let mut rd = Reader { buf: &self.buf, j, sps: self.sps, centre, outer, symbol: SYNC_SYMBOLS };
        let nid_bits = (0..NID_DIBITS).fold(0u64, |a, _| a << 2 | rd.next() as u64);
        let nid = nid::decode(nid_bits, self.nac)?;
        // The all-ones and all-zeros codewords (NAC FFF / DUID F, NAC 000 /
        // DUID 0) are what a flat stretch of -3 or +1 symbols after a false
        // sync decodes to. No transmitter sends them: NAC FFF is a receiver
        // setting ("any NAC"), and 000 isn't used.
        if (nid.nac, nid.duid) == (0xfff, 0xf) || (nid.nac, nid.duid) == (0x000, 0x0) {
            return None;
        }
        self.nac = Some(nid.nac);
        self.stats.frames[nid.duid as usize] += 1;
        self.stats.nid_errors += nid.errors;
        let at = self.start + j as u64;
        out.push(Event::Frame { at, nid });
        if nid.duid == duid::PDU {
            return Some(pdu(&mut self.stats, j, &mut rd, at, nid.nac, out));
        }
        if nid.duid != duid::TSDU {
            // Voice and data frames: look for the next sync after this NID.
            return Some(j + self.at(rd.symbol));
        }
        let mut tsbks = Vec::new();
        let mut bad = 0;
        for b in 0..MAX_BLOCKS as u8 {
            let block: [u8; BLOCK_DIBITS] = std::array::from_fn(|_| rd.next());
            let (octets, errors) = decode_half_rate(&deinterleave(&block));
            self.stats.trellis_errors += errors;
            if tsbk_ok(&octets) {
                let msg: [u8; TSBK_LEN] = octets[..TSBK_LEN].try_into().unwrap();
                tsbks.push((b, msg));
                if msg[0] & 0x80 != 0 {
                    break;
                }
            } else {
                bad += 1;
            }
        }
        let end = rd.symbol;
        self.stats.tsbk_ok += tsbks.len() as u32;
        self.stats.tsbk_bad += bad;
        out.extend(tsbks.into_iter().map(|(block, octets)| Event::Tsbk { at, block, nac: nid.nac, octets }));
        // The next frame follows at once: look again from a symbol before.
        Some(j + self.at(end.saturating_sub(1)))
    }
}

/// A PDU after its NID: the header block, then (an unconfirmed or MBT one)
/// its data blocks, all ½-rate trellis. The sample after it.
fn pdu(stats: &mut Stats, j: usize, rd: &mut Reader, at: u64, nac: u16, out: &mut Vec<Event>) -> usize {
    let sample = |rd: &Reader, symbol: usize| j + (symbol as f64 * rd.sps).round() as usize;
    let block: [u8; BLOCK_DIBITS] = std::array::from_fn(|_| rd.next());
    let (head, _) = decode_half_rate(&deinterleave(&block));
    if !tsbk_ok(&head) {
        stats.pdu_bad += 1;
        return sample(rd, rd.symbol);
    }
    let header: [u8; TSBK_LEN] = head[..TSBK_LEN].try_into().unwrap();
    // Unconfirmed (0x15) and MBT (0x17) data blocks are ½-rate; confirmed
    // data's are ¾-rate, and no broadcast comes that way.
    let blocks = (header[6] & 0x7f) as usize;
    if !matches!(header[0] & 0x1f, 0x15 | 0x17) || blocks == 0 || blocks > MAX_PDU_BLOCKS {
        return sample(rd, rd.symbol);
    }
    let mut data = Vec::with_capacity(12 * blocks);
    for _ in 0..blocks {
        let block: [u8; BLOCK_DIBITS] = std::array::from_fn(|_| rd.next());
        data.extend_from_slice(&decode_half_rate(&deinterleave(&block)).0);
    }
    let end = sample(rd, rd.symbol.saturating_sub(1));
    let n = data.len() - 4;
    let want = u32::from_be_bytes(data[n..].try_into().unwrap());
    if crc32(&data[..n]) != want {
        stats.pdu_bad += 1;
        return end;
    }
    data.truncate(n);
    stats.pdu_ok += 1;
    out.push(Event::Pdu { at, nac, header, data });
    end
}

/// Reads a frame's data dibits, symbol by symbol, skipping status symbols.
struct Reader<'a> {
    buf: &'a [f32],
    j: usize,
    sps: f64,
    centre: f32,
    outer: f32,
    symbol: usize,
}

impl Reader<'_> {
    fn next(&mut self) -> u8 {
        while is_status(self.symbol) {
            self.symbol += 1;
        }
        let v = (self.buf[self.j + (self.symbol as f64 * self.sps).round() as usize] - self.centre) / self.outer;
        self.symbol += 1;
        if v > 2.0 / 3.0 {
            1
        } else if v > 0.0 {
            0
        } else if v > -2.0 / 3.0 {
            2
        } else {
            3
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::crc::ccitt;
    use super::super::trellis::encode_half_rate;
    use super::*;

    /// A frame's symbols (+3, +1, -1, -3 as ±1, ±1/3), status symbols included.
    fn tsdu(nac: u16, messages: &[[u8; TSBK_LEN]]) -> Vec<f32> {
        let mut dibits: Vec<u8> = (0..SYNC_SYMBOLS).map(|k| (FRAME_SYNC >> (46 - 2 * k) & 3) as u8).collect();
        let nid = nid::encode(nac << 4 | duid::TSDU as u16) << 1;
        dibits.extend((0..32).map(|k| (nid >> (62 - 2 * k) & 3) as u8));
        for (b, m) in messages.iter().enumerate() {
            let mut o = [0u8; 12];
            o[..10].copy_from_slice(m);
            if b + 1 == messages.len() {
                o[0] |= 0x80;
            }
            let crc = ccitt(&o[..10]);
            o[10..].copy_from_slice(&crc.to_be_bytes());
            dibits.extend(encode_half_rate(&o));
        }
        let level = |d: u8| [1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0][d as usize];
        let mut symbols = Vec::new();
        let mut it = dibits.into_iter();
        let mut n = 0;
        loop {
            if is_status(n) {
                symbols.push(1.0 / 3.0);
            } else {
                match it.next() {
                    Some(d) => symbols.push(level(d)),
                    None => break,
                }
            }
            n += 1;
        }
        symbols
    }

    /// Symbols at 10 samples each, scaled to Hz, with an offset (frequency error).
    fn samples(symbols: &[f32], scale: f32, offset: f32) -> Vec<f32> {
        symbols.iter().flat_map(|&s| std::iter::repeat_n(s * scale + offset, 10)).collect()
    }

    const NET: [u8; 10] = [0x3b, 0x00, 0x00, 0xde, 0xe0, 0x09, 0x11, 0x00, 0x88, 0x70];
    const RFSS: [u8; 10] = [0x3a, 0x00, 0x00, 0x09, 0x11, 0x01, 0x40, 0x00, 0x88, 0x70];

    #[test]
    fn decodes_a_tsdu() {
        let mut x = vec![0.0; 137];
        for _ in 0..3 {
            x.extend(samples(&tsdu(0x910, &[NET, RFSS]), 1800.0, 230.0));
        }
        x.extend(vec![0.0; 4000]);
        let mut f = Framer::new(10.0);
        let mut ev = Vec::new();
        f.push(&x, &mut ev);
        let tsbks: Vec<_> = ev.iter().filter_map(|e| if let Event::Tsbk { nac, octets, .. } = e { Some((*nac, *octets)) } else { None }).collect();
        let mut net = NET;
        let mut rfss = RFSS;
        rfss[0] |= 0x80;
        net[0] &= 0x7f;
        assert_eq!(tsbks, vec![(0x910, net), (0x910, rfss), (0x910, net), (0x910, rfss), (0x910, net), (0x910, rfss)]);
        assert_eq!((f.stats.tsbk_ok, f.stats.tsbk_bad, f.stats.frames[duid::TSDU as usize]), (6, 0, 3));
    }

    #[test]
    fn decodes_an_inverted_channel() {
        let mut x = samples(&tsdu(0x293, &[NET]), -1800.0, 0.0);
        x.extend(vec![0.0; 4000]);
        let mut f = Framer::new(10.0);
        let mut ev = Vec::new();
        f.push(&x, &mut ev);
        assert_eq!(f.stats.tsbk_ok, 1);
    }

    #[test]
    fn a_flat_signal_after_a_sync_is_not_a_frame() {
        // The sync, then -3 for ever: all -3 would decode as NAC FFF.
        let mut symbols: Vec<f32> = (0..SYNC_SYMBOLS).map(sync_sign).collect();
        symbols.extend(std::iter::repeat_n(-1.0, 400));
        let mut f = Framer::new(10.0);
        let mut ev = Vec::new();
        f.push(&samples(&symbols, 1800.0, 0.0), &mut ev);
        assert_eq!(f.stats.frames_total(), 0);
    }

    /// An MBT: sync, NID (PDU), header block, one data block (8 octets and
    /// the CRC-32).
    fn mbt(nac: u16, header: [u8; 10], data: [u8; 8]) -> Vec<f32> {
        use super::super::crc::crc32;
        let mut dibits: Vec<u8> = (0..SYNC_SYMBOLS).map(|k| (FRAME_SYNC >> (46 - 2 * k) & 3) as u8).collect();
        let nid = nid::encode(nac << 4 | duid::PDU as u16) << 1;
        dibits.extend((0..32).map(|k| (nid >> (62 - 2 * k) & 3) as u8));
        let mut h = [0u8; 12];
        h[..10].copy_from_slice(&header);
        h[10..].copy_from_slice(&ccitt(&header).to_be_bytes());
        dibits.extend(encode_half_rate(&h));
        let mut d = [0u8; 12];
        d[..8].copy_from_slice(&data);
        d[8..].copy_from_slice(&crc32(&data).to_be_bytes());
        dibits.extend(encode_half_rate(&d));
        let level = |d: u8| [1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0][d as usize];
        let mut symbols = Vec::new();
        let mut it = dibits.into_iter();
        let mut n = 0;
        loop {
            if is_status(n) {
                symbols.push(1.0 / 3.0);
            } else {
                match it.next() {
                    Some(d) => symbols.push(level(d)),
                    None => break,
                }
            }
            n += 1;
        }
        symbols
    }

    #[test]
    fn decodes_an_mbt() {
        let header = [0x37, 0xfd, 0x00, 0x00, 0x39, 0x11, 0x81, 0x3c, 0x01, 0x28];
        let data = [0xa0, 0x00, 0xa6, 0xe9, 0x70, 0xde, 0xe0, 0x00];
        let mut x = samples(&mbt(0x910, header, data), 1800.0, 0.0);
        x.extend(vec![0.0; 6000]);
        let mut f = Framer::new(10.0);
        let mut ev = Vec::new();
        f.push(&x, &mut ev);
        let pdus: Vec<_> = ev.iter().filter_map(|e| if let Event::Pdu { header, data, .. } = e { Some((*header, data.clone())) } else { None }).collect();
        assert_eq!(pdus, vec![(header, data.to_vec())]);
        assert_eq!((f.stats.pdu_ok, f.stats.pdu_bad), (1, 0));
    }

    #[test]
    fn same_in_small_pieces() {
        let mut x = samples(&tsdu(0x910, &[NET, RFSS, NET]), 1800.0, 0.0);
        x.extend(vec![0.0; 4000]);
        let mut f = Framer::new(10.0);
        let mut ev = Vec::new();
        for c in x.chunks(97) {
            f.push(c, &mut ev);
        }
        assert_eq!(f.stats.tsbk_ok, 3);
    }
}
