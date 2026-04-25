# P25 Survey Tool — Implementation Plan

Companion to `DECISIONS.md`. This file describes *how* we'll build it.

## High-level flow

```
┌────────────────┐
│  CLI parser    │  start/stop freq, step, sdr, gain, ppm, output, threshold
└───────┬────────┘
        │
┌───────▼────────┐    ┌──────────────────────┐
│ Band detector  │ →  │ default step lookup  │
└───────┬────────┘    └──────────────────────┘
        │
┌───────▼────────────────────────────────────────────────┐
│ Phase 1: Energy scan                                   │
│  for chunk in range(start, stop, sdr_bw):              │
│    tune SDR → capture → FFT → find peaks > threshold   │
│    snap to step grid → emit candidates                 │
└───────┬────────────────────────────────────────────────┘
        │
        │  candidates: List[float]
        │
┌───────▼────────────────────────────────────────────────┐
│ Phase 2: Decode each candidate                         │
│  tune SDR → P25 demodulator → frame assembler          │
│  parse TSBKs:                                          │
│    NET_STS_BCST  → WACN, SYSID                         │
│    RFSS_STS_BCST → RFSS, Site                          │
│    IDEN_UP*      → band plan (channel→freq map)        │
│    ADJ_STS_BCST  → neighbors (need band plan to xlate) │
│  collect RSSI + BER while dwelling                     │
│  exit when complete OR max_dwell reached               │
└───────┬────────────────────────────────────────────────┘
        │
┌───────▼────────────────────────────────────────────────┐
│ Output                                                 │
│   append NDJSON record to survey file                  │
│   update live console table                            │
└────────────────────────────────────────────────────────┘
```

## Module layout

```
p25-survey/
├── DECISIONS.md
├── PLAN.md
├── README.md
├── pyproject.toml          # entry point: p25-survey = p25_survey.cli:main
├── p25_survey/
│   ├── __init__.py
│   ├── cli.py              # argparse + orchestrator
│   ├── bands.py            # band table, default step lookup
│   ├── sdr.py              # gr-osmosdr wrappers, device probing, retune
│   ├── energy_scan.py      # Phase 1: FFT-based candidate finder
│   ├── decoder.py          # Phase 2: op25 demod + frame assembler integration
│   ├── tsbk.py             # TSBK collector: FDMA opcodes 0x39/0x3a/0x3b/0x3c + TDMA 0xfa/0xfb/0xfe
│   ├── survey.py           # SurveyRecord dataclass + NDJSON writer
│   ├── report.py           # post-scan plain-text summary report writer
│   ├── console.py          # live table rendering (rich)
│   └── _vendored/
│       ├── REVENDOR.md     # provenance + re-vendoring procedure
│       └── op25/
│           ├── p25_demodulator.py
│           ├── p25_decoder.py
│           ├── helper_funcs.py
│           └── log_ts.py
├── configs/
│   └── example.yaml        # SDR config sample
├── docs/
│   └── schema.md           # JSON survey record schema
└── tests/
    ├── test_bands.py
    ├── test_tsbk.py        # decode known TSBK byte streams from captures
    └── test_energy_scan.py # synthetic IQ → expected peaks
```

## op25 reuse map

Files we'll import from `op25/op25/gr-op25_repeater/apps/`:

**Vendored (copied into `p25_survey/_vendored/op25/` with upstream SHA in header):**

| op25 source | What we use |
|---|---|
| `p25_demodulator.py` | `p25_demod_cb` / `p25_demod_fb` — C4FM/CQPSK demod chain output to symbols |
| `p25_decoder.py` | `p25_decoder_sink_b` — frame assembler block + msgq for decoded frames |
| `helper_funcs.py` | `from_dict`, `get_ordinals` |
| `log_ts.py` | timestamping |

**Referenced but not vendored:**

| op25 source | Why |
|---|---|
| `tk_p25.py` | Bit-layout reference for TSBK opcodes (lines 661, 944, 977, 1288, 1315, 1348, 1355). We reimplement just the four FDMA opcodes (`0x39/0x3a/0x3b/0x3c`) plus three TDMA opcodes (`0xfa/0xfb/0xfe`) in `p25_survey/tsbk.py`, decoupled from `rx_block`. |

**System dependency:** `gr-op25_repeater` C++ module (provides `op25_repeater.frame_assembler`, `op25.fsk4_demod_ff`, etc.) must be built and installed on the host — standard op25 prerequisite, not vendorable.

## Key algorithms

### Energy scan peak detection

```
for fc in arange(start + bw/2, stop, bw * 0.8):       # 0.8 → leave guardband
    samples = sdr.read(fc, n=2**18)                    # ~262k samples ≈ 100 ms @ 2.4 MHz
    psd = welch(samples, nperseg=4096)                 # ~600 Hz bin width
    noise_floor = median(psd)                          # robust, ignores peaks
    peaks = bins where psd > noise_floor + threshold_db
    coalesce adjacent bins (P25 CC is 12.5 kHz wide)
    snap each peak center to nearest step boundary
    emit (freq, peak_power_dbm)
```

Threshold default: 8 dB above median (tunable via `--threshold`).

### TSBK collection state machine

```
state = {
    'confirmed': False,
    'wacn': None, 'sysid': None, 'nac': None,
    'rfss': None, 'site': None,
    'iden_up': {},         # channel_id → (base, step, offset, type)
    'neighbors': [],       # list of {wacn, sysid, rfss, site, freq}
    'rssi_samples': [],
    'ber_samples': [],
}

on each TSBK (FDMA — fires on every Phase 1 CC):
    if opcode == 0x3b: state['wacn'], state['sysid'] = parse_net_sts(tsbk)
    if opcode == 0x3a: state['rfss'], state['site'] = parse_rfss_sts(tsbk)
    if opcode == 0x39: state['iden_up'][ch_id] = parse_iden_up(tsbk)
    if opcode == 0x3c:
        adj = parse_adj_sts(tsbk)
        adj['freq'] = resolve_channel(adj['ch_id'], state['iden_up'])
        state['neighbors'].append(adj)

on each TDMA broadcast PDU (handlers wired in but only fire if we ever decode Phase 2 voice slots — not in v1's CC-only flow):
    if opcode == 0xfb: parse_tdma_net_sts(...)
    if opcode == 0xfa: parse_tdma_rfss_sts(...)
    if opcode == 0xfe: parse_tdma_adj_sts(...)

complete when: wacn, sysid, rfss, site all set AND iden_up has the entries needed to resolve any pending neighbor freqs AND at least 2s elapsed since last new ADJ_STS_BCST.
```

### Adaptive dwell exit conditions

| Condition | Action |
|---|---|
| No frame sync in 2s | abandon: not a CC |
| `wacn && sysid && rfss && site && iden_up_complete && neighbors_settled` | success: log and move on |
| `max_dwell` (12s) reached | log partial result, mark `complete=false` |

## Survey JSON schema (preview)

```json
{
  "ts": "2026-04-25T18:23:11.402Z",
  "freq_hz": 851012500,
  "freq_mhz": 851.0125,
  "complete": true,
  "wacn": "BEE00",
  "system_id": "1A4",
  "nac": "293",
  "rfss_id": 1,
  "site_id": 7,
  "neighbors": [
    {"freq_mhz": 851.5375, "rfss_id": 1, "site_id": 8, "wacn": "BEE00", "system_id": "1A4"},
    {"freq_mhz": 852.0125, "rfss_id": 1, "site_id": 9, "wacn": "BEE00", "system_id": "1A4"}
  ],
  "iden_up": [
    {"id": 0, "type": "P25", "base_mhz": 851.00625, "step_khz": 6.25, "offset_mhz": 0}
  ],
  "signal": {
    "rssi_dbfs_mean": -42.1,
    "rssi_dbfs_peak": -38.7,
    "ber_pct_mean": 0.4,
    "decode_rate_pct": 98.7
  },
  "dwell_ms": 4123,
  "sdr": {"driver": "rtlsdr", "ppm": 0, "gain_db": 40}
}
```

## CLI shape (preview)

```
p25-survey \
    --start 851.0 \
    --stop 870.0 \
    [--step 12.5]                  # kHz; auto from band if omitted
    [--sdr rtlsdr|airspy|hackrf]   # autoprobe if omitted
    [--device-args "rtl=0"]        # passed to gr-osmosdr
    [--gain 40]                    # dB
    [--ppm 0]
    [--threshold 8]                # dB above noise floor
    [--max-dwell 12]               # seconds per candidate
    [--output survey.json]
    [--resume]                     # skip freqs already in output
    [--thorough]                   # disable FFT scan, walk every step
    [--verbose]
```

## Test strategy

Two tiers based on the dev/test split (Mac dev, Linux op25 host for hardware):

**Mac (no GNU Radio dependency):**
- `tests/test_bands.py` — default step lookup for boundary frequencies.
- `tests/test_tsbk.py` — feed known-good TSBK byte sequences (captured from a real CC, anonymized) and verify field extraction. Covers both FDMA (0x39/0x3a/0x3b/0x3c) and TDMA (0xfa/0xfb/0xfe) opcodes.
- `tests/test_energy_scan.py` — synthesize IQ with a tone at known offset, verify peak detection + grid snap. Uses `numpy` only, no SDR.
- `tests/test_report.py` — render a sample NDJSON survey to text, snapshot.

**Linux op25 host (live SDR):**
- Smoke test: scan a 5 MHz window known to contain an active CC, confirm detection.
- End-to-end: full band scan, compare detected systems against RR ground truth.
- Documented as a manual recipe in `README.md`.

## Build / install

- Python 3.10+ (matching what op25 builds against on recent Debian/Ubuntu)
- Depends on system packages: `gnuradio`, `gr-osmosdr`, `gr-op25_repeater` (built from sibling `../op25`)
- Pure Python deps: `numpy`, `scipy` (welch), `rich` (console table), `click` or `argparse` (sticking with stdlib argparse)
- Install: `pip install -e .` from the project root; entry point exposes `p25-survey` command

## Risks / unknowns

1. **Retune speed on RTL-SDR** is ~30–50 ms; for the energy scan that's fine, but verify it's not a bottleneck for Phase 2 candidate iteration. Airspy/HackRF retune faster.
2. **op25's demodulator expects a sustained sample stream** for AGC and FLL convergence. We may need to give it 200–500 ms of warmup after each retune before counting "no sync = not a CC". Will tune empirically.
3. **`channel_id_to_string` neighbor resolution** depends on receiving IDEN_UP* before ADJ_STS_BCST. Deferred-resolution (re-walk pending neighbors when iden_up arrives) handles arrival order issues.
4. **GNU Radio flowgraph mutation cost** — retuning is cheap; restarting a flowgraph for each candidate is expensive. Plan: keep one flowgraph alive, only retune the source. Verify this works with op25's demodulator block.

## Milestones

1. **Skeleton** — package scaffold, CLI parses args, prints intent. *(ready to start)*
2. **Bands + tuning step defaults** — `bands.py` with table + tests.
3. **SDR layer** — `sdr.py` with gr-osmosdr wrapper, device probe, sample capture.
4. **Energy scan** — Phase 1 working; emits candidate list from a real range.
5. **TSBK parser** — `tsbk.py` standalone (no GNU Radio); unit tests with captured bytes.
6. **Decoder integration** — wire op25 demod + frame_assembler msgq into our TSBK parser; confirm CC detection on a known-good freq.
7. **Survey output** — NDJSON writer + live console table.
8. **End-to-end** — full scan of a known band, validate against RR data.
9. **Polish** — resume support, thorough mode, error paths.
