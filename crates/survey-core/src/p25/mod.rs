//! P25 control channel broadcasts: what a survey learns a site from.

mod bandplan;
pub mod crc;
pub mod framer;
mod mac;
mod mbt;
pub mod nid;
pub mod receiver;
mod site;
pub mod trellis;
mod tsbk;

pub use bandplan::{BandPlan, IdenUp};
pub use mac::parse_mac;
pub use mbt::parse_mbt;
pub use site::{Neighbor, SiteInfo};
pub use tsbk::{TSBK_LEN, parse_tsbk};

/// A site's status bits, as RFSS_STS_BCST / ADJ_STS_BCST carry them (C, F, V, A).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SiteFlags {
    /// C: a conventional channel.
    pub conventional: bool,
    /// F: the site is in a failure condition.
    pub failure: bool,
    /// V: the information is current (clear: last known).
    pub valid: bool,
    /// A: the site has an active network connection to its RFSS controller
    /// (clear: failsoft, site trunking).
    pub active: bool,
}

/// The broadcasts that describe a site, from an FDMA TSBK or MBT, or a TDMA
/// MAC PDU.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Broadcast {
    /// A band plan entry: how channel numbers become frequencies.
    IdenUp(IdenUp),
    /// NET_STS_BCST: the network (WACN) and system, and this channel.
    NetStatus { wacn: u32, sysid: u16, channel: u16, lra: Option<u8> },
    /// RFSS_STS_BCST: this site, and this channel; `active` is its A bit.
    RfssStatus { sysid: u16, rfss: u8, site: u8, channel: u16, lra: Option<u8>, active: Option<bool> },
    /// ADJ_STS_BCST: a neighbouring site's control channel. The WACN only
    /// comes in the TDMA extended form; the flags in the FDMA TSBK.
    Adjacent { wacn: Option<u32>, sysid: u16, rfss: u8, site: u8, channel: u16, lra: Option<u8>, flags: Option<SiteFlags> },
    /// SCCB / SCCB_EXP: this site's secondary control channels.
    Secondary { rfss: u8, site: u8, channels: [Option<u16>; 2] },
    /// SYS_SRV_BCST: the services the site offers now, and is equipped for
    /// (24-bit sets: [`service_names`]), and its request priority level.
    SystemService { available: u32, supported: u32, priority: u8 },
    /// TIME_DATE_ANN: the local time's offset from UTC, minutes (when the
    /// site says it's valid).
    TimeDate { utc_offset_min: Option<i16> },
    /// P_PARM_BCST: the control channel is protected (encrypted), with this
    /// algorithm.
    Protected { algid: u8 },
}

/// System services, TIA-102.AABC Table 6.2.19-1: bit b1 is the 24-bit
/// field's MSB. The flag and reserved bits have no name.
const SERVICES: [Option<&str>; 24] = [
    None,
    None,
    Some("network active"),
    None,
    Some("group voice"),
    Some("individual voice"),
    Some("PSTN-unit voice"),
    Some("unit-PSTN voice"),
    None,
    Some("group data"),
    Some("individual data"),
    None,
    Some("unit registration"),
    Some("group affiliation"),
    Some("group affiliation query"),
    Some("authentication"),
    Some("encryption"),
    Some("user status"),
    Some("user message"),
    Some("unit status"),
    Some("user status query"),
    Some("unit status query"),
    Some("unit page"),
    Some("emergency alarm"),
];

/// The names of the services set in a 24-bit SYS_SRV_BCST field.
pub fn service_names(bits: u32) -> Vec<&'static str> {
    SERVICES.iter().enumerate().filter_map(|(n, name)| name.filter(|_| bits >> (23 - n) & 1 == 1)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn services() {
        // b5 (group voice) and b13 (unit registration), and b1 (a flag: no name).
        let bits = 1 << 19 | 1 << 11 | 1 << 23;
        assert_eq!(service_names(bits), vec!["group voice", "unit registration"]);
    }
}
