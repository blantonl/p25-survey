# P25 Survey Tool

Scan a frequency range with an SDR (RTL-SDR / Airspy / HackRF), identify P25
control channels, and log per-system metadata. Optionally cross-reference
findings against [RadioReference](https://www.radioreference.com/) and
auto-tune SDR gain via BER feedback.

For each control channel found, records:

- **Identity**: WACN, System ID, NAC, RFSS ID, Site ID
- **Channel**: control channel frequency, full IDEN_UP band plan (FDMA + TDMA)
- **Topology**: neighbor sites with frequencies (resolved through the band plan)
- **Signal quality**: RSSI (mean + peak, dBFS), BER, decode rate
- **RR enrichment** (with `--rr`): system name, site description, frequency offset vs database, neighbor diff

Outputs three files per scan: NDJSON survey, plain-text human-readable report,
and a Markdown submission report listing data not yet in RadioReference.

## Quick start

```bash
# Scan 800 MHz P25 with an Airspy
./p25-survey --start 851 --stop 869 --sdr airspy --gain 14 \
    --output /tmp/survey-800.json

# Scan 700 MHz public-safety downlink with RR enrichment
./p25-survey --start 769 --stop 775 --sdr airspy --gain 14 \
    --rr --hide-no-cc --output /tmp/survey-700.json

# Find the best gain for your SDR/location/band
./p25-survey --start 769 --stop 775 --sdr airspy --gain 14 \
    --rr --auto-gain --output /tmp/survey-700.json
```

## Build

Single-file executable via `shiv`:

```bash
make dev-deps     # one-time: install shiv + pytest + numpy/scipy for testing
make              # produces ./p25-survey (~85 KB)
make test         # 135 unit tests, no SDR or GNU Radio needed
./p25-survey --help
```

The executable bundles the package only — `numpy`, `scipy`, `rich`, GNU Radio,
and `gr-op25_repeater` come from the host's Python (this avoids ABI conflicts
with GNU Radio's compiled C extensions).

## Software dependencies

On the host running the survey:

- Python 3.10+
- GNU Radio 3.10+
- `gr-osmosdr` with the driver matching your SDR (RTL-SDR / Airspy / HackRF)
- **`gr-op25_repeater` built from the patched fork at
  [blantonl/op25 branch `p25-survey-patches`](https://github.com/blantonl/op25/tree/p25-survey-patches)**

The op25 fork carries two patches the survey tool depends on:
1. Guard `op25_audio` destructor against an unstarted websocket thread.
2. Add `frame_assembler::get_decode_stats()` for BER + decode rate.

### Ubuntu 24.04 install recipe

Runtime stack (GNU Radio + osmosdr + SDR drivers):

```bash
sudo apt-get update
sudo apt-get install -y \
    gnuradio gnuradio-dev \
    gr-osmosdr libosmosdr-dev \
    librtlsdr-dev libairspy-dev libhackrf-dev \
    python3-pip python3-pybind11
```

Then build and install the patched op25:

```bash
sudo apt-get install -y libcppunit-dev cmake build-essential pkg-config \
    libsndfile1-dev libspdlog-dev
git clone https://github.com/blantonl/op25.git
cd op25 && git checkout p25-survey-patches
mkdir build && cd build && cmake .. && make -j$(nproc) && \
    sudo make install && sudo ldconfig
```

Pure unit tests (`make test`) don't need any of the above.

## How it works

Three phases per scan:

1. **Energy scan** — sweep the SDR's center frequency in chunks of `0.8 ×
   sample_rate`, FFT each capture, find spectral peaks above
   `noise_floor + threshold_db`. Snap to the band's tuning grid. Output: a
   list of candidate frequencies.

2. **P25 decode** — for each candidate, run op25's P25 demodulator + frame
   assembler with adaptive dwell. Bail at `--confirm-timeout` (2 s default)
   if no P25 broadcasts arrive. Otherwise dwell up to `--max-dwell` (12 s)
   while collecting NET_STS_BCST, RFSS_STS_BCST, IDEN_UP*, ADJ_STS_BCST
   broadcasts; resolve neighbor frequencies through the captured band plan.

3. **Optional gain sweep** (`--auto-gain`) — sweep 5 gain values × 4 s dwell
   on each confirmed CC, measure BER per gain, recommend the gain that
   minimizes block error rate. Per-band median across CCs becomes the
   suggested `--gain` for re-running the scan.

## CLI reference

### Frequency sweep

| Flag | Description |
|---|---|
| `--start MHz` | Start frequency (e.g., `851.0`) |
| `--stop MHz` | Stop frequency (e.g., `870.0`) |
| `--step kHz` | Tuning step. Defaults from the band table below; override for special cases. |

### SDR

| Flag | Description |
|---|---|
| `--sdr {rtlsdr,airspy,hackrf}` | Driver. Autoprobed if omitted. |
| `--device-args ARGS` | Raw gr-osmosdr device args (e.g. `airspy=0,LNA=10,MIX=15,IF=12`). Lets you set per-stage gain or other driver-specific options. |
| `--gain dB` | Sets the SDR's default gain stage (Airspy `linearity` preset 0–21, RTL-SDR tuner, HackRF IF/VGA). Omit for AGC. Use `--list-gains` to see the available range. For Airspy, see [`docs/airspy-gain-tables.md`](docs/airspy-gain-tables.md) for the full preset → LNA/MIX/IF lookup tables and per-stage control via `--device-args`. |
| `--ppm PPM` | Frequency correction in PPM. |
| `--list-gains` | Probe the SDR and print available gain stages, then exit. |

### Scan behavior

| Flag | Default | Description |
|---|---|---|
| `--threshold dB` | 8 | Phase 1 energy margin above noise floor. **Lower → more candidates, weaker signals; higher → faster, only strong signals.** See "Tuning the threshold" below. |
| `--confirm-timeout SECONDS` | 2 | Phase 2 abandon-fast: bail if no P25 broadcasts arrive in this window. |
| `--max-dwell SECONDS` | 12 | Hard cap per candidate. Adaptive logic exits earlier when complete. |
| `--hide-no-cc` | off | Hide no-cc rows from the live console table (still saved to JSON). |

### RadioReference enrichment

| Flag | Description |
|---|---|
| `--rr` | Enable RR cross-reference. Prompts for username + password at startup (credentials never stored). Each decoded CC is matched against the RR database; results inline-annotated in the TXT report; non-matching items written to `*-submissions.md`; per-band ppm offsets summarized at end. |

### Auto-gain (BER-driven sweep)

| Flag | Description |
|---|---|
| `--auto-gain` | After Phase 2 finishes, sweep gain values on each confirmed CC and print per-CC + per-band recommendations. After the summary, prompts to re-run the scan with the recommended gain. |
| `--gain-sweep "4,8,12,16,20"` | Custom sweep grid. Defaults to driver-appropriate values. |

### Output

| Flag | Description |
|---|---|
| `--output PATH` | NDJSON survey file. Default: `survey-YYYYMMDD-HHMMSS.json`. **Truncates the file by default**; use `--resume` to append. |
| `--resume` | Append to an existing `--output`; skip frequencies already in the file. |
| `--phase1-only` | Run only Phase 1 (spectrum survey). Skip P25 decode. |
| `--verbose` | Verbose logging. |

## Output files

For `--output /tmp/survey.json`, you get three files:

| File | Format | Contents |
|---|---|---|
| `/tmp/survey.json` | NDJSON | One record per characterized frequency (CCs and no-cc candidates). Survives crashes (per-record fsync); resumable. |
| `/tmp/survey.txt` | Plain text | Human-readable report grouped by control channel, with full band plan, neighbor table, RR annotations, and a Non-CC candidates appendix. |
| `/tmp/survey-submissions.md` | Markdown | Only with `--rr`. Lists items not in RadioReference: new systems, new sites, frequency mismatches, neighbor diffs. Designed to paste into a forum thread or RR support ticket. |

NDJSON survey schema is documented in [`docs/schema.md`](docs/schema.md).

## Tuning the threshold

`--threshold N` is the **dB margin above the median noise floor** that an
FFT bin must clear to be considered a candidate. The trade-off:

| Value | Behavior | When to use |
|---|---|---|
| 6–10 | Sensitive — catches weak signals, more false positives | Hunting weak/distant CCs; rural VHF; willing to spend extra time on no-cc rows |
| 12–16 | Balanced (default 14 in most examples) | First-pass surveys, most US-urban scenarios |
| 18+ | Picky — only strong signals, fast scan | Quick re-scans; densely populated bands where you only care about local sites |

Phase 2 has its own filter (`--confirm-timeout` abandons a candidate that
produces no P25 broadcasts), so a low threshold mostly costs scan time, not
final-report quality. Non-P25 noise gets filtered at the decode step
regardless.

## Tuning step defaults (US)

Auto-selected from the start frequency. Override with `--step <kHz>`.

| Band | Range | Default step | Notes |
|---|---|---|---|
| VHF land mobile | 150–174 MHz | 7.5 kHz | Post-narrowband raster |
| UHF land mobile | 380–512 MHz | 12.5 kHz | Post-narrowband |
| 700 MHz PS narrowband downlink | 769–775 MHz | 6.25 kHz | base TX — **CCs broadcast here** |
| 700 MHz PS narrowband uplink | 799–805 MHz | 6.25 kHz | mobile TX |
| 800 MHz PS rebanded | 851–869 MHz | 12.5 kHz | base TX — **CCs broadcast here** |

## Examples by band

```bash
# Texas 800 MHz statewide PS (TXWARN, regional systems, etc.)
./p25-survey --start 851 --stop 869 --sdr airspy --gain 14 \
    --threshold 14 --rr --hide-no-cc --output /tmp/survey-800.json

# 700 MHz PS narrowband downlink (DART, NCTCOG, etc. for DFW area)
./p25-survey --start 769 --stop 775 --sdr airspy --gain 14 \
    --threshold 14 --rr --hide-no-cc --output /tmp/survey-700.json

# VHF land mobile — be generous with threshold; lots of non-P25 noise
./p25-survey --start 150 --stop 174 --sdr airspy --gain 14 \
    --threshold 8 --rr --hide-no-cc --output /tmp/survey-vhf.json

# UHF land mobile (federal, state, narrowbanded business)
./p25-survey --start 380 --stop 512 --sdr airspy --gain 14 \
    --threshold 12 --rr --hide-no-cc --output /tmp/survey-uhf.json

# Calibrate gain for an unfamiliar SDR/location:
./p25-survey --start 851 --stop 869 --sdr airspy --gain 14 \
    --rr --auto-gain --output /tmp/cal.json
# (At the end, accept the rescan prompt to validate the recommendation.)
```

## Interrupting a scan

`Ctrl-C` exits gracefully. Records decoded up to that point are already on
disk (per-record fsync). Resume with `--resume`:

```bash
# First attempt got Ctrl-C'd partway through:
./p25-survey --start 851 --stop 869 --sdr airspy --rr \
    --output /tmp/survey-800.json
# ... ^C ...
# Pick up where it left off:
./p25-survey --start 851 --stop 869 --sdr airspy --rr \
    --resume --output /tmp/survey-800.json
```

## Project docs

- [`DECISIONS.md`](DECISIONS.md) — every architectural choice with rationale.
- [`PLAN.md`](PLAN.md) — module layout, algorithms, milestones.
- [`docs/schema.md`](docs/schema.md) — NDJSON survey record schema.
- [`docs/airspy-gain-tables.md`](docs/airspy-gain-tables.md) — Airspy linearity/sensitivity preset → LNA/MIX/IF lookup tables, snapshot from libairspy.

## License

GPL-3.0-or-later. Required because `gr-op25_repeater` is GPLv3.
