//! FDMA trunking signalling blocks (TSBKs): the site broadcasts.
//!
//! A TSBK is 12 octets after the trellis decoder: 10 of message and a CRC.
//! These take the 10 message octets. Octet 0 holds the last-block flag (bit
//! 7), the protected flag (bit 6) and the opcode (bits 5..0); octet 1 the
//! manufacturer ID; octets 2..9 the arguments. Field positions below are bit
//! numbers in the 80-bit message, 79 = the first octet's MSB (TIA-102.AABC).

use super::Broadcast;
use super::bandplan::{IdenUp, slots_per_carrier};

/// A TSBK's message octets (without the CRC).
pub const TSBK_LEN: usize = 10;

const IDEN_UP_TDMA: u8 = 0x33;
const IDEN_UP_VU: u8 = 0x34;
const SCCB_EXP: u8 = 0x29;
const SCCB: u8 = 0x39;
const RFSS_STS_BCST: u8 = 0x3a;
const NET_STS_BCST: u8 = 0x3b;
const ADJ_STS_BCST: u8 = 0x3c;
const IDEN_UP: u8 = 0x3d;

/// A channel number field that names no channel.
const NO_CHANNEL: u16 = 0xffff;

/// The site broadcast in a TSBK's message octets, or None for any other
/// message. Only the standard ones (manufacturer ID 0): a manufacturer's
/// opcodes mean other things. A protected (encrypted) TSBK is skipped.
pub fn parse_tsbk(t: &[u8; TSBK_LEN]) -> Option<Broadcast> {
    let protected = t[0] & 0x40 != 0;
    let opcode = t[0] & 0x3f;
    let mfid = t[1];
    if protected || mfid != 0 {
        return None;
    }
    let v = t.iter().fold(0u128, |a, &b| (a << 8) | b as u128);
    let f = |lsb: u32, width: u32| ((v >> lsb) & ((1u128 << width) - 1)) as u64;
    let channel = |lsb: u32| Some(f(lsb, 16) as u16).filter(|&c| c != NO_CHANNEL);
    Some(match opcode {
        IDEN_UP_VU => {
            // iden 63..60, bandwidth 59..56, transmit offset 55..42 (sign, 13
            // bits of spacings), spacing 41..32 (125 Hz), base 31..0 (5 Hz).
            let step_hz = f(32, 10) as u32 * 125;
            Broadcast::IdenUp(IdenUp {
                iden: f(60, 4) as u8,
                base_hz: f(0, 32) * 5,
                step_hz,
                offset_hz: signed(f(42, 14), 13) * step_hz as i64,
                tdma: false,
                slots: 1,
            })
        }
        IDEN_UP_TDMA => {
            // iden 63..60, channel type 59..56, then as IDEN_UP_VU.
            let step_hz = f(32, 10) as u32 * 125;
            Broadcast::IdenUp(IdenUp {
                iden: f(60, 4) as u8,
                base_hz: f(0, 32) * 5,
                step_hz,
                offset_hz: signed(f(42, 14), 13) * step_hz as i64,
                tdma: true,
                slots: slots_per_carrier(f(56, 4) as u8),
            })
        }
        IDEN_UP => {
            // iden 63..60, bandwidth 59..51, transmit offset 50..42 (sign, 8
            // bits of 250 kHz), spacing 41..32, base 31..0.
            Broadcast::IdenUp(IdenUp {
                iden: f(60, 4) as u8,
                base_hz: f(0, 32) * 5,
                step_hz: f(32, 10) as u32 * 125,
                offset_hz: signed(f(42, 9), 8) * 250_000,
                tdma: false,
                slots: 1,
            })
        }
        NET_STS_BCST => {
            // LRA 63..56, WACN 55..36, system 35..24, channel 23..8, class 7..0.
            Broadcast::NetStatus { wacn: f(36, 20) as u32, sysid: f(24, 12) as u16, channel: f(8, 16) as u16 }
        }
        RFSS_STS_BCST => {
            // LRA 63..56, flags 55..52, system 51..40, RFSS 39..32, site 31..24,
            // channel 23..8, class 7..0.
            Broadcast::RfssStatus { sysid: f(40, 12) as u16, rfss: f(32, 8) as u8, site: f(24, 8) as u8, channel: f(8, 16) as u16 }
        }
        ADJ_STS_BCST => {
            // As RFSS_STS_BCST, of the neighbour.
            Broadcast::Adjacent { wacn: None, sysid: f(40, 12) as u16, rfss: f(32, 8) as u8, site: f(24, 8) as u8, channel: f(8, 16) as u16 }
        }
        SCCB => {
            // RFSS 63..56, site 55..48, then two (channel, class) pairs:
            // 47..32 + 31..24 and 23..8 + 7..0. Class 0 marks a pair unused.
            let entry = |ch_lsb: u32, class_lsb: u32| channel(ch_lsb).filter(|_| f(class_lsb, 8) != 0);
            Broadcast::Secondary { rfss: f(56, 8) as u8, site: f(48, 8) as u8, channels: [entry(32, 24), entry(8, 0)] }
        }
        SCCB_EXP => {
            // RFSS 63..56, site 55..48, transmit channel 47..32 (then its
            // receive channel, which isn't a control channel's frequency).
            Broadcast::Secondary { rfss: f(56, 8) as u8, site: f(48, 8) as u8, channels: [channel(32), None] }
        }
        _ => return None,
    })
}

/// A sign-and-magnitude field: the bit above `magnitude_bits` set means
/// positive (an offset added to the downlink), clear means negative.
pub(super) fn signed(field: u64, magnitude_bits: u32) -> i64 {
    let m = (field & ((1 << magnitude_bits) - 1)) as i64;
    if field >> magnitude_bits & 1 == 1 { m } else { -m }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A TSBK with `fields` = (lsb, width, value) set in its 80 bits.
    fn tsbk(opcode: u8, fields: &[(u32, u32, u64)]) -> [u8; TSBK_LEN] {
        let mut v: u128 = (opcode as u128 & 0x3f) << 72;
        for &(lsb, width, value) in fields {
            v |= (value as u128 & ((1u128 << width) - 1)) << lsb;
        }
        v.to_be_bytes()[6..].try_into().unwrap()
    }

    /// Sign and magnitude, as the air carries a transmit offset.
    fn sm(value: i64, magnitude_bits: u32) -> u64 {
        (value.unsigned_abs()) | if value >= 0 { 1 << magnitude_bits } else { 0 }
    }

    #[test]
    fn net_status() {
        let t = tsbk(NET_STS_BCST, &[(36, 20, 0xBEE00), (24, 12, 0x1A4), (8, 16, 0x1234)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::NetStatus { wacn: 0xBEE00, sysid: 0x1A4, channel: 0x1234 }));
        let t = tsbk(NET_STS_BCST, &[(36, 20, 0xFFFFF), (24, 12, 0xFFF), (8, 16, 0xFFFF)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::NetStatus { wacn: 0xFFFFF, sysid: 0xFFF, channel: 0xFFFF }));
    }

    #[test]
    fn rfss_status() {
        let t = tsbk(RFSS_STS_BCST, &[(40, 12, 0x1A4), (32, 8, 1), (24, 8, 7), (8, 16, 0x1023)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::RfssStatus { sysid: 0x1A4, rfss: 1, site: 7, channel: 0x1023 }));
    }

    #[test]
    fn adjacent_carries_its_system() {
        let t = tsbk(ADJ_STS_BCST, &[(40, 12, 0x1A4), (32, 8, 2), (24, 8, 15), (8, 16, 0x2055)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::Adjacent { wacn: None, sysid: 0x1A4, rfss: 2, site: 15, channel: 0x2055 }));
    }

    #[test]
    fn secondary() {
        let t = tsbk(SCCB, &[(56, 8, 1), (48, 8, 7), (32, 16, 0x1010), (24, 8, 0xF0), (8, 16, 0x1011), (0, 8, 0xF0)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::Secondary { rfss: 1, site: 7, channels: [Some(0x1010), Some(0x1011)] }));
        // An unused pair: class 0, or no channel.
        let t = tsbk(SCCB, &[(56, 8, 1), (48, 8, 7), (32, 16, 0x1010), (24, 8, 0xF0), (8, 16, 0x1011), (0, 8, 0)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::Secondary { rfss: 1, site: 7, channels: [Some(0x1010), None] }));
        let t = tsbk(SCCB, &[(56, 8, 1), (48, 8, 7), (32, 16, 0xFFFF), (24, 8, 0xF0)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::Secondary { rfss: 1, site: 7, channels: [None, None] }));
        let t = tsbk(SCCB_EXP, &[(56, 8, 1), (48, 8, 7), (32, 16, 0x1010), (16, 16, 0x1777)]);
        assert_eq!(parse_tsbk(&t), Some(Broadcast::Secondary { rfss: 1, site: 7, channels: [Some(0x1010), None] }));
    }

    #[test]
    fn iden_up_vu() {
        // 851.00625 MHz, 12.5 kHz steps, -45 MHz (3600 steps).
        let t = tsbk(IDEN_UP_VU, &[(60, 4, 3), (42, 14, sm(-3600, 13)), (32, 10, 100), (0, 32, 170_201_250)]);
        let Some(Broadcast::IdenUp(i)) = parse_tsbk(&t) else { panic!() };
        assert_eq!(i, IdenUp { iden: 3, base_hz: 851_006_250, step_hz: 12_500, offset_hz: -45_000_000, tdma: false, slots: 1 });
        let t = tsbk(IDEN_UP_VU, &[(42, 14, sm(3600, 13)), (32, 10, 100), (0, 32, 170_201_250)]);
        let Some(Broadcast::IdenUp(i)) = parse_tsbk(&t) else { panic!() };
        assert_eq!(i.offset_hz, 45_000_000);
    }

    #[test]
    fn iden_up_tdma() {
        let t = tsbk(IDEN_UP_TDMA, &[(60, 4, 2), (56, 4, 3), (42, 14, sm(-3600, 13)), (32, 10, 100), (0, 32, 170_201_250)]);
        let Some(Broadcast::IdenUp(i)) = parse_tsbk(&t) else { panic!() };
        assert_eq!(i, IdenUp { iden: 2, base_hz: 851_006_250, step_hz: 12_500, offset_hz: -45_000_000, tdma: true, slots: 2 });
        let t = tsbk(IDEN_UP_TDMA, &[(56, 4, 4), (32, 10, 100)]);
        let Some(Broadcast::IdenUp(i)) = parse_tsbk(&t) else { panic!() };
        assert_eq!(i.slots, 4);
    }

    #[test]
    fn iden_up_800() {
        // Offset in 250 kHz units: -180 = -45 MHz. 6.25 kHz steps.
        let t = tsbk(IDEN_UP, &[(42, 9, sm(-180, 8)), (32, 10, 50), (0, 32, 170_201_250)]);
        let Some(Broadcast::IdenUp(i)) = parse_tsbk(&t) else { panic!() };
        assert_eq!(i, IdenUp { iden: 0, base_hz: 851_006_250, step_hz: 6_250, offset_hz: -45_000_000, tdma: false, slots: 1 });
    }

    #[test]
    fn others_are_not_broadcasts() {
        for op in [0x00, 0x01, 0x28, 0x3f] {
            assert_eq!(parse_tsbk(&tsbk(op, &[])), None, "opcode {op:#x}");
        }
        // A manufacturer's opcode 0x3b is not NET_STS_BCST.
        let mut t = tsbk(NET_STS_BCST, &[(36, 20, 0xBEE00)]);
        t[1] = 0x90;
        assert_eq!(parse_tsbk(&t), None);
        // Nor is a protected one.
        let mut t = tsbk(NET_STS_BCST, &[(36, 20, 0xBEE00)]);
        t[0] |= 0x40;
        assert_eq!(parse_tsbk(&t), None);
    }
}
