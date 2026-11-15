//! Phase 2 (TDMA) MAC messages that broadcast the site: the explicit forms
//! of the FDMA broadcasts. `msg[0]` is the MAC opcode; positions are octets.

use super::Broadcast;
use super::bandplan::{IdenUp, slots_per_carrier};
use super::tsbk::signed;

const IDEN_UP_TDMA_EXT: u8 = 0xf3;
const RFSS_STS_BCST_EXP: u8 = 0xfa;
const NET_STS_BCST_EXP: u8 = 0xfb;
const ADJ_STS_BCST_EXP: u8 = 0xfc;
const ADJ_STS_BCST_EXT_EXP: u8 = 0xfe;

fn be(b: &[u8]) -> u64 {
    b.iter().fold(0, |a, &x| (a << 8) | x as u64)
}

/// The site broadcast in a MAC message, or None for any other (or a short one).
pub fn parse_mac(msg: &[u8]) -> Option<Broadcast> {
    let (&op, _) = msg.split_first()?;
    let need = match op {
        IDEN_UP_TDMA_EXT => 14,
        RFSS_STS_BCST_EXP | NET_STS_BCST_EXP | ADJ_STS_BCST_EXP => 10,
        ADJ_STS_BCST_EXT_EXP => 15,
        _ => return None,
    };
    if msg.len() < need {
        return None;
    }
    Some(match op {
        IDEN_UP_TDMA_EXT => {
            // 2: iden (high nibble), channel type (low); 3..5: transmit offset
            // (14 bits, sign first) then spacing (10 bits); 6..9: base (5 Hz).
            let step_hz = (be(&msg[4..6]) & 0x3ff) as u32 * 125;
            Broadcast::IdenUp(IdenUp {
                iden: msg[2] >> 4,
                base_hz: be(&msg[6..10]) * 5,
                step_hz,
                offset_hz: signed(be(&msg[3..5]) >> 2 & 0x3fff, 13) * step_hz as i64,
                tdma: true,
                slots: slots_per_carrier(msg[2] & 0xf),
            })
        }
        RFSS_STS_BCST_EXP => Broadcast::RfssStatus { sysid: (be(&msg[2..4]) & 0xfff) as u16, rfss: msg[4], site: msg[5], channel: be(&msg[6..8]) as u16 },
        // 2..4: WACN (20 bits) and system (12).
        NET_STS_BCST_EXP => Broadcast::NetStatus { wacn: (be(&msg[2..5]) >> 4) as u32, sysid: (be(&msg[4..6]) & 0xfff) as u16, channel: be(&msg[6..8]) as u16 },
        ADJ_STS_BCST_EXP => {
            Broadcast::Adjacent { wacn: None, sysid: (be(&msg[2..4]) & 0xfff) as u16, rfss: msg[4], site: msg[5], channel: be(&msg[6..8]) as u16 }
        }
        // As the explicit form, and 12..14: WACN (20 bits).
        ADJ_STS_BCST_EXT_EXP => Broadcast::Adjacent {
            wacn: Some((be(&msg[12..15]) >> 4) as u32),
            sysid: (be(&msg[2..4]) & 0xfff) as u16,
            rfss: msg[4],
            site: msg[5],
            channel: be(&msg[6..8]) as u16,
        },
        _ => unreachable!(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(op: u8, args: &[u8], len: usize) -> Vec<u8> {
        let mut m = vec![0u8; len];
        m[0] = op;
        m[2..2 + args.len()].copy_from_slice(args);
        m
    }

    #[test]
    fn rfss_status() {
        let m = msg(RFSS_STS_BCST_EXP, &[0x01, 0xA4, 1, 7, 0x10, 0x23], 16);
        assert_eq!(parse_mac(&m), Some(Broadcast::RfssStatus { sysid: 0x1A4, rfss: 1, site: 7, channel: 0x1023 }));
    }

    #[test]
    fn net_status() {
        // WACN BEE00 then system 1A4 share octet 4.
        let m = msg(NET_STS_BCST_EXP, &[0xBE, 0xE0, 0x01, 0xA4, 0x12, 0x34], 16);
        assert_eq!(parse_mac(&m), Some(Broadcast::NetStatus { wacn: 0xBEE00, sysid: 0x1A4, channel: 0x1234 }));
    }

    #[test]
    fn adjacent() {
        let m = msg(ADJ_STS_BCST_EXP, &[0x01, 0xA4, 2, 9, 0x20, 0x55], 16);
        assert_eq!(parse_mac(&m), Some(Broadcast::Adjacent { wacn: None, sysid: 0x1A4, rfss: 2, site: 9, channel: 0x2055 }));
        let mut m = msg(ADJ_STS_BCST_EXT_EXP, &[0x01, 0xA4, 2, 9, 0x20, 0x55], 20);
        m[12..15].copy_from_slice(&[0xBE, 0xE0, 0x00]);
        assert_eq!(parse_mac(&m), Some(Broadcast::Adjacent { wacn: Some(0xBEE00), sysid: 0x1A4, rfss: 2, site: 9, channel: 0x2055 }));
    }

    #[test]
    fn iden_up_tdma_ext() {
        // iden 2, type 3 (2 slots); offset -3600 x 12.5 kHz; base 851.00625 MHz.
        let off: u64 = 3600; // sign bit clear: negative
        let spac: u64 = 100;
        let w = (off << 10 | spac) & 0xFF_FFFF; // 14 + 10 bits in octets 3..5
        let base = (851_006_250u64 / 5).to_be_bytes();
        let m = msg(IDEN_UP_TDMA_EXT, &[0x23, (w >> 16) as u8, (w >> 8) as u8, w as u8, base[4], base[5], base[6], base[7]], 16);
        assert_eq!(
            parse_mac(&m),
            Some(Broadcast::IdenUp(IdenUp { iden: 2, base_hz: 851_006_250, step_hz: 12_500, offset_hz: -45_000_000, tdma: true, slots: 2 }))
        );
    }

    #[test]
    fn others_and_short_ones() {
        assert_eq!(parse_mac(&[]), None);
        assert_eq!(parse_mac(&[0; 16]), None);
        assert_eq!(parse_mac(&[RFSS_STS_BCST_EXP, 0, 0]), None);
    }
}
