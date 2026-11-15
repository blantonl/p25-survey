//! P25 control channel broadcasts: what a survey learns a site from.

mod bandplan;
pub mod crc;
pub mod framer;
mod mac;
pub mod nid;
mod site;
pub mod trellis;
mod tsbk;

pub use bandplan::{BandPlan, IdenUp};
pub use mac::parse_mac;
pub use site::{Neighbor, SiteInfo};
pub use tsbk::{TSBK_LEN, parse_tsbk};

/// The broadcasts that describe a site, from an FDMA TSBK or a TDMA MAC PDU.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Broadcast {
    /// A band plan entry: how channel numbers become frequencies.
    IdenUp(IdenUp),
    /// NET_STS_BCST: the network (WACN) and system, and this channel.
    NetStatus { wacn: u32, sysid: u16, channel: u16 },
    /// RFSS_STS_BCST: this site, and this channel.
    RfssStatus { sysid: u16, rfss: u8, site: u8, channel: u16 },
    /// ADJ_STS_BCST: a neighbouring site's control channel. The WACN only
    /// comes in the TDMA extended form.
    Adjacent { wacn: Option<u32>, sysid: u16, rfss: u8, site: u8, channel: u16 },
    /// SCCB / SCCB_EXP: this site's secondary control channels.
    Secondary { rfss: u8, site: u8, channels: [Option<u16>; 2] },
}
