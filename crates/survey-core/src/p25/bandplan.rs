//! The band plan: what the identifier updates (IDEN_UP*) say channel numbers
//! mean. A channel number is 4 bits of table (the iden) and 12 of channel.

/// One identifier update: a table of evenly spaced channels.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IdenUp {
    pub iden: u8,
    /// Channel 0's downlink frequency.
    pub base_hz: u64,
    pub step_hz: u32,
    /// Uplink minus downlink.
    pub offset_hz: i64,
    /// TDMA channels: two or more slots a carrier. (IDEN_UP_TDMA can describe
    /// 1-slot FDMA channels too: those aren't.)
    pub tdma: bool,
    /// TDMA slots per carrier: channel numbers count slots, so the carrier is
    /// channel / slots.
    pub slots: u8,
    /// The channels' bandwidth, where the update says (IDEN_UP_VU).
    pub bandwidth_hz: Option<u32>,
}

/// Slots per carrier of an IDEN_UP_TDMA channel type (0..=5 defined; the
/// rest reserved, taken as 2).
pub(super) fn slots_per_carrier(channel_type: u8) -> u8 {
    match channel_type {
        0..=2 => 1,
        4 => 4,
        _ => 2,
    }
}

/// The 16 tables, as heard so far.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct BandPlan {
    tables: [Option<IdenUp>; 16],
}

impl BandPlan {
    /// Add (or replace) a table. True if it is new or changed.
    pub fn insert(&mut self, i: IdenUp) -> bool {
        let slot = &mut self.tables[i.iden as usize & 0xf];
        let changed = *slot != Some(i);
        *slot = Some(i);
        changed
    }

    /// A channel number's downlink frequency, if its table has been heard.
    pub fn frequency(&self, channel: u16) -> Option<u64> {
        let t = self.tables[(channel >> 12) as usize]?;
        let n = (channel & 0xfff) as u64;
        let carrier = if t.tdma && t.slots > 0 { n / t.slots as u64 } else { n };
        Some(t.base_hz + t.step_hz as u64 * carrier)
    }

    /// Whether channel numbers in `channel`'s table can be resolved.
    pub fn knows(&self, channel: u16) -> bool {
        self.tables[(channel >> 12) as usize].is_some()
    }

    /// The tables heard, by iden.
    pub fn entries(&self) -> impl Iterator<Item = &IdenUp> {
        self.tables.iter().flatten()
    }

    pub fn len(&self) -> usize {
        self.tables.iter().flatten().count()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fdma(iden: u8, base_hz: u64, step_hz: u32) -> IdenUp {
        IdenUp { iden, base_hz, step_hz, offset_hz: 0, tdma: false, slots: 1, bandwidth_hz: None }
    }

    #[test]
    fn empty_resolves_nothing() {
        assert_eq!(BandPlan::default().frequency(0x0000), None);
    }

    #[test]
    fn fdma_channels() {
        let mut p = BandPlan::default();
        p.insert(fdma(0, 851_006_250, 12_500));
        assert_eq!(p.frequency(0x0000), Some(851_006_250));
        assert_eq!(p.frequency(0x0001), Some(851_018_750));
        assert_eq!(p.frequency(0x0010), Some(851_006_250 + 16 * 12_500));
        assert_eq!(p.frequency(0x5000), None);
    }

    #[test]
    fn tdma_channels_count_slots() {
        let mut p = BandPlan::default();
        p.insert(IdenUp { iden: 2, base_hz: 851_000_000, step_hz: 12_500, offset_hz: 0, tdma: true, slots: 2, bandwidth_hz: None });
        let t = 2 << 12;
        assert_eq!(p.frequency(t), Some(851_000_000));
        assert_eq!(p.frequency(t | 1), Some(851_000_000));
        assert_eq!(p.frequency(t | 2), Some(851_012_500));
        assert_eq!(p.frequency(t | 3), Some(851_012_500));
    }

    #[test]
    fn a_table_heard_again_replaces_it() {
        let mut p = BandPlan::default();
        assert!(p.insert(fdma(0, 100_000_000, 12_500)));
        assert!(!p.insert(fdma(0, 100_000_000, 12_500)));
        assert!(p.insert(fdma(0, 200_000_000, 12_500)));
        assert_eq!(p.frequency(0), Some(200_000_000));
        assert_eq!(p.len(), 1);
    }
}
