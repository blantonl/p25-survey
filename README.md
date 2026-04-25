# P25 Survey Tool

Scan a frequency range for P25 control channels. For each one found, log:

- WACN, System ID, NAC
- RFSS ID, Site ID
- Control channel frequency
- Neighbor sites (RFSS/Site/frequency)
- Signal strength (RSSI) and quality (BER, decode rate)

Writes an NDJSON survey file and a plain-text summary report. Skips voice channels.

## Build

Requires Python 3.10+ and `shiv` for packaging.

```bash
make dev-deps     # installs shiv + test deps
make              # produces single-file executable: ./p25-survey
./p25-survey --help
```

## Run

```bash
./p25-survey --start 851.0 --stop 870.0 --sdr rtlsdr --output survey.json
```

### Gain control

`--gain N` sets the SDR's default gain stage. The semantics are driver-specific —
use `--list-gains` to see what your hardware actually exposes:

```bash
./p25-survey --list-gains --sdr airspy
# → Default gain (used by --gain N):  0 – 21 dB, step 1
#   Per-stage gains: LNA / MIX / IF, each 0 – 15 dB
```

For per-stage control on any driver, bypass `--gain` and use raw osmosdr args:

```bash
./p25-survey --start 851 --stop 870 --sdr airspy \
    --device-args "airspy=0,LNA=10,MIX=15,IF=12"
```

Hardware runtime requirements (on the host running the survey):

- GNU Radio (3.10+ recommended)
- `gr-op25_repeater` Python module installed — same prerequisite as running op25's `multi_rx.py`
- `gr-osmosdr` with the driver you're using (rtlsdr / airspy / hackrf)
- A supported SDR plugged in

Pure unit tests (`make test`) don't need any of the above.

## Tuning step defaults (US)

Auto-selected from start frequency; override with `--step <kHz>`:

| Band | Range | Default step | Notes |
|---|---|---|---|
| VHF | 150–174 MHz | 7.5 kHz | |
| UHF | 380–512 MHz | 12.5 kHz | |
| 700 MHz PS NB downlink | 769–775 MHz | 6.25 kHz | base TX — **CCs here** |
| 700 MHz PS NB uplink | 799–805 MHz | 6.25 kHz | mobile TX |
| 800 MHz PS rebanded | 851–869 MHz | 12.5 kHz | base TX — CCs here |

## Project docs

- [`DECISIONS.md`](DECISIONS.md) — every architectural choice with rationale.
- [`PLAN.md`](PLAN.md) — module layout, algorithms, milestones.

## Status

Early development. CLI and band logic land first; SDR and decoder integration follow on a Linux host with op25 already running.
