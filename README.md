# P25 Survey

Scan a frequency range with an SDR (Airspy, HydraSDR, RTL-SDR or HackRF),
find the P25 control channels, and log what each one says about its site.
Cross-reference the findings against [RadioReference](https://www.radioreference.com/)
and report what RadioReference doesn't have yet.

For each control channel found, it records:

- **Identity**: WACN, System ID, NAC, RFSS and site, LRA
- **Band plan**: every IDEN_UP entry (FDMA and TDMA, spacing, offset, bandwidth)
- **Topology**: neighbouring sites with their control channels (resolved through
  the band plan) and status (failsoft, failure, conventional, stale)
- **Secondary control channels** the site announces (SCCB)
- **Services and status**: the site's services (SYS_SRV_BCST), failsoft, UTC offset,
  an encrypted control channel (P_PARM_BCST)
- **Signal**: level over the noise floor, the share of control messages that
  decoded, and the carrier's frequency error (the radio's clock error)
- **RadioReference** (with `--rr`): system and site, whether the frequency,
  NAC, secondary control channels and neighbours match what RR lists

It also tells apart what isn't a control channel of its own: voice and
conventional P25, other signals, and **copies** of a strong control channel
(the radio's image, intermodulation), which would otherwise look like sites.

Each scan writes three files: the records (NDJSON), a text report, and, with
`--rr`, a Markdown report of what to submit to RadioReference.

**0.5 is a rewrite in Rust**: one program, with no GNU Radio, op25 or Python
to install, that runs on Linux (including the Raspberry Pi) and macOS
(Windows should build, but is untried). It reads and writes the same survey files as the Python version
(0.4.x, now on the [`python`](../../tree/python) branch); see
[Coming from the Python version](#coming-from-the-python-version). It is
tested on the air with an Airspy; the RTL-SDR, HackRF and HydraSDR drivers
are tested on recorded samples but not yet on the radios
([docs/hardware-testing.md](docs/hardware-testing.md)).

## Quick start

```bash
# Scan 800 MHz with the first radio plugged in
p25-survey scan --start 851 --stop 869

# 700 MHz public-safety downlink, cross-referenced with RadioReference
p25-survey scan --start 769 --stop 775 --rr --hide-no-cc --output survey-700.json

# Find the best gain for your radio, antenna and band
p25-survey scan --start 769 --stop 775 --auto-gain
```

## Install

1. **The library for your radio**, and only that one. p25-survey loads it when
   the radio is opened; nothing else needs it.

   | Radio | Debian / Ubuntu / Raspberry Pi OS | macOS (Homebrew) |
   |---|---|---|
   | Airspy R2, Mini | `sudo apt install libairspy0` | `brew install airspy` |
   | HydraSDR RFOne | `sudo apt install libhydrasdr1` (or build [hydrasdr-host](https://github.com/hydrasdr/hydrasdr-host)) | build hydrasdr-host |
   | RTL-SDR | `sudo apt install librtlsdr2` (older releases: `librtlsdr0`) | `brew install librtlsdr` |
   | HackRF One | `sudo apt install libhackrf0` | `brew install hackrf` |

   On Linux a radio also needs its udev rules to be usable without root:
   the radio's tools package installs them (`sudo apt install airspy`,
   `rtl-sdr` or `hackrf`). An RTL-SDR also needs the kernel's DVB driver kept
   off it (`blacklist dvb_usb_rtl28xxu`).
2. **p25-survey**: download it, or build it.

### Download

From the [latest release](https://github.com/blantonl/p25-survey/releases/latest):

| System | File |
|---|---|
| Linux, x86-64 (Ubuntu 22.04, Debian 12 or newer) | `p25-survey-VERSION-x86_64-unknown-linux-gnu.tar.gz` |
| Linux, 64-bit ARM: Raspberry Pi 4/5 with a 64-bit OS (Raspberry Pi OS 12 or newer) | `p25-survey-VERSION-aarch64-unknown-linux-gnu.tar.gz` |
| macOS, Apple silicon | `p25-survey-VERSION-aarch64-apple-darwin.tar.gz` |
| macOS, Intel | `p25-survey-VERSION-x86_64-apple-darwin.tar.gz` |
| Windows (untried) | `p25-survey-VERSION-x86_64-pc-windows-msvc.zip` |

```bash
tar xzf p25-survey-*.tar.gz
cd p25-survey-*/
./p25-survey radios        # is the radio there?
```

Copy `p25-survey` anywhere on your `PATH` (e.g. `~/.local/bin`). `SHA256SUMS`
in the release has each file's checksum. On macOS, a downloaded program is
quarantined until you allow it: `xattr -d com.apple.quarantine p25-survey`.

### Build from source

1. **Rust**, from [rustup.rs](https://rustup.rs) (1.88 or newer), for your user
   only:
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```
   A C compiler and linker are needed too: on Debian/Ubuntu
   `sudo apt install build-essential`, on macOS `xcode-select --install`.
2. **Build**:
   ```bash
   git clone https://github.com/blantonl/p25-survey
   cd p25-survey
   cargo build --release
   ./target/release/p25-survey radios     # is the radio there?
   ```
   The program is `target/release/p25-survey`; copy it anywhere on your
   `PATH`, or `cargo install --path crates/p25-survey`.

On a Raspberry Pi (4 or 5, 64-bit OS) the first build takes a while.

## How it works

For each tuning of the radio across `--start` to `--stop`:

1. **Look** (0.35 s): short FFTs over the look; for each channel on the band's
   grid (6.25 kHz; 2.5 kHz on VHF), the 20th-percentile power against the local
   noise floor. A control channel never keys down, so it stands out; voice
   channels and bursts don't.
2. **Decode** every candidate at once, from one FFT filter bank, each with a
   C4FM and a CQPSK (simulcast) receiver. A candidate is done when its
   identity, band plan and its own channel are in, every channel number it
   named resolves and nothing new has come for 3 s; or when it isn't P25 (no
   frame in 2 s), or isn't a control channel (no control frames); or at
   `--max-dwell` (12 s).
3. **Gain sweep** (with `--auto-gain`): the control channels found are decoded
   again at each gain of a grid; the advice per band is the lowest gain within
   2 % of the best.

Copies are marked at the end of each tuning: a channel that announces
another's frequency, has the same identity as a better one, or sits at the
mirror image of a much stronger one with its NAC.

## Commands

| Command | |
|---|---|
| `p25-survey scan [--radio R] --start MHz --stop MHz [options]` | Survey with a radio |
| `p25-survey scan <recording>... [options]` | Survey SigMF recordings (each a tuning) |
| `p25-survey report <survey.json> [--rr]` | The reports again from a survey file; `--rr` looks it up now |
| `p25-survey radios` | The radios plugged in (and libraries that aren't installed) |
| `p25-survey gains [--radio R]` | A radio's gain settings (also `scan --list-gains`) |
| `p25-survey detect <recording>` | The carriers that stand out in a recording |
| `p25-survey decode <recording> --freq MHz[,...]` | Decode given control channels from a recording |
| `p25-survey info <recording>` | A recording's metadata and level |

## Scan options

### Frequency sweep

| Option | |
|---|---|
| `--start MHz`, `--stop MHz` | The range (with a radio; for recordings, their span by default) |
| `--step kHz[,kHz...]` | The channel grid(s) to look on. By default the band's: 6.25 kHz, 2.5 kHz on VHF. Several (`5,6.25,7.5,12.5`) for VHF/UHF systems that mix spacings: each is looked on, and a channel found on more than one is kept once. |

### Radio

| Option | |
|---|---|
| `--radio KIND[:SERIAL]` | `airspy`, `hydrasdr`, `rtlsdr` or `hackrf`, optionally one by serial (as `p25-survey radios` lists it). By default the first radio plugged in. |
| `--rate MS/s` | The highest sample rate to use. Defaults: Airspy and HydraSDR their highest up to 10 (R2: 10, Mini: 6), RTL-SDR 2.4, HackRF 9.6. |
| `--gain N` | Airspy, HydraSDR: linearity preset 0-21 (default 14). RTL-SDR: tuner gain in dB, the nearest it has (default: its AGC). HackRF: LNA gain 0-40 dB in 8 dB steps (default 16). `--gain auto`: the radio's AGC. |
| `--sensitivity N` | Airspy, HydraSDR: the sensitivity preset 0-21 instead. |
| `--lna N --mix N --if N` | Airspy, HydraSDR: each stage set, AGC off. See [docs/airspy-gain-tables.md](docs/airspy-gain-tables.md). |
| `--vga N`, `--amp` | HackRF: baseband gain 0-62 dB (default 24); the 14 dB RF amplifier. |
| `--ppm N` | The radio's frequency error, as the report gives it (e.g. `0.83`): every tuning is corrected by it. The receivers follow a few kHz of error regardless. |
| `--list-gains` | Print the radio's gain settings and exit. |

### Scan behaviour

| Option | Default | |
|---|---|---|
| `--threshold dB` | 4.5 | How far over the local noise floor a channel's steady power must be to be decoded. Lower: weaker channels, more candidates (each costs CPU, not time: they're decoded together). |
| `--confirm-timeout S` | 2 | A candidate with no P25 frame by then isn't P25. |
| `--max-dwell S` | 12 | The most a candidate is listened to. |
| `--phase1-only` | | Only look: list each tuning's candidates, decode none. |
| `--hide-no-cc` | | Leave what isn't a control channel out of the console (it's still in the files). |

### RadioReference

| Option | |
|---|---|
| `--rr` | Look each control channel up in RadioReference (a Premium subscription). The login is asked for at the start of every run and never stored; an expired subscription is refused. The lookups run beside the scan. |
| `--rr-stdin` | Read the username and password from the first two lines of standard input instead of asking (for scripts). |

### Gain sweep

| Option | |
|---|---|
| `--auto-gain` | After each tuning, decode its control channels again at each gain of the grid; print the advice per band, and offer to scan again at it. |
| `--gain-sweep N,N,...` | The grid. Defaults: Airspy/HydraSDR 6,9,12,15,18; RTL-SDR 20,28,34,39,44,49.6; HackRF 0,8,16,24,32,40. |

### Output

| Option | |
|---|---|
| `--output PATH` | The survey file (NDJSON). Default `survey-YYYYMMDD-HHMMSS.json` (UTC). Truncated at the start, unless `--resume`. |
| `--resume` | Keep the records already in `--output` and survey only what they don't cover (partial control channels get another chance). |

## Output files

For `--output survey.json`:

| File | |
|---|---|
| `survey.json` | NDJSON, one record per candidate (control channels, copies, other P25, and the rest). Written and synced to disk as each tuning ends, so a crash loses at most the one under way. Schema: [docs/schema.md](docs/schema.md). |
| `survey.txt` | The report: the radio's clock error, every control channel with its band plan, neighbours, secondary control channels and RR match, then the copies, other P25 and the rest. |
| `survey-submissions.md` | With `--rr`: new systems, new sites, frequency and NAC mismatches, secondary and neighbour control channels RR doesn't list (or lists as traffic), neighbours missing from RR's roster. To paste into a forum thread or a ticket. |

## Bands (US)

| Band | Range | Grid |
|---|---|---|
| VHF high | 136–174 MHz | 2.5 kHz |
| UHF | 380–512 MHz | 6.25 kHz |
| 700 MHz public safety, downlink (control channels) | 769–775 MHz | 6.25 kHz |
| 800 MHz public safety, rebanded (control channels) | 851–869 MHz | 6.25 kHz |
| 900 MHz SMR, downlink | 935–940 MHz | 6.25 kHz |

```bash
p25-survey scan --start 851 --stop 869 --rr --hide-no-cc --output survey-800.json
p25-survey scan --start 769 --stop 775 --rr --hide-no-cc --output survey-700.json
p25-survey scan --start 935 --stop 940 --rr --hide-no-cc --output survey-900.json
p25-survey scan --start 150 --stop 174 --step 2.5,6.25,7.5 --rr --hide-no-cc --output survey-vhf.json
p25-survey scan --start 450 --stop 470 --rr --hide-no-cc --output survey-uhf.json
```

## Interrupting and resuming

Ctrl-C finishes the tuning under way with what it has, waits for the
RadioReference lookups and writes the reports; Ctrl-C again stops at once.
`--resume` with the same `--output` carries on where it stopped.

## The radio's clock

The reports give the radio's frequency error, measured on the control
channels' carriers (base stations are GPS-disciplined): `Radio clock: +0.83 ppm`.
Pass it as `--ppm 0.83` to correct every tuning by it. It isn't needed to
decode (the receivers follow a few kHz of error), but it puts the carriers
where the band plan says they are.

## Coming from the Python version

The survey files are the same: the Python version's reports read this
version's records, and `p25-survey report` reads the Python version's. Fields
this version adds (verdict, level, copies, carrier offset) are at the top
level of each record.

| Python (0.4) | Rust (0.5) |
|---|---|
| `./p25-survey --start … --stop …` | `p25-survey scan --start … --stop …` |
| `--sdr airspy` | `--radio airspy` (`--sdr` still works) |
| `--device-args "airspy=0,LNA=10,MIX=15,IF=12"` | `--lna 10 --mix 15 --if 12` |
| `--device-args "airspy=0,sensitivity=12"` | `--sensitivity 12` |
| `--sample-rate 6` | `--rate 6` (`--sample-rate` still works) |
| `--threshold 8` (over the median of one snapshot) | `--threshold 4.5` (over the local floor of the 20th-percentile power over 0.35 s) |
| `--list-gains` | `--list-gains`, or `p25-survey gains` |
| `--verbose` | (gone) |

What else changed:

- No GNU Radio, op25, gr-osmosdr or Python: one program and the radio's library.
- Every candidate in a tuning is decoded at once, each with a C4FM and a CQPSK
  receiver, so a band takes about as long as its tunings (800 MHz: four, about
  30 seconds with an Airspy), and decodes more than op25 did, notably on weak
  and simulcast sites.
- Copies of a strong control channel are recognised as such.
- The clock error comes from the carriers on the air, not from RR's frequencies.
- The gain sweep counts control messages decoded, so it sees frames lost
  altogether, not only those that fail their CRC.

## Development

```bash
cargo test            # unit tests; the corpus test runs if the recordings are in corpus/raw
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

- [DECISIONS.md](DECISIONS.md): every design decision and why, with what the
  corpus and live scans measured.
- [corpus/README.md](corpus/README.md): the IQ recordings it's developed and
  tested against, and op25's results on them.
- [docs/schema.md](docs/schema.md): the survey record.
- [docs/airspy-gain-tables.md](docs/airspy-gain-tables.md): what each Airspy
  gain preset sets.
- [docs/hardware-testing.md](docs/hardware-testing.md): trying a radio for the
  first time.
- [CHANGELOG.md](CHANGELOG.md).

## License

Copyright © 2026 RadioReference.com LLC.

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or
[MIT license](LICENSE-MIT), at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted
for inclusion in this work by you, as defined in the Apache-2.0 license, shall
be dual licensed as above, without any additional terms or conditions.

Versions 0.4.x and earlier (Python, on the `python` branch) are GPL-3.0-or-later.
