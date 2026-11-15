//! CRC-CCITT as a TSBK carries it: x^16 + x^12 + x^5 + 1 over the 80
//! message bits, register starting at 0, the result inverted.

pub fn ccitt(data: &[u8]) -> u16 {
    let mut crc: u16 = 0;
    for &b in data {
        crc ^= (b as u16) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 { crc << 1 ^ 0x1021 } else { crc << 1 };
        }
    }
    !crc
}

/// A TSBK's 12 octets: is the CRC in the last two right?
pub fn tsbk_ok(block: &[u8; 12]) -> bool {
    ccitt(&block[..10]) == u16::from_be_bytes([block[10], block[11]])
}

/// CRC-32 as a PDU's data carries it: x^32 + x^26 + x^23 + x^22 + x^16 +
/// x^12 + x^11 + x^10 + x^8 + x^7 + x^5 + x^4 + x^2 + x + 1, MSB first.
/// Register from 0, the result inverted (as the CRC-CCITT above), which is
/// what every PDU on the corpus passes.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc: u32 = 0;
    for &b in data {
        crc ^= (b as u32) << 24;
        for _ in 0..8 {
            crc = if crc & 0x8000_0000 != 0 { crc << 1 ^ 0x04c1_1db7 } else { crc << 1 };
        }
    }
    !crc
}
