//! Multi-block trunking (MBT): the FDMA site broadcasts some systems send as
//! a data PDU (a header block and data blocks) rather than as TSBKs. Only
//! the extended (alternate) format, 0x17, carries them.
//!
//! The header's 10 octets: 0 format (low 5 bits), 1 SAP, 2 manufacturer ID,
//! 3..5 an address (here: 4 bits reserved, then the System ID, 12), 6 blocks
//! to follow, 7 opcode (low 6 bits), 8..9 opcode-specific. `data` is the data
//! blocks' octets without their CRC-32.

use super::{Broadcast, TSBK_LEN};

/// The extended format, which the broadcasts use.
pub const FORMAT_EXTENDED: u8 = 0x17;

const RFSS_STS_BCST: u8 = 0x3a;
const NET_STS_BCST: u8 = 0x3b;
const ADJ_STS_BCST: u8 = 0x3c;
const P_PARM_BCST: u8 = 0x3e;

/// The site broadcast in an MBT, or None for any other.
pub fn parse_mbt(header: &[u8; TSBK_LEN], data: &[u8]) -> Option<Broadcast> {
    if header[0] & 0x1f != FORMAT_EXTENDED || header[2] != 0 {
        return None;
    }
    let sysid = u16::from_be_bytes([header[4], header[5]]) & 0xfff;
    let be16 = |i: usize| data.get(i..i + 2).map(|b| u16::from_be_bytes([b[0], b[1]]));
    Some(match header[7] & 0x3f {
        // Header 8: RFSS, 9: site; data 0..1: the neighbour's channel.
        ADJ_STS_BCST => Broadcast::Adjacent { wacn: None, sysid, rfss: header[8], site: header[9], channel: be16(0)?, lra: None, flags: None },
        // Data 0..2: WACN (20 bits), then 3..4: the channel.
        NET_STS_BCST => {
            let d = data.get(..5)?;
            Broadcast::NetStatus { wacn: (d[0] as u32) << 12 | (d[1] as u32) << 4 | (d[2] as u32) >> 4, sysid, channel: be16(3)?, lra: None }
        }
        // Data 0: RFSS, 1: site, 2..3: the channel.
        RFSS_STS_BCST => {
            let d = data.get(..4)?;
            Broadcast::RfssStatus { sysid, rfss: d[0], site: d[1], channel: be16(2)?, lra: None, active: None }
        }
        // Header 9: the algorithm (the key and message indicator are in the data).
        P_PARM_BCST => Broadcast::Protected { algid: header[9] },
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unhex(s: &str) -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
    }

    /// CTTRS Dallas (855.0625) as op25 passed it on: NAC, header, its CRC,
    /// the data less its CRC-32.
    #[test]
    fn a_neighbour_from_the_air() {
        let b = unhex("091037fd00003911813c012840b2a000a6e970dee000");
        let header: [u8; 10] = b[2..12].try_into().unwrap();
        assert_eq!(
            parse_mbt(&header, &b[14..]),
            Some(Broadcast::Adjacent { wacn: None, sysid: 0x911, rfss: 1, site: 40, channel: 0xa000, lra: None, flags: None })
        );
    }

    #[test]
    fn network_and_rfss_status() {
        let mut h = [FORMAT_EXTENDED, 0x3d, 0, 0, 0x01, 0xa4, 0x81, NET_STS_BCST, 0, 0];
        assert_eq!(
            parse_mbt(&h, &[0xbe, 0xe0, 0x00, 0x10, 0x23, 0, 0, 0]),
            Some(Broadcast::NetStatus { wacn: 0xbee00, sysid: 0x1a4, channel: 0x1023, lra: None })
        );
        h[7] = RFSS_STS_BCST;
        assert_eq!(
            parse_mbt(&h, &[1, 7, 0x10, 0x23, 0, 0, 0, 0]),
            Some(Broadcast::RfssStatus { sysid: 0x1a4, rfss: 1, site: 7, channel: 0x1023, lra: None, active: None })
        );
        h[7] = P_PARM_BCST;
        h[9] = 0x84;
        assert_eq!(parse_mbt(&h, &[]), Some(Broadcast::Protected { algid: 0x84 }));
        // Another format, or a manufacturer's: not these.
        h[0] = 0x15;
        assert_eq!(parse_mbt(&h, &[]), None);
    }
}
