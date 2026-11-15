# Decisions

## 2026-11-15 — Rewrite in Rust, from scratch

The Python p25-survey works but needs GNU Radio, gr-osmosdr and a built op25
on the host, which is most of the trouble of installing it, and ties it to
Linux. The rewrite is one program with its own DSP and P25 decoding.

It is written from the P25 standards (TIA-102) and from what the corpus
measures, not by translating another decoder: no op25 or Trunk Recorder code,
and no dependency on either. Ideas from published work are fair game; code is
not copied.

## 2026-11-15 — An engine with no I/O

`survey-core` takes samples and a sample count and returns events; when it
wants the radio retuned it says so and waits to be told it happened. It never
opens a device, starts a thread or reads the clock. So:

- every part of the survey runs on recordings, in tests and in CI, exactly as
  it runs on a radio;
- timing (dwell, settle, give-up) is counted in samples, so a recording
  replayed faster than real time behaves as it did live.

The Python version's decode logic lives inside a GNU Radio flowgraph and
wall-clock timers, which is why its tests stop short of the decoder.

## 2026-11-15 — Decode every candidate in a tuning at once

The Python version retunes to each candidate in turn and decodes it for up to
12 s, so a band's scan time grows with the number of candidates. Here one
FFT-based channelizer cuts every candidate out of the same wideband samples,
and every control channel in a tuning is decoded together for the whole
dwell. Scan time grows with the number of tunings instead: at 6 MS/s an
Airspy covers 851–869 MHz in four.

## 2026-11-15 — Find carriers that stay on

The Python Phase 1 takes one 22 ms spectrum and keeps anything 8 dB over the
median. On the corpus that proposes 60, 53 and 100 candidates where there are
7, 11 and 8 control channels: voice channels keyed at that moment, and
spurs. A control channel never keys down, so the detector takes a low
percentile of each frequency's power over a few hundred milliseconds against
the local noise floor; only carriers that stayed on survive.

## 2026-11-15 — Both receivers, the better one per channel

op25's CQPSK chain (what the Python version runs) is far better on simulcast
(Metrocrest 854.7875: 82 % of TSBKs vs 8 % with C4FM), but C4FM is better on
others (859.2125: 78 % vs 50 %). Each channel runs both until one has clearly
done better, then keeps that one.

## 2026-11-15 — Broadcast parsing follows TIA-102, not op25

Where the Python version (after op25) and the standard differ, the standard
wins:

- ADJ_STS_BCST carries the neighbour's System ID (bits 51..40); it is kept.
- The transmit-offset sign bit means positive when set, in IDEN_UP_TDMA's MAC
  form as in the FDMA ones (op25 and the Python version invert it there).
- Only manufacturer ID 0 is parsed: a manufacturer's TSBK with the same opcode
  means something else. Protected (encrypted) TSBKs are skipped.
- An SCCB entry with System Service Class 0 is unused, as is channel $FFFF.
- Neighbours are kept by (RFSS, site), not by channel number.

## 2026-11-15 — The corpus

Seven recordings from dragon1's Airspy Mini (6 MS/s, 16-bit; 800 MHz in four
tunings, 700 MHz, and two gain settings), with op25's results on every
candidate over the whole recording, from both of its receivers. See
[corpus/README.md](corpus/README.md). The recordings and the message logs
(which carry unit IDs) stay out of git; the broadcasts used as test fixtures
carry none.

## 2026-11-15 — License

GPL-3.0-or-later, as the Python version. That was required there by op25;
here nothing requires it, and it can be revisited before the first release.
