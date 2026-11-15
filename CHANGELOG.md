# Changelog

## 0.5.1 (2026-11-15)

- Prebuilt binaries with each release: Linux (x86-64, 64-bit ARM / Raspberry
  Pi), macOS (Apple silicon, Intel), Windows (untried).
- `--auto-gain` looks at each tuning again at the highest gain that decodes
  its control channels as well as the best (when above the scan's), and
  decodes what was too weak at the scan's gain. On an antenna that needed
  more gain, this found 4 control channels at 700 MHz and 3 at 800 MHz that
  the scan had missed: as many as a scan at the gain that antenna needs.
- The Airspy/HydraSDR gain sweep goes up to preset 21, and the advice says
  when the best gain is the highest tried.

## 0.5.0 (2026-11)

A rewrite in Rust: one program, with no GNU Radio, op25, gr-osmosdr or
Python. It does everything 0.4.0 did, reads and writes the same survey files,
and keeps the old flag names where it can (see the README's "Coming from the
Python version").

- **Its own P25 decoding**, from the TIA-102 standards: frame sync, NID,
  trellis, CRCs, TSBKs, MBT PDUs and the Phase 2 MAC broadcasts, with a C4FM
  and a CQPSK (simulcast) receiver on every channel.
- **Every candidate in a tuning decoded at once**, from one FFT filter bank:
  a band takes about as long as its tunings (800 MHz: about 30 seconds with
  an Airspy) instead of growing with its candidates.
- **More decoded than op25**, on the same recordings: 115 % of op25's better
  receiver's site broadcasts, every site it identified, and seven more
  (weak and simulcast sites above all).
- **Carrier detection by steady power** (the 20th percentile over 0.35 s,
  against the local floor, on the channel grid): far fewer false candidates,
  and two control channels 25 kHz apart both found.
- **Copies** (the radio's image, intermodulation of a strong control
  channel) recognised and marked, not reported as sites.
- **The radio's clock error** measured on the carriers, and `--ppm` to
  correct it.
- **Gain sweep** scoring control messages decoded, so frames lost altogether
  count; advice per band, and the offer to scan again at it.
- **Radios**: Airspy (R2 at 10 MS/s, Mini at 6), HydraSDR, RTL-SDR and
  HackRF, each through its maker's library, loaded only when used. The first
  radio plugged in is used by default. RTL-SDR, HackRF and HydraSDR are not
  yet tried on the radios themselves.
- **RadioReference** lookups run beside the scan; a weak control channel
  whose WACN wasn't decoded borrows it from the survey's others with the
  same System ID; `--rr-stdin` for scripts.
- `--resume`, Ctrl-C (finish the tuning, write the reports), records synced
  to disk each tuning; `p25-survey report` renders the reports from a
  survey file and can look it up afterwards; `detect`, `decode` and `info`
  for recordings.
- Licensed MIT OR Apache-2.0 (0.4.x and earlier: GPL-3.0-or-later).

## 0.4.0 and earlier

The Python version (on GNU Radio and op25): see the `python` branch and the
releases up to v0.4.0.
