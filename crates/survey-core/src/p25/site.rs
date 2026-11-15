//! What one control channel has told us about its site.

use std::collections::{BTreeMap, BTreeSet};

use super::{BandPlan, Broadcast, SiteFlags};

/// A neighbouring site, as ADJ_STS_BCST announces it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Neighbor {
    pub rfss: u8,
    pub site: u8,
    pub sysid: u16,
    /// Only the TDMA extended form carries it.
    pub wacn: Option<u32>,
    /// Its control channel, as a channel number.
    pub channel: u16,
    pub lra: Option<u8>,
    /// Its status bits (the FDMA TSBK carries them; MBT and TDMA don't).
    pub flags: Option<SiteFlags>,
}

/// Everything a control channel has broadcast about its site. Channel numbers
/// are kept as heard and resolved through the band plan when asked, so a
/// neighbour heard before its IDEN_UP gets a frequency once the table arrives.
#[derive(Clone, Debug, Default)]
pub struct SiteInfo {
    /// From the frames' NIDs: the one heard most.
    pub nac: Option<u16>,
    nac_votes: BTreeMap<u16, u32>,
    pub wacn: Option<u32>,
    pub sysid: Option<u16>,
    pub rfss: Option<u8>,
    pub site: Option<u8>,
    /// The channel the site says it is broadcasting on (RFSS / NET status).
    pub channel: Option<u16>,
    /// Location registration area.
    pub lra: Option<u8>,
    /// RFSS_STS_BCST's A bit: an active network connection (false: failsoft).
    pub network_active: Option<bool>,
    /// SYS_SRV_BCST: services available, and supported (24-bit sets).
    pub services: Option<(u32, u32)>,
    /// TIME_DATE_ANN: the local time's offset from UTC, minutes.
    pub utc_offset_min: Option<i16>,
    /// P_PARM_BCST: the control channel is encrypted, with this algorithm.
    pub encryption_algid: Option<u8>,
    pub plan: BandPlan,
    /// By (RFSS, site): a site keeps one entry, the latest heard.
    neighbors: BTreeMap<(u8, u8), Neighbor>,
    /// Secondary control channels, as channel numbers.
    secondary: BTreeSet<u16>,
    /// Broadcasts taken, and how many told us something new.
    pub broadcasts: u32,
    pub new: u32,
}

impl SiteInfo {
    /// Take one broadcast. True if it told us something we didn't know.
    pub fn push(&mut self, b: Broadcast) -> bool {
        self.broadcasts += 1;
        let new = match b {
            Broadcast::IdenUp(i) => self.plan.insert(i),
            Broadcast::NetStatus { wacn, sysid, channel, lra } => {
                set(&mut self.wacn, wacn) | set(&mut self.sysid, sysid) | set(&mut self.channel, channel) | lra.is_some_and(|l| set(&mut self.lra, l))
            }
            Broadcast::RfssStatus { sysid, rfss, site, channel, lra, active } => {
                set(&mut self.sysid, sysid)
                    | set(&mut self.rfss, rfss)
                    | set(&mut self.site, site)
                    | set(&mut self.channel, channel)
                    | lra.is_some_and(|l| set(&mut self.lra, l))
                    | active.is_some_and(|a| set(&mut self.network_active, a))
            }
            Broadcast::Adjacent { wacn, sysid, rfss, site, channel, lra, flags } => {
                // The same neighbour can come as a TSBK (with its flags) and an
                // MBT (without): what one form lacks the other keeps.
                let old = self.neighbors.get(&(rfss, site)).copied();
                let n = Neighbor {
                    rfss,
                    site,
                    sysid,
                    channel,
                    wacn: wacn.or(old.and_then(|o| o.wacn)),
                    lra: lra.or(old.and_then(|o| o.lra)),
                    flags: flags.or(old.and_then(|o| o.flags)),
                };
                self.neighbors.insert((rfss, site), n) != Some(n)
            }
            Broadcast::Secondary { channels, .. } => channels.into_iter().flatten().fold(false, |acc, c| self.secondary.insert(c) | acc),
            Broadcast::SystemService { available, supported, .. } => set(&mut self.services, (available, supported)),
            Broadcast::TimeDate { utc_offset_min } => utc_offset_min.is_some_and(|m| set(&mut self.utc_offset_min, m)),
            Broadcast::Protected { algid } => set(&mut self.encryption_algid, algid),
        };
        self.new += new as u32;
        new
    }

    /// Note the NAC from a frame's NID. True if the most-heard one changed.
    /// (One NID in a noisy stretch can decode to another NAC.)
    pub fn nac_heard(&mut self, nac: u16) -> bool {
        *self.nac_votes.entry(nac).or_default() += 1;
        let top = self.nac_votes.iter().max_by_key(|&(_, &n)| n).map(|(&v, _)| v);
        let changed = top != self.nac;
        self.nac = top;
        changed
    }

    /// Each NAC heard, with how many frames had it.
    pub fn nac_votes(&self) -> impl Iterator<Item = (u16, u32)> + '_ {
        self.nac_votes.iter().map(|(&n, &v)| (n, v))
    }

    /// WACN, system, RFSS and site all heard.
    pub fn has_identity(&self) -> bool {
        self.wacn.is_some() && self.sysid.is_some() && self.rfss.is_some() && self.site.is_some()
    }

    /// The frequency the site announces for itself (what a radio's error is
    /// measured against), once its table is heard.
    pub fn announced_hz(&self) -> Option<u64> {
        self.plan.frequency(self.channel?)
    }

    /// Neighbours, with their control channel's frequency where the band plan
    /// resolves it.
    pub fn neighbors(&self) -> impl Iterator<Item = (Neighbor, Option<u64>)> + '_ {
        self.neighbors.values().map(|&n| (n, self.plan.frequency(n.channel)))
    }

    /// Secondary control channels: (channel number, frequency if resolved).
    pub fn secondary(&self) -> impl Iterator<Item = (u16, Option<u64>)> + '_ {
        self.secondary.iter().map(|&c| (c, self.plan.frequency(c)))
    }

    /// Channel numbers heard (own, neighbours', secondaries') whose table
    /// hasn't been: what more listening would still resolve.
    pub fn unresolved(&self) -> usize {
        let own = self.channel.iter().copied();
        let others = self.neighbors.values().map(|n| n.channel).chain(self.secondary.iter().copied());
        own.chain(others).filter(|&c| !self.plan.knows(c)).count()
    }
}

/// Set `slot` to `v`; true if that changed it.
fn set<T: PartialEq + Copy>(slot: &mut Option<T>, v: T) -> bool {
    let changed = *slot != Some(v);
    *slot = Some(v);
    changed
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::p25::IdenUp;

    const PLAN_800: IdenUp = IdenUp { iden: 0, base_hz: 851_006_250, step_hz: 6_250, offset_hz: -45_000_000, tdma: false, slots: 1, bandwidth_hz: None };

    #[test]
    fn a_site_from_its_broadcasts() {
        let mut s = SiteInfo::default();
        // A neighbour before the band plan: resolved once the table comes.
        assert!(s.push(Broadcast::Adjacent { wacn: None, sysid: 0x1A4, rfss: 1, site: 8, channel: 0x0010, lra: None, flags: None }));
        assert_eq!(s.neighbors().next().unwrap().1, None);
        assert_eq!(s.unresolved(), 1);
        assert!(s.push(Broadcast::IdenUp(PLAN_800)));
        assert!(s.push(Broadcast::NetStatus { wacn: 0xBEE00, sysid: 0x1A4, channel: 0x0001, lra: None }));
        assert!(!s.has_identity());
        assert!(s.push(Broadcast::RfssStatus { sysid: 0x1A4, rfss: 1, site: 7, channel: 0x0001, lra: None, active: None }));
        assert!(s.has_identity());
        assert_eq!(s.announced_hz(), Some(851_012_500));
        assert_eq!(s.neighbors().next().unwrap().1, Some(851_006_250 + 16 * 6_250));
        assert_eq!(s.unresolved(), 0);
        // Heard again: nothing new.
        assert!(!s.push(Broadcast::RfssStatus { sysid: 0x1A4, rfss: 1, site: 7, channel: 0x0001, lra: None, active: None }));
        assert!(!s.push(Broadcast::Adjacent { wacn: None, sysid: 0x1A4, rfss: 1, site: 8, channel: 0x0010, lra: None, flags: None }));
        assert!(s.push(Broadcast::Adjacent { wacn: None, sysid: 0x1A4, rfss: 1, site: 9, channel: 0x0020, lra: None, flags: None }));
        assert_eq!(s.neighbors().count(), 2);
        assert_eq!((s.broadcasts, s.new), (7, 5));
    }

    #[test]
    fn the_nac_heard_most() {
        let mut s = SiteInfo::default();
        assert!(s.nac_heard(0x00A));
        assert!(!s.nac_heard(0x00A));
        assert!(!s.nac_heard(0xFFF));
        assert_eq!(s.nac, Some(0x00A));
    }

    #[test]
    fn a_neighbour_by_tsbk_and_by_mbt_is_one() {
        let flags = Some(SiteFlags { conventional: false, failure: false, valid: true, active: true });
        let mut s = SiteInfo::default();
        assert!(s.push(Broadcast::Adjacent { wacn: None, sysid: 0x911, rfss: 1, site: 40, channel: 0xa000, lra: Some(1), flags }));
        // The MBT form, without flags: nothing new, and the flags stay.
        assert!(!s.push(Broadcast::Adjacent { wacn: None, sysid: 0x911, rfss: 1, site: 40, channel: 0xa000, lra: None, flags: None }));
        assert_eq!(s.neighbors().next().unwrap().0.flags, flags);
    }

    #[test]
    fn secondaries() {
        let mut s = SiteInfo::default();
        assert!(s.push(Broadcast::Secondary { rfss: 1, site: 7, channels: [Some(0x0002), None] }));
        assert!(!s.push(Broadcast::Secondary { rfss: 1, site: 7, channels: [Some(0x0002), None] }));
        s.push(Broadcast::IdenUp(PLAN_800));
        assert_eq!(s.secondary().collect::<Vec<_>>(), vec![(0x0002, Some(851_018_750))]);
    }
}
