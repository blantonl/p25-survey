# Trying a radio for the first time

The RTL-SDR, HackRF and HydraSDR drivers have been tested at their sample
rates on resampled recordings (decoding there matched 6 MS/s within a TSBK),
but not yet against the radios. On the machine the radio is plugged into (a
Raspberry Pi 4/5 with a 64-bit OS is fine), as in the README's Install:

```bash
# Rust, for this user only (no sudo)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
# the radio's library and its tools (for the udev rules), e.g.
sudo apt install librtlsdr2 rtl-sdr        # or libhackrf0 hackrf, libhydrasdr1, libairspy0 airspy
cargo build --release
```

Then, for each radio:

1. `./target/release/p25-survey radios`: is it listed (`rtlsdr:SERIAL`,
   `hackrf:SERIAL`, `hydrasdr:SERIAL`)? A library that won't load says so.
   `./target/release/p25-survey gains` shows its gain settings (the first
   radio found; `--radio` for another).
2. A short band first, then a full one:
   ```bash
   ./target/release/p25-survey scan --radio rtlsdr --start 851 --stop 856 --hide-no-cc
   ./target/release/p25-survey scan --radio rtlsdr --start 851 --stop 869 --hide-no-cc --output s.json
   ```
   Look for: the control channels known to be there; "N samples lost"
   at the end (there should be none: if there is, the computer isn't
   keeping up); "no samples from the radio for 2 s" (the stream stopped).
3. Gain: `--gain N` (RTL-SDR: tuner dB; HackRF: LNA dB 0-40, plus `--vga`
   and `--amp`; HydraSDR: linearity 0-21), then `--auto-gain` and check
   the advice it gives makes sense.
4. DC spike (RTL-SDR, HackRF): no candidate should sit at a hop's centre.
   `detect` on a short recording made with the radio shows the spectrum
   the scan saw.
5. The clock: the report's "Radio clock" line is the radio's frequency
   error in ppm (an RTL-SDR without a TCXO can be tens of ppm; the
   receivers follow up to a few kHz).

What is still unknown, per radio:

- RTL-SDR: retuning while librtlsdr's async read runs (it is how librtlsdr
  is used everywhere, but untried here); the AGC default; whether 2.4 MS/s
  runs without drops on a Pi's USB.
- HackRF: the baseband filter at 9.6 MS/s; LNA/VGA defaults; the DC
  blocker's effect on its (larger) spike.
- HydraSDR: the sample rates the RFOne offers (the driver takes the
  highest up to 10 MS/s); the deprecated linearity/sensitivity calls in
  libhydrasdr 1.1 (still exported, and what 1.0.x has).
