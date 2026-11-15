//! Signal processing: filters, channel extraction, demodulation.

pub mod channel;
pub mod channelizer;
pub mod cqpsk;
pub mod detect;
pub mod filter;
pub mod fm;

pub use channel::{Channel, SYMBOL_RATE};
pub use channelizer::{Channelizer, HeadId};
pub use fm::Discriminator;
