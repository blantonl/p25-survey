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
