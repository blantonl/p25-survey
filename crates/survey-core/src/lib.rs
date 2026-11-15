//! The survey engine, with no I/O: it is given samples and a sample clock and
//! returns what it decoded, and asks its caller to retune or change gain.
//! Radios, files, threads and wall-clock time belong to the caller
//! (`survey-radio`, `p25-survey`), so everything here runs the same on a live
//! radio and on a recording, and is tested on recordings.
//!
//! - [`p25`]: TSBK and TDMA MAC broadcast parsing, the band plan, and what a
//!   control channel has told us about its site ([`p25::SiteInfo`]).

pub mod p25;
