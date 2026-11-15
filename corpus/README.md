# p25-survey IQ corpus

Recorded IQ that the Rust p25-survey is developed and tested against, with
reference results from the Python/op25 p25-survey on the same samples.

The recordings stay out of git (size, and control channels carry unit IDs).
They live on dragon1 at `~/p25-corpus/raw/`. This directory holds the
scripts, and the reference results once they are reviewed.

## Recordings

Airspy Mini on dragon1, 6 MSPS complex int16 (`ci16_le`, SigMF), linearity
gain 14 unless noted, ppm 0. Recorded 2026-11-15.

| Name | Center MHz | s | Gain | Why |
|---|---|---|---|---|
| `800-a-853.4` | 853.4 | 45 | lin 14 | Several CCs in one view: DFW Airport simulcast 851.0875, NTIRN 853.7875 / 853.9125, Metrocrest 854.7875, CTTRS Dallas 855.0625 (clean), 855.4125 |
| `800-b-857.6` | 857.6 | 45 | lin 14 | 857.8375 and 859.2125 (op25 decoded 47 % / 27 % in May), NTIRN 858.1375 / 858.4375 / 859.7125 / 859.7375 |
| `800-c-862.6` | 862.6 | 20 | lin 14 | Sweep step 860.1–865.1 |
| `800-d-866.6` | 866.6 | 20 | lin 14 | Sweep step 864.1–869.1 |
| `700-a-772.3` | 772.3 | 45 | lin 14 | 700 MHz downlink 769.8–774.8, incl. DFW East 774.48125 |
| `800-a-853.4-lin8` | 853.4 | 10 | lin 8 | Gain study: step A, low gain |
| `800-a-853.4-lin20` | 853.4 | 10 | lin 20 | Gain study: step A, high gain |

`800-a` + `800-b` + `800-c` + `800-d` cover 851–869 MHz as four sweep steps
(about 5 MHz usable each), so a full 800 MHz scan can be replayed.

At lin 14 the RMS level is about −31.5 dBFS with peaks about 8.6 dB below full
scale; lin 18 starts to approach full scale.

## Scripts

- `capture.py`: records the table above with `airspy_rx` and writes the
  SigMF metadata (with sha512). `--only NAME` re-records one.
- `baseline.py`: reference results from the Python p25-survey **v0.3.5** (a
  clean `git archive` in `~/p25-corpus/p25-survey-v0.3.5`, not a working
  tree) on one recording:
  - Phase 1: `energy_scan.find_peaks` on 1<<17-sample snapshots at 0.5, 5,
    15 and 30 s (what a live scan sees);
  - Phase 2: every candidate (Phase 1, plus the known CCs passed with `--cc`)
    decoded over the whole recording with op25's `p25_demod_cb`, with both
    the CQPSK chain (filter `rrc`, as p25-survey runs it) and FSK4/C4FM
    (filter `rc`; with `rrc` it decodes nothing). Every frame_assembler
    message is logged, and p25-survey's own `_process_msg` builds the record.
- `run_baseline.sh`: `baseline.py` over every recording.
- `compare.py`: the Rust decoder against the baseline, channel by channel
  (copy `raw/` and `baseline/` here first: `rsync -a dragon1:p25-corpus/raw/ corpus/raw/`).

Outputs in `baseline/`: `<name>.phase1.json`,
`<name>.<demod>.records.ndjson` (one SurveyRecord plus fec stats per
candidate), `<name>.<demod>.msgs.ndjson` (every message, in order).

## Using it

The Rust decoder should, on each recording, decode at least the TSBKs op25
does (the better of its two chains) and produce the same identity, band plan,
neighbours and secondary CCs. Short clips cut from these recordings become
the CI fixtures.

## Baseline, 2026-11-15 (op25, best of CQPSK / FSK4)

TSBK decode rate per chain (CRC-passed / attempted, whole recording):

| CC MHz | Identity | CQPSK | FSK4 | Notes |
|---|---|---|---|---|
| 851.0875 | 00001/3CC 20-1, DFW Airport simulcast | 100 | 96 | |
| 852.375 | BEE00/C74 1-1, NAC C71 | 10 | — | weak; not in the May survey |
| 853.7875 | BEE00/44A 1-3, NTIRN NE Tarrant | 87 | 75 | |
| 854.5125 | C0907/F09 1-1 | 13 (no site) | 19 | weak; only FSK4 completes |
| 854.7875 | 46FA8/00A 10-10, Metrocrest | 82 | 8 | simulcast: CQPSK only |
| 855.0625 | DEE00/911 1-64, CTTRS Dallas | 100 | 100 | clean reference |
| 855.4125 | 46F74/0D6 78-78 | 100 | 100 | |
| 857.2125 | BEE00/44A 2-1, NTIRN Irving | 85 | 48 | was 853.9125 in May (CC moved) |
| 857.8375 | 46F74/0D6 60-60 | 84 | 24 | |
| 858.1375 | BEE00/44A 1-2 | 99.5 | 62 | |
| 858.4375 | BEE00/44A 1-1 | 100 | 47 | |
| 858.5375 | BEE00/44A 1-7 | 55 | 14 | |
| 859.2125 | BEE00/693 1-2 | 50 | 78 | FSK4 better |
| 859.2875 | BEE00/693 1-1, NAC 690 | 13 | 7 | weak |
| 859.7125 | BEE00/44A 1-8 | 94 | 22 | |
| 859.7375 | BEE00/44A 3-2 | 89 | 34 | |
| 770.3625 | BEE00/899 1-3 | 15 | — | weak duplicate of 771.36875's site? |
| 771.36875 | BEE00/899 1-3 | 100 | 100 | |
| 771.53125 | 46F74/0D6 70-70 | 100 | 100 | |
| 772.19375 | 46F74/0D6 50-50 | 37 | — | |
| 774.56875 | BEE00/44A 3-1 | 88 | 17 | |
| 774.68125 | BEE00/899 1-1 | 5 | — | weak |

No P25 control channels on 860–869 MHz (`800-c`, `800-d`): negative cases.

Phase 1 (8 dB over the median, single 22 ms snapshot) proposed 60 / 53 / 100
candidates on `800-a` / `800-b` / `700-a` for 7 / 11 / 8 control channels.
That over-detection is what persistence-based detection should cut.

Gain: at lin 8 only the two strongest CCs decode; lin 20 adds Phase 1 noise
(86 candidates) and lowers some decode rates (854.7875: 64 % vs 82 % at lin 14).
