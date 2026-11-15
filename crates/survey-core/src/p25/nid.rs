//! The network identifier (NID) after each frame sync: NAC (12 bits) and
//! DUID (4), protected by a BCH(63,16) code (minimum distance 23), then one
//! parity bit. TIA-102.BAAA.

use std::sync::OnceLock;

/// BCH(63,16) generator polynomial, octal 6331 1413 6723 5453 (degree 47).
const GENERATOR: u64 = 0o6331141367235453;

/// Data unit IDs.
pub mod duid {
    pub const HDU: u8 = 0x0;
    pub const TDU: u8 = 0x3;
    pub const LDU1: u8 = 0x5;
    pub const TSDU: u8 = 0x7;
    pub const LDU2: u8 = 0xa;
    pub const PDU: u8 = 0xc;
    pub const TDULC: u8 = 0xf;
}

/// A decoded NID.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Nid {
    pub nac: u16,
    pub duid: u8,
    /// Bits the codeword differs from what was received.
    pub errors: u32,
}

/// The 63-bit codeword for 16 information bits.
pub fn encode(info: u16) -> u64 {
    let mut r = (info as u64) << 47;
    for bit in (47..63).rev() {
        if r >> bit & 1 == 1 {
            r ^= GENERATOR << (bit - 47);
        }
    }
    ((info as u64) << 47) | (r & ((1 << 47) - 1))
}

fn codewords() -> &'static [u64] {
    static TABLE: OnceLock<Vec<u64>> = OnceLock::new();
    TABLE.get_or_init(|| (0..=u16::MAX).map(encode).collect())
}

/// Most errors a NID is accepted with (the code corrects 11; fewer keeps
/// noise that happens to follow a sync from passing).
pub const MAX_ERRORS: u32 = 9;

/// Decode the NID's 64 bits (first received in the MSB). `nac`, if known,
/// is tried first; otherwise every codeword is.
pub fn decode(bits: u64, nac: Option<u16>) -> Option<Nid> {
    let rx = bits >> 1;
    let best = |infos: &mut dyn Iterator<Item = u16>| infos.map(|i| (i, (encode(i) ^ rx).count_ones())).min_by_key(|&(_, e)| e);
    if let Some(nac) = nac {
        let (info, errors) = best(&mut (0..16u16).map(|d| nac << 4 | d))?;
        if errors <= MAX_ERRORS {
            return Some(Nid { nac: info >> 4, duid: (info & 0xf) as u8, errors });
        }
    }
    let table = codewords();
    let (info, errors) = table.iter().enumerate().map(|(i, &c)| (i as u16, (c ^ rx).count_ones())).min_by_key(|&(_, e)| e)?;
    (errors <= MAX_ERRORS).then_some(Nid { nac: info >> 4, duid: (info & 0xf) as u8, errors })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codewords_are_far_apart() {
        // Linear code: the minimum distance is the lightest non-zero codeword.
        let min = codewords().iter().skip(1).map(|c| c.count_ones()).min().unwrap();
        assert_eq!(min, 23);
    }

    #[test]
    fn corrects_errors() {
        let info = 0x910 << 4 | duid::TSDU as u16;
        let sent = encode(info) << 1;
        let hit = sent ^ (1 << 5) ^ (1 << 30) ^ (1 << 60) ^ (1 << 44);
        assert_eq!(decode(hit, None), Some(Nid { nac: 0x910, duid: duid::TSDU, errors: 4 }));
        assert_eq!(decode(hit, Some(0x910)), Some(Nid { nac: 0x910, duid: duid::TSDU, errors: 4 }));
        // Noise isn't a NID.
        assert_eq!(decode(0x5a5a_5a5a_5a5a_5a5a, None), None);
    }
}
