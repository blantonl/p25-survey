//! A TSBK block on the air: 196 bits (98 dibits) = 49 trellis symbols
//! (constellation points, each sent as a pair of dibits), interleaved,
//! carrying 48 dibits (12 octets: the message and its CRC) through the
//! ½-rate trellis code (TIA-102.BAAA).

/// ½-rate trellis: the 4-bit symbol sent for (state = previous input dibit, input dibit).
const HALF_RATE: [[u8; 4]; 4] = [[0, 15, 12, 3], [4, 11, 8, 7], [13, 2, 1, 14], [9, 6, 5, 10]];

/// Dibits in an interleaved block.
pub const BLOCK_DIBITS: usize = 98;

/// The two dibits each trellis symbol (constellation point 0..15) is sent
/// as. In C4FM terms: 0 = (+1, -1), 1 = (-1, -1), 2 = (+3, -3), 3 = (-3, -3),
/// 4 = (-3, -1), 5 = (+3, -1), 6 = (-1, -3), 7 = (+1, -3), 8 = (-3, +3),
/// 9 = (+3, +3), 10 = (-1, +1), 11 = (+1, +1), 12 = (+1, +3), 13 = (-1, +3),
/// 14 = (+3, +1), 15 = (-3, +1); dibits +3 = 01, +1 = 00, -1 = 10, -3 = 11.
const POINT_DIBITS: [(u8, u8); 16] =
    [(0, 2), (2, 2), (1, 3), (3, 3), (3, 2), (1, 2), (2, 3), (0, 3), (3, 1), (1, 1), (2, 0), (0, 0), (0, 1), (2, 1), (1, 0), (3, 0)];

/// The trellis output for (state, input) as the 4 bits of its two dibits:
/// what the Viterbi compares the received dibits with. Neighbouring C4FM
/// levels differ in one dibit bit, so these bit errors are what noise causes.
const EXPECTED: [[u8; 4]; 4] = {
    let mut t = [[0u8; 4]; 4];
    let mut s = 0;
    while s < 4 {
        let mut d = 0;
        while d < 4 {
            let (a, b) = POINT_DIBITS[HALF_RATE[s][d] as usize];
            t[s][d] = a << 2 | b;
            d += 1;
        }
        s += 1;
    }
    t
};

/// Trellis symbol carried by the `p`th dibit pair on the air. Pairs go out in
/// the order 0, 4, 8, … 48, then 1, 5, … 45, then 2, … 46, then 3, … 47.
fn symbol_index(p: usize) -> usize {
    match p {
        0..=12 => 4 * p,
        13..=24 => 4 * (p - 13) + 1,
        25..=36 => 4 * (p - 25) + 2,
        _ => 4 * (p - 37) + 3,
    }
}

/// Undo the interleave: each trellis symbol's two received dibits (as 4
/// bits, first dibit high), in trellis order.
pub fn deinterleave(dibits: &[u8; BLOCK_DIBITS]) -> [u8; 49] {
    let mut s = [0u8; 49];
    for p in 0..49 {
        s[symbol_index(p)] = (dibits[2 * p] & 3) << 2 | (dibits[2 * p + 1] & 3);
    }
    s
}

/// Viterbi-decode 49 received dibit pairs to 48 dibits, packed as 12 octets.
/// Also returns the dibit bit errors on the chosen path.
pub fn decode_half_rate(symbols: &[u8; 49]) -> ([u8; 12], u32) {
    const INF: u32 = u32::MAX / 2;
    let mut metric = [0, INF, INF, INF];
    // Survivor: the input dibit that led to each state, per step.
    let mut from = [[0u8; 4]; 49];
    for (t, &rx) in symbols.iter().enumerate() {
        let mut next = [INF; 4];
        for (s, &m) in metric.iter().enumerate() {
            if m >= INF {
                continue;
            }
            for d in 0..4 {
                let cost = m + (EXPECTED[s][d] ^ rx).count_ones();
                if cost < next[d] {
                    next[d] = cost;
                    from[t][d] = s as u8;
                }
            }
        }
        metric = next;
    }
    // The block ends with a flush dibit of 0: end in state 0.
    let mut state = 0usize;
    let mut dibits = [0u8; 49];
    for t in (0..49).rev() {
        dibits[t] = state as u8;
        state = from[t][state] as usize;
    }
    let mut out = [0u8; 12];
    for (i, &d) in dibits[..48].iter().enumerate() {
        out[i / 4] |= d << (6 - 2 * (i % 4));
    }
    (out, metric[0])
}

/// Encode 12 octets as an interleaved block (for tests and synthesis).
pub fn encode_half_rate(octets: &[u8; 12]) -> [u8; BLOCK_DIBITS] {
    let mut symbols = [0u8; 49];
    let mut state = 0usize;
    for (t, sym) in symbols.iter_mut().enumerate() {
        let d = if t < 48 { (octets[t / 4] >> (6 - 2 * (t % 4)) & 3) as usize } else { 0 };
        *sym = HALF_RATE[state][d];
        state = d;
    }
    let mut out = [0u8; BLOCK_DIBITS];
    for p in 0..49 {
        (out[2 * p], out[2 * p + 1]) = POINT_DIBITS[symbols[symbol_index(p)] as usize];
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const MSG: [u8; 12] = [0x3b, 0x00, 0x00, 0xde, 0xe0, 0x09, 0x11, 0x00, 0x88, 0x70, 0x12, 0x34];

    #[test]
    fn interleave_covers_every_symbol() {
        let mut seen = [false; 49];
        for p in 0..49 {
            seen[symbol_index(p)] = true;
        }
        assert!(seen.iter().all(|&s| s));
    }

    #[test]
    fn constellation_map_is_one_to_one() {
        let mut seen: Vec<_> = POINT_DIBITS.to_vec();
        seen.sort();
        seen.dedup();
        assert_eq!(seen.len(), 16);
    }

    #[test]
    fn round_trip() {
        let block = encode_half_rate(&MSG);
        let (out, errors) = decode_half_rate(&deinterleave(&block));
        assert_eq!((out, errors), (MSG, 0));
    }

    #[test]
    fn corrects_scattered_errors() {
        let mut block = encode_half_rate(&MSG);
        for i in [3, 30, 61, 90] {
            block[i] ^= 1;
        }
        let (out, errors) = decode_half_rate(&deinterleave(&block));
        assert_eq!(out, MSG);
        assert_eq!(errors, 4);
    }
}
