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

Seven recordings from an Airspy Mini (6 MS/s, 16-bit; 800 MHz in four
tunings, 700 MHz, and two gain settings), with op25's results on every
candidate over the whole recording, from both of its receivers. See
[corpus/README.md](corpus/README.md). The recordings and the message logs
(which carry unit IDs) stay out of git; the broadcasts used as test fixtures
carry none.

## 2026-11-15 — C4FM receiver: timing and levels from each frame sync

The channel's FM discriminator is averaged over a symbol (integrate and
dump). Each frame sync is found by correlating with the sync's 24 outer
symbols (normalised, so level and frequency offset don't matter; the sign
gives the polarity); its +3 and -3 symbols give the outer level and centre
for slicing the rest of the frame. A control channel's frames are a few
hundred symbols each, so there is no clock to track between syncs.

The ½-rate trellis symbols go out as a constellation point per *pair* of
dibits (TIA-102.BAAA's table), not as their 4 bits; the Viterbi compares the
received dibits with each branch's, so a symbol off by one level costs one
bit, as noise makes it.

On the corpus (2026-11-15), C4FM alone: every site broadcast op25 decodes on
the clean channels; more than op25's better chain on the C4FM sites (853.7875
928 vs 901, 857.2125 975 vs 951, the weak 854.5125 172 vs 36); and two sites
op25 never identified (771.73125, 771.98125). Simulcast channels need the
CQPSK receiver (Metrocrest 854.7875: 91 vs 428).

## 2026-11-15 — CQPSK receiver, and both receivers on every channel

The CQPSK receiver tracks the carrier (a slow loop on the instantaneous
frequency), applies an RRC matched filter (α 0.2) and takes the phase step
across one symbol: ±45° / ±135°, scaled to Hz so it reads like the C4FM
receiver's averaged discriminator and the same framer takes it. Without the
tracking a radio 600 Hz off wraps ±135° past ±180°; the Airspy in the corpus
is about +800 Hz off at 855 MHz.

Every channel runs both receivers, each with its own framer; a TSBK either
decodes is taken once (same frame time within 15 symbols, same block). On
the corpus that is 115 % of op25's better chain's site broadcasts: far ahead
on weak and C4FM sites (858.5375 1105 vs 553, 859.2125 994 vs 369, 854.5125
384 vs 36), every site op25 identified identified the same, and four more.
op25's CQPSK is still a little ahead on the strongest simulcast channels
(Metrocrest 854.7875 392 vs 428, 857.8375 128 vs 191): an equaliser or soft
decisions are what's left there.

## 2026-11-15 — The NAC is the one heard most; two NIDs are never real

One NID in a noisy stretch can decode to another NAC: a site's NAC is the
majority of its frames'. And a false sync followed by a flat stretch of -3 or
+1 symbols decodes as a perfect NID: NAC FFF / DUID F (all ones) or NAC 000 /
DUID 0 (all zeros). On 852.375 that was 176 frames against 160 real ones.
No transmitter sends either (FFF is a receiver's "any NAC"), so both are
rejected.

## 2026-11-15 — The channelizer: one FFT for every channel

Overlap-save: N-sample blocks overlapping by the prefilter's length (less
one) are transformed once; each channel takes the N/D bins around it,
weighted by the prefilter, and an inverse FFT of N/D points gives it its
samples moved to 0 Hz and decimated by D. A per-block phase correction keeps
the move continuous and a small NCO takes off the rest of the offset. The
prefilter must be nothing past ±(output rate)/2, since a channel keeps only
the bins inside that; otherwise the block edges show. Eleven control
channels of the corpus decode 6.4x faster than real time on one core.

## 2026-11-15 — Detection on the channel raster

P25 channels sit on a raster: 6.25 kHz (2.5 kHz on VHF high band). Over the
look (0.35 s) each ~1 kHz bin's power is taken at its 20th percentile, so a
carrier that dropped out at all reads low. Each raster channel's power (±4
kHz) against the local floor (the median over ±150 kHz) is its level; a
candidate is 4.5 dB or more over the floor, the strongest within two raster
steps, and not within 6 dB of its own level all the way 25 kHz either side
(something wide is flat). An earlier rule ("over the noise for 25 kHz
either side") lost Metrocrest 854.7875 live, among the skirts and a spur of a
45 dB carrier 75 kHz below, though its peak stood 5 dB over all of them. Two
control channels 25 kHz apart (859.7125 and 859.7375) both show, because the
level drops to the floor between them. A threshold this low is cheap:
candidates are decoded together and one that isn't P25 is dropped at 2 s.

## 2026-11-15 — When a candidate is done

Not P25: no frame by 2 s. Not a control channel: frames but no TSDU by 2 s
(a weak control channel whose TSDUs haven't passed a CRC yet stays).
Complete: WACN, system, RFSS, site, a band plan and its own channel heard;
every channel number it named resolved; and nothing new for 3 s (6 s if some
channel number never resolves: a few sites name channels from a table they
never broadcast). At the 12 s cap it is complete if everything heard
resolves, otherwise partial. A channel complete in one hop is skipped by the
next, overlapping one.

## 2026-11-15 — Copies

The corpus has control channels heard where they aren't: the radio's own
image (773.23125 is 771.36875 mirrored about the tuning centre, 772.3) and
intermodulation. A record is a copy if it announces another frequency for
itself, if it has the same NAC, system, RFSS and site as one that decodes
better, or if it is at the mirror image of one at least 10 dB stronger in
the same hop. Copies keep their record, marked, and aren't complete.

## 2026-11-15 — Records in the Python schema

`scan --output` writes NDJSON the Python p25-survey's report and submission
tools read unchanged (checked with its report.py); our additions (verdict,
level, announced frequency, copy, per-receiver counts) are at the top level,
since it builds neighbours, band plan entries and the signal block from
exactly their keys. A hop's records are written when the hop ends: a copy
can't be told from what it copies until both are in.

## 2026-11-15 — Airspy through libairspy, loaded at run time

libairspy (a small C library, packaged everywhere) does the Airspy's USB and
its real-to-I/Q conversion; it is loaded with libloading when an Airspy is
opened, so the program builds and runs without it and needs it only to use
one. Float I/Q at the highest rate up to 6 MS/s; the callback copies each
block into a bounded queue (a full queue drops the block and counts it,
rather than stall USB). A retune empties the queue, and live scans settle
0.25 s for the transfers still in flight. `--gain` is the linearity preset
(14 if not given, as the corpus was recorded), `--sensitivity` the other
presets, `auto` the AGC.

## 2026-11-15 — First live scans (Airspy Mini, linearity 14)

851-869 MHz in 28 s, 769-775 MHz in 25 s, keeping up with the radio on one
core (about a third of it). 800 MHz: 20 control channels, every one the
recordings had (Metrocrest 854.7875 only once the "wide" rule was fixed),
and 859.6125 (BEE00/366 1-1). 700 MHz: 771.36875 is 58 dB here and makes
copies of itself across the band (772.15625, 773.10625 at 30 dB, 773.18125,
774.85), each decoding cleanly with its identity; all are marked copies. At
linearity 9 the copies go but so do the weakest sites (771.98125, 772.19375,
774.68125): the gain sweep is what settles that, per band.

Copy rules learned live: a channel heard a raster step off its true
frequency (859.29375 for 859.2875) takes the frequency it announces, and one
that announces itself is never a copy (851.0875 is the exact mirror image of
855.4125 about 853.25, and is a real site).

## 2026-11-15 — The gain sweep: TSBKs decoded, each channel against its best

`--auto-gain`: after each hop's dwell, its control channels (those that
announce themselves: not copies) are measured together at each gain of the
grid (Airspy linearity 6, 9, 12, 15, 18 by default), 3 s each after the
settle. A gain's score is the mean over the channels of the TSBKs it decoded
as a share of that channel's best gain's, so a strong channel doesn't drown
out a weak one; a band's advice is the lowest gain within 2 % of its best
score (less gain, more headroom). The Python version took the lowest TSBK
error rate per channel (failed CRCs over attempts) and the median per band;
that can't see frames never synced at all, which is what too little gain does
to a weak channel, and what too much does through intermodulation.

Live, 700 MHz (2026-11-15): 6: 71 %, 9: 97 %, 12: 99 %, 15: 80 %, 18: 60 %:
--gain 12. 769.78125 decodes 117 TSBKs in 3 s at 9 and 12, 20 at 15, none at
18; nothing clipped. The sweep adds 5 × 3.25 s to each hop with control
channels.

## 2026-11-15 — RadioReference and the reports

The same four SOAP calls, matching rules and enrichment record (`rr`, field
for field) as the Python version's radioreference.py and enrich.py, so either
reads the other's records: system by WACN/SYSID (several RR systems can share
one), site by RFSS/site number with the NAC to choose between subsystems, the
control channel's frequency against the site's, the NAC, and the neighbours
against the roster. HTTPS through ureq (rustls: no system TLS library), XML
through roxmltree. The login is asked for at the start of every run (the
password without echo) and never stored; answers are cached for the run.

Reports and enrichment work from the records as written (ours or the Python
version's), so `p25-survey report survey.json [--rr]` renders them again, or
looks a survey up after the fact. The text report keeps copies, voice and
conventional P25 and the rest apart from the control channels (the Python
version counted any record with a NAC as a control channel). The submissions
report has the Python version's sections.

The radio's clock is taken from the control channels' carrier offsets as the
CQPSK receiver measured them (decode 50 % or better; median, IQR), not from
RR: base stations are GPS-disciplined, and RR's frequencies are on the
raster, so the Python version's offsets against RR were nearly always 0. Live
(Airspy Mini): +0.81 ppm, IQR 0.21, over 21 control channels on two bands.

## 2026-11-15 — RR lookups beside the scan, not in it

The RR API takes a second or two a call; done between hops, the radio's
queue overflowed and half the air was lost. The lookups run on a thread of
their own, a hop's records at a time; the scan never waits, and at the end
the records are written again with RR's answers before the reports.

A weak control channel without a decoded WACN borrows it for the lookup from
the survey's other control channels with the same System ID, when they agree
on one (noted in `rr.notes`; the record keeps what was decoded). Live, that
put NTIRN's Mansfield (1-5) and Texas Motor Speedway (1-6) sites on the map.
One still unresolved when its hop ends waits for the whole survey.

`--rr-stdin` takes the username and password from standard input instead of
asking (scripts); nothing is stored either way.

Live, 851-869 MHz with --rr (2026-11-15): 19 control channels, every one
matched to its RR site and every frequency listed; to submit: NTIRN Grand
Prairie (1-7) sends NAC 440 where RR has 447, and CTTRS Dallas announces a
neighbour (1-99, 464.55 MHz) RR's roster doesn't have.

## 2026-11-15 — RTL-SDR, HackRF and HydraSDR

Each through its maker's library, loaded when that radio is opened (as
libairspy): librtlsdr, libhackrf, libhydrasdr. A program that needs only
the one in use, and builds with none.

- HydraSDR (RFOne): libhydrasdr grew from libairspy, so the Airspy driver
  takes the library's prefix; set_freq takes a uint64_t (1.0.2 and 1.1.x
  alike). Its rates (10, 5, 2.5 MS/s) aren't a whole number of P25 samples
  per symbol after any decimation, so the channelizer takes the nearest
  within 1 % (10 MS/s / 208: 10.016 a symbol); the framer, timing each
  frame from its sync, doesn't mind.
- RTL-SDR: librtlsdr's async read blocks, so it runs on a thread of ours;
  2.4 MS/s, unsigned 8-bit; `--gain` is the tuner's dB (nearest it has),
  AGC by default.
- HackRF: 9.6 MS/s (a whole number of samples per symbol), signed 8-bit,
  baseband filter at 75 % of the rate; `--gain` is the LNA (0-40 dB, 8 dB
  steps), `--vga` the baseband gain (24 by default), `--amp` the 14 dB
  amplifier.
- RTL-SDR and HackRF leak their LO as a spike at the centre: a one-pole DC
  blocker (a notch a few hertz wide) takes it off, and live tunings are
  half a raster step off a channel, so none sits on what's left.
- Each radio says what `--gain N` means on it and what a sweep tries.

Tested at each one's rate with the corpus resampled (2.4 MS/s quantised to
8 bits, 9.6 and 10 MS/s): the same TSBKs as at 6 MS/s, within one. The
drivers themselves have not run against their radios yet.

## 2026-11-15 — Resume, Ctrl-C, --ppm

`--output` truncates, as the Python version's does; `--resume` keeps the
records there and appends. What they cover isn't surveyed again, except
partial control channels, which get another chance; a channel surveyed again
keeps only its newest record (the file is rewritten at the end, and
`report` reads it the same way). Each hop's records are synced to disk as
it ends, so a crash loses at most the hop under way.

Ctrl-C once ends the hop under way with what each candidate has (partial),
waits for RR, and writes the reports; twice, it stops at once. On the
corpus: stopped after 1.2 s, resumed, and the same 19 control channels as a
run straight through.

`--ppm N` takes the radio's error as the report gives it (+0.83: it reads
0.83 ppm high) and asks the radio for each centre corrected by it. Records
carry it (`sdr_ppm`), and the clock in the reports is the whole error
(correction plus what was still measured), so its advice is the same with
or without one. Live: carrier offsets from about +750 Hz to within ±140 Hz
(the carrier measurement's own noise, about ±0.15 ppm).

## 2026-11-15 — Python 0.3.6–0.4.0, ported

The Python version went on to 0.4.0 after the corpus baseline (0.3.5). All
of it is here, decoded from TIA-102 as the rest:

- MBT: multi-block trunking, the site broadcasts some systems send as a
  data PDU (a header block and data blocks, ½-rate trellis, the data under
  a CRC-32: register from 0, inverted, as every PDU on the corpus passes)
  instead of TSBKs: ADJ_STS_BCST, NET_STS_BCST, RFSS_STS_BCST and
  P_PARM_BCST (an encrypted control channel). CTTRS Dallas sends its VHF
  neighbour (1-40, Childress) only this way.
- SYS_SRV_BCST (the site's services), TIME_DATE_ANN (its UTC offset), the
  LRA, the C/F/V/A status bits of a site and its neighbours (failsoft,
  failure, stale, conventional), and IDEN_UP_VU's channel bandwidth.
- A 1-slot IDEN_UP_TDMA channel is FDMA (`is_tdma` false), as 0.3.9 has it.
- RR: secondary control channels checked against the site's own RR
  frequencies; an expired Premium subscription refused, with the renewal
  link (but 01-01-1970, a PHP timestamp of 0 seen on working accounts, only
  warns).
- The reports: the new fields, flags and mismatches, and the RR line in
  0.4.0's words.
- The 900 MHz SMR band; `--step` taking several grids (the detector looks
  on each); the Airspy at its highest rate (an R2's 10 MS/s).

A neighbour heard both as a TSBK (with its status bits) and as an MBT
(without) is one neighbour: what one form lacks the other keeps, or the
two would alternate as "new" and the channel never go quiet.

## 2026-11-15 — License

GPL-3.0-or-later, as the Python version. That was required there by op25;
here nothing requires it, and it can be revisited before the first release.
