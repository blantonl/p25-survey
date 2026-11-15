//! Signal processing: filters, channel extraction, demodulation.

pub mod channel;
pub mod filter;
pub mod fm;

pub use channel::{Channel, SYMBOL_RATE};
pub use fm::Discriminator;
