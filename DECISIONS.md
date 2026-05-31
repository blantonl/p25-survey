# P25 Survey Tool — Decisions

Living record of architectural and scope decisions. Append as new ones land. Each entry: date, decision, rationale, alternatives rejected.

---

## 2026-04-25 — Project goals and scope

**Decision:** Build a CLI tool that scans a frequency range on a single SDR, identifies P25 control channels, and logs system metadata (WACN, System ID, NAC, RFSS/Site, control channel, neighbor sites, signal quality) to JSON + console.

**In scope:**
- P25 Phase 1 control channel detection (FSK/C4FM, 9600 baud, 4800 sym/s)
- P25 Phase 2 control channels (control channel itself remains Phase 1 modulation, so detection is the same)
- US bands: 700 MHz PS, 800 MHz, VHF 150–174, UHF 380–512
- SDR backends: RTL-SDR, Airspy, HackRF (via gr-osmosdr, the same layer op25 uses)

**Out of scope (initially):**
- Voice channel decoding/recording — explicitly skipped per requirements
- DMR / NXDN / SmartNet / OpenSky / TETRA scanning
- Web UI or live dashboard beyond a console table
- Database upload (RadioReference DB submission, etc.)
- Continuous unattended monitoring with stateful re-detection

---

## 2026-04-25 — Language: Python on op25

**Decision:** Pure Python tool that imports op25's existing modules from `op25/gr-op25_repeater/apps/` and links against the built `gr-op25_repeater` GNU Radio module.

**Rationale:** op25 already has the entire P25 stack we need:
- `p25_demodulator.py` — C4FM / CQPSK / FSK4 demodulator chains
- `p25_decoder.py` — frame assembler wrappers
- `tk_p25.py` — TSBK opcode parsing including the four we care about: `0x39` IDEN_UP*, `0x3a` RFSS_STS_BCST, `0x3b` NET_STS_BCST, `0x3c` ADJ_STS_BCST, plus `channel_id_to_string()` for translating channel-ID/number → frequency via decoded band plan
- `gr-op25_repeater` C++ blocks expose RSSI and decode-rate metrics

Reimplementing this in C++ on trunk-recorder would duplicate hundreds of files. trunk-recorder is also architected around long-running monitoring of *known* systems; blind scanning is a poor fit.

**Rejected:** C++ on trunk-recorder libs; hybrid Python-orchestrator-spawning-op25-processes (subprocess overhead per candidate not worth it).

**License consequence:** op25 is GPLv3 → this tool will be GPLv3.

---

## 2026-04-25 — Scan strategy: FFT energy scan, then targeted decode

**Decision:** Two-phase scanner.

**Phase 1 (energy scan):** Tune the SDR across the user's start–stop range in chunks of `~0.8 × sample_rate` (usable bandwidth), capture N samples per chunk, FFT, and find bins with power > noise_floor + threshold. Snap each peak to the nearest tuning-step grid. Output: candidate frequency list.

**Phase 2 (decode):** For each candidate, tune narrowband, run the op25 P25 demodulator, attempt to decode TSBKs. Confirm CC (Phase 1 NAC sync + at least one valid TSBK), then dwell to collect band plan + neighbors.

**Rationale:** Pure stepwise tune-and-decode at e.g. 12.5 kHz across 19 MHz of 800 MHz band with 3 s dwell ≈ 75 minutes of scanning, most of it dwelling on empty channels. FFT energy scan compresses Phase 1 to seconds.

**Trade-off accepted:** A weak CC below the energy threshold could be missed. Mitigations: configurable `--threshold` (dB above noise floor), and a `--thorough` flag for stepwise fallback (deferred — implement only if needed).

---

## 2026-04-25 — Dwell strategy: adaptive

**Decision:** Per-candidate dwell logic:

1. Hard-fail if no P25 frame sync within `confirm_timeout` (default 2s) → not a CC, move on.
2. Once confirmed, dwell until we have collected:
   - At least one IDEN_UP* TSBK (band plan, needed to translate neighbor channel IDs to frequencies)
   - One NET_STS_BCST (WACN + SYSID)
   - One RFSS_STS_BCST (RFSS ID + Site ID)
   - At least one ADJ_STS_BCST cycle (neighbors); accept "no neighbors" as a valid result if the system is single-site
3. Cap total dwell at `max_dwell` (default 12s) — log whatever was collected and move on.

**Rationale:** TSBC duty cycle on a healthy CC repeats these broadcasts every few seconds. Adaptive saves time on quiet single-site systems and gives more time on busy ones. Predictable upper bound preserves total scan time guarantees.

---

## 2026-04-25 — Tuning step defaults (US)

**Decision:** Auto-select default step from the start frequency's band; CLI `--step` overrides.

| Band | Range | Default step | Rationale |
|---|---|---|---|
| VHF | 150–174 MHz | 7.5 kHz | Post-narrowbanding raster |
| UHF | 380–512 MHz | 12.5 kHz | Standard narrowband |
| 700 MHz PS | 763–775 / 793–805 MHz | 6.25 kHz | FCC narrowband segment is 1920× 6.25 kHz |
| 800 MHz | 851–869 MHz | 12.5 kHz | Rebanded raster is 6.25 but deployed P25 CCs sit on 12.5 kHz boundaries |
| (any other) | — | 12.5 kHz | Conservative default |

The step is used to:
1. Snap FFT-detected peaks to the nearest grid point before decode.
2. Drive the fallback stepwise mode if `--thorough` is enabled.

---

## 2026-04-25 — Output format: JSON survey + live console table

**Decision:** Two outputs.

**JSON survey file** (`--output survey.json`, default `survey-YYYYMMDD-HHMMSS.json`):
- Append-only newline-delimited JSON (NDJSON), one record per detected control channel.
- Resumable: if the file exists, skip frequencies already characterized unless `--force-rescan`.
- Schema documented in `docs/schema.md`.

**Live console** (rich/curses-style):
- Top section: scan progress (current freq, % complete, candidates remaining).
- Bottom section: rolling table of detected CCs with key fields.
- Falls back to plain stdout lines if not a TTY.

**Rejected:** CSV (lossy on neighbor lists), RR-import format (not requested, can be added later as a converter).

---

## 2026-04-25 — Project location and structure

**Decision:** Standalone directory at `~/dev/bcfy-clients/p25-survey/`.

**Rationale:** Keeps the survey tool's git history clean and separate from op25 forks; we don't want to upstream this into op25.

---

## 2026-04-25 — op25 linkage: vendored Python, system gr-op25_repeater

**Decision:** Vendor the small set of op25 Python files we depend on into `p25_survey/_vendored/op25/`. The C++ GNU Radio module (`gr-op25_repeater`, providing `op25_repeater.frame_assembler`, `op25.fsk4_demod_ff`, etc.) must still be built and installed system-wide on the host — that's a normal op25 build requirement and not something we can vendor.

**Files to vendor:**
- `p25_demodulator.py`
- `p25_decoder.py`
- `helper_funcs.py`
- `log_ts.py`
- (TSBK bit layouts are *referenced* from `tk_p25.py` but reimplemented locally in `p25_survey/tsbk.py`; we don't vendor `tk_p25.py` itself)

**Rationale:** Self-contained import surface; the survey tool doesn't break when the sibling `op25/` checkout moves, gets rebased, or is missing. Trade-off: when op25 evolves these files we must re-vendor manually. Cost is low because the vendored files are small and stable.

**Provenance tracking:** Each vendored file gets a header comment with the upstream commit SHA + path so re-vendoring is mechanical. A `_vendored/REVENDOR.md` documents the procedure.

---

## 2026-04-25 — Phase 2 TDMA broadcasts: include parsing

**Decision:** `tsbk.py` will handle both FDMA TSBK opcodes (`0x39` IDEN_UP, `0x3a` RFSS_STS, `0x3b` NET_STS, `0x3c` ADJ_STS) and Phase 2 TDMA MAC PDU broadcast opcodes (`0xfa` rfss_sts_bcst, `0xfb` net_sts_bcst, `0xfe` adj_sts_bcst).

**Caveat:** v1 only locks on control channels, which are always Phase 1 FDMA even on Phase 2 systems. TDMA broadcasts arrive on *voice* channel slots and we skip voice channels. So in v1 the TDMA handlers exist but normally won't fire. They're in place for future expansion (e.g., a "characterize voice channels" mode) without requiring a parser revisit.

**Rationale:** Cost of including the handlers now is small (bit layouts are already in `tk_p25.py` lines 1288–1355); cost of adding them later if the design expands is a separate refactor. Better to have them.

---

## 2026-04-25 — Output: add plain-text summary report

**Decision:** In addition to the NDJSON survey file and live console table, write a human-readable `.txt` summary on scan completion. Default name mirrors the JSON (`survey-YYYYMMDD-HHMMSS.txt`).

**Format:** One section per detected control channel, with key fields, a neighbor table, and a signal-quality summary. Designed to be readable in a terminal or pasted into a forum thread.

**Implementation:** A small formatter that reads the completed NDJSON file. Generated at the end of the run, not append-as-you-go (avoids the rewrite-the-whole-file-on-every-update tax).

---

## 2026-04-25 — Dev/test environment

**Decision:** Code is authored on this Mac, but anything touching gr-osmosdr, gr-op25_repeater, or live SDR hardware runs on a separate Linux box where op25 is already built and operational.

**Workflow:**
- Lint, type-check, pure-Python unit tests (band tables, TSBK byte parsing, peak detection on synthetic IQ): run on Mac.
- Decoder integration tests, energy scan against live SDR, end-to-end survey runs: run on the Linux op25 host.
- Deployment mechanism (rsync, git, pip install) — TBD; user will provide host details when we get to that stage.

**Rationale:** macOS GNU Radio via Homebrew is feasible but flaky for op25; the existing Linux op25 host is the source of truth for "does this actually work."

---

## 2026-04-25 — Packaging: single-file executable via shiv

**Decision:** Build artifact is a single executable file `p25-survey` produced by [shiv](https://shiv.readthedocs.io/). Run with `./p25-survey --start 851 --stop 870 ...`. No `pip install` required to use the tool; only `make` + Python.

**Rationale:**
- `gr-op25_repeater` is a C++ GNU Radio module loaded via Python bindings. It *must* come from the host's GNU Radio install — no Python packaging tool can bundle it portably. Whatever we pick has to accept this constraint.
- Among options that respect that constraint:
  - **shiv** → single `.pyz` zipapp with shebang. Bundles all pure-Python deps (numpy, scipy, rich, our package, vendored op25 files). Fast to build. Idiomatic Python. **Chosen.**
  - **PyInstaller --onefile** → larger (~100 MB), extracts to /tmp at runtime, slower to build, no benefit over shiv given the GR constraint.
  - **Nuitka --onefile** → truly compiles Python; slow build; doesn't help because GR-bound code still loads system shared libs at runtime.
  - **shell-style single .py with vendored code inline** → painful with multiple vendored op25 files; loses package structure for tests.

**Build:** `make` runs `shiv -c p25-survey -o p25-survey -e p25_survey.cli:main .`. Output is one executable file. `make clean` removes it.

**Distribution model:** Copy `p25-survey` to the target host, `chmod +x`, run. Host must have Python 3.10+, GNU Radio, and `gr-op25_repeater` installed (i.e., any host where `multi_rx.py` already runs).

---

## 2026-04-25 — Don't bundle numpy/scipy (ABI conflict with GNU Radio)

**Decision:** The shiv build uses `--no-deps`. numpy and scipy are imported from the host's site-packages, not bundled.

**Why:** Bundling numpy in the shiv archive caused a load failure when `gnuradio.blocks` imported on dragon1: `ImportError: numpy.core.multiarray failed to import`. GNU Radio's compiled C extensions are linked against the system numpy ABI; when shiv's bundled numpy is added to `sys.path` first, the GR extension binds against the bundled numpy and fails.

**How to apply:** Any host that runs GNU Radio already has numpy + scipy installed (they're hard transitive deps of `python3-gnuradio`). The shiv binary is now ~40 KB (vs 26 MB when numpy/scipy were bundled) and requires only:
- Python 3.10+
- GNU Radio + gr-osmosdr (brings numpy/scipy)
- gr-op25_repeater (for Phase 2, not yet wired)

For local dev, `[project.optional-dependencies] dev` still pulls them in via `pip install -e '.[dev]'`.

---

## 2026-04-25 — op25 patch: guard `op25_audio` destructor

**Status: upstreamed 2026-04-27** in [boatbod/op25 PR #271](https://github.com/boatbod/op25/pull/271). Stock `boatbod/op25` `dev` carries the fix; we no longer need a private patch.

**Decision (historical):** We carried a one-line patch to op25's `op25_audio.cc` (`ws_stop` skips when no websocket was started). It was required for any frame_assembler use that doesn't enable the websocket — the upstream destructor unconditionally `ws_thread.join()`s a default-constructed thread, which threw and aborted the process.

**Why:** Survey runs don't need the websocket. Passing `""` as the destination crashed on shutdown without the fix. Multi_rx.py masks the bug by always using a destination, which is why it lay dormant in upstream.

---

## 2026-04-25 — op25 patch: expose decode statistics

**Status: superseded 2026-04-30** by the JSON-`control()` interface (see the 2026-04-28 entry below). The struct + pybind11 binding described here was rejected upstream in favor of the `control()` JSON channel; the counter increments and accessors were kept and now live behind `{"cmd":"fec_stats"}` in `boatbod/op25` `dev`.

**Decision (historical):** Patch op25 to expose `frame_assembler.get_decode_stats()` returning a struct with cumulative TSBK/PDU CRC pass/fail counts and timeout count. Survey decoder polls it during the dwell loop to compute BER and decode rate.

**Why:** op25 silently drops TSBKs that fail CRC inside `process_TSBK`/`process_PDU`. From the message queue we only see *successful* frames, so we can't measure link quality without instrumenting the FEC path.

**Files touched:**
- `include/gnuradio/op25_repeater/frame_assembler.h` — added `op25_decode_stats` POD struct + `virtual op25_decode_stats get_decode_stats()` on `frame_assembler`.
- `lib/p25p1_fdma.h` / `.cc` — `d_stat_tsbk_attempted/passed`, `d_stat_pdu_attempted/passed`, `d_stat_timeouts` counters; const accessors. Increments happen at the right points: `tsbk_attempted++` on every `process_TSBK` call; `tsbk_passed++` after first-block CRC passes (count once per TSBK); same pattern for PDU; `timeouts++` in `check_timeout`.
- `lib/rx_base.h` — virtual `get_decode_stats()` returning empty struct (default for non-P25 sync types).
- `lib/rx_sync.h` — override delegating to `p25fdma`.
- `lib/frame_assembler_impl.h` — override delegating to `d_sync->get_decode_stats()`.
- `python/op25_repeater/bindings/frame_assembler_python.cc` — pybind11 binding for the struct (read-only properties) + the new method.

**How we use it:** Decoder polls `fa.get_decode_stats()` every 100 ms during the dwell. At the end:
- `ber_pct_mean = (attempted - passed) / attempted * 100` — block-level error rate, BER-equivalent for survey purposes
- `decode_rate_pct = passed / attempted * 100`

**Validated live on dragon1:** Adjacent CCs at -28 dBFS show BER of 0% vs 29% — the metric correctly captures link quality independent of raw RSSI (e.g., multipath, off-axis antenna, etc. that lower decode quality without lowering received power).

---

## 2026-04-25 — `--threshold` is a margin above noise floor (not below)

**Decision:** The `--threshold N` flag is **dB above the chunk's median PSD**, not
below it. A bin is a candidate when `psd_db > median(psd_db) + threshold_db`.

**Why this convention:** "How much above noise" is the natural language for
spectral-peak detection. Lower margin = more sensitive (catches weaker
signals); higher margin = pickier (only strong signals).

**Calibration guide:**
- 6–10 dB: sensitive, more false positives, useful for weak/distant CCs (rural VHF).
- 12–16 dB: balanced; the default of 8 leans sensitive.
- 18+ dB: only strong signals; fast scan; might miss weak CCs.

A low threshold doesn't hurt final-report correctness — Phase 2's
`--confirm-timeout` filters non-P25 noise at the decode step. The cost of a
low threshold is **time** spent abandoning no-cc candidates.

---

## 2026-04-25 — `--rr` integration uses our own minimal SOAP client

**Decision:** Hand-rolled SOAP client in `radioreference.py` using `urllib`
+ `xml.etree.ElementTree`. No `zeep`/`suds` dependency. Operations:
`getUserData`, `getTrsBySysid`, `getTrsDetails`, `getTrsSites`.

**Why:** Adding `zeep` would mean another bundled-vs-host-installed
decision, and we only need 4 operations. RR returns SOAP-ENC arrays where
members are `<item xsi:type="...">` elements, not the type-named elements
the WSDL implies — our parser detects items by content (presence of
expected child fields) rather than by tag name. More robust to RR's actual
wire format.

**Auth:** Username + password prompted at startup (`getpass.getpass` for
the password). **Never stored on disk.** App-level key bundled in
`_BUNDLED_APP_KEY`; can be overridden with `P25_SURVEY_APPKEY` env var.
In-memory cache scoped to the scan run.

---

## 2026-04-25 — `--auto-gain`: BER as the optimization signal

**Decision:** After Phase 2 finishes, optionally sweep 5 gain values × 4 s
dwell on each confirmed CC. Pick the gain that minimizes per-channel block
error rate (BER as proxy). Aggregate to a per-band median recommendation.

**Why BER and not RSSI:** Strong RSSI doesn't mean clean decode. A too-hot
front end has high RSSI and bad BER (compression / IMD). BER is the only
metric that finds the actual decoder sweet spot.

**Default sweep grid per driver:**
- Airspy: `[4, 8, 12, 16, 20]` (linearity 0–21)
- RTL-SDR: `[10, 20, 30, 40, 49.6]` (tuner gain table)
- HackRF: `[12, 24, 36, 48, 60]` (IF/VGA 0–62)

User can override with `--gain-sweep "4,8,12,16,20"`.

**Tie-breaking** in `pick_best_gain()`: lowest BER wins; ties go to highest
decode_rate, then to lower gain (less front-end strain on stronger signal
days).

**Rescan prompt:** After the sweep, the tool offers to re-run with the
recommended gain (default Yes; new output filename to preserve the original
survey). The rescan disables `--auto-gain` to prevent recommend→rescan
loops. If stdin isn't a TTY, no prompt.

---

## 2026-04-25 — `--output` truncates by default

**Decision:** Without `--resume`, an existing `--output` file is truncated
at scan startup. With `--resume`, it's preserved and we skip already-done
frequencies.

**Why:** Append-only-by-default caused real confusion in testing — running
the same scan twice silently concatenated results, and the report rendered
both stale and fresh records side by side. CLI convention is "truncate by
default, --append/--resume to keep". We match that.

Per-record fsync still applies during the run, so crash safety is preserved
either way.

---

## 2026-04-28 — op25 FEC stats: migrate from custom binding to JSON `control()`

**Decision:** Retire our custom `frame_assembler.get_decode_stats()` pybind11 binding (added 2026-04-25 above) in favor of a JSON-`control()` interface in boatbod's op25 fork. Replace the direct `fa.get_decode_stats()` call with `json.loads(fa.control('{"cmd":"fec_stats"}'))` and parse the agreed JSON envelope.

**Why:** Boatbod's feedback on our FEC-stats PR was that pybind11 surface area is fragile and compiler-version-sensitive across his fork's user base. He proposed consolidating per-feature C++ glue behind a single `virtual std::string control(const std::string& args)` method that takes/returns JSON. We agreed to:
1. Drop the pybind11 additions from our PR.
2. Land a base-class signature change first (`void control(const std::string&)` → `std::string control(const std::string&)`, mechanical fork-wide refactor).
3. Re-cut the FEC-stats PR on top, dispatching `{"cmd":"fec_stats"}` through `control()` and returning a JSON envelope.

The maintainer chose option 2 (return-by-value) over option 1 (out-param) — RVO + move semantics make it zero-cost, and the binding-side handling is symmetric.

**Agreed JSON envelope shape:**
```json
{
  "cmd": "fec_stats",
  "schema": 1,
  "data": {
    "voice":   {"frames_total": ..., "golay_corrected": ..., "rs_corrected": ..., "rs_unrecoverable": ...},
    "control": {"tsbk_attempted": ..., "tsbk_crc_passed": ..., "trellis_corrected": ...},
    "sync":    {"losses": ..., "acquisitions": ...}
  }
}
```

Two design choices that matter:
- **Raw counters, not pre-computed rates.** Op25 doesn't take a position on smoothing windows; consumers compute BER themselves. Lets each op25 release evolve without redefining "BER".
- **`schema` field in the envelope.** Lets us branch on schema version for forward compatibility instead of sniffing op25 build strings (boatbod doesn't tag releases reliably).

**Status (shipped 2026-04-30):** Both PRs merged into boatbod/op25 `dev`:
- [PR #273](https://github.com/boatbod/op25/pull/273) — `control()` signature widened to `std::string` return.
- [PR #272](https://github.com/boatbod/op25/pull/272) — `fec_stats` command implemented; counters in `p25p1_fdma`, JSON formatting in `rx_sync.cc`.

What we ship today (control section + sync.losses): `tsbk_attempted`, `tsbk_crc_passed`, `pdu_attempted`, `pdu_crc_passed`, `losses`. The `voice`, `trellis_corrected`, and `sync.acquisitions` slots are reserved in the schema for future PRs but not populated — would need new instrumentation in the voice decoder and sync state machine. Forward-compatible additions under `schema: 1`; consumers should treat missing fields as null.

**This repo** (`v0.3.0`):
- `decoder.py` swapped to `json.loads(fa.control('{"cmd":"fec_stats"}'))["data"]["control"]`. Local `SurveyState` field names (`tsbk_attempted` / `tsbk_passed`) preserved; envelope's `tsbk_crc_passed` mapped to `tsbk_passed` at the boundary so the public `SignalQuality` schema doesn't change.
- No try/except fallback to the old `get_decode_stats()` API. We control deploys and the patches branch was always meant to be transitional — clean cutover at v0.3.0 keeps the code simple.
- README install recipe now points at stock `boatbod/op25` `dev`. The `p25-survey-patches` branch is no longer maintained.

**Follow-up (not yet done):** The voice-FEC counters in the schema (`golay_corrected`, `rs_unrecoverable`) would give us real post-FEC residual error data. Today's `ber_pct_mean` is a CRC-pass-rate proxy on TSBK blocks (control-channel only). When upstream ships voice instrumentation, we can compute proper symbol-level BER from voice frames and surface "voice FEC marginal" as a distinct flag, keeping the CRC proxy as a fallback for control-only dwells with no voice traffic.

---

## 2026-05-31 — parse the full broadcast catalog ourselves, including FDMA MBT (v0.4.0)

**Decision:** Parse the survey-relevant P25 trunking broadcasts from their raw
bytes in `tsbk.py` rather than relying on what op25 chooses to surface, and
decode them from FDMA Multi-Block Trunking (MBT) PDUs as well as single-block
TSBKs.

**What this added:**
- **FDMA MBT decode** for RFSS_STS_BCST (0x3a), NET_STS_BCST (0x3b),
  ADJ_STS_BCST (0x3c), and P_PARM_BCST (0x3e). Bit layouts ported from boatbod
  op25's `decode_mbt_data`. Some VHF/UHF systems advertise neighbors and
  secondary CCs via MBT instead of TSBK; without this they showed zero
  neighbors.
- **Expanded TIA-102.AABC-B catalog** op25 doesn't decode: SYS_SRV_BCST (0x38)
  services-available/supported bitmaps + request priority (mapped to service
  names); LRA on RFSS/NET/ADJ_STS; TIME_DATE_ANN (0x35) signed UTC offset only
  (date/time dropped as transient); P_PARM_BCST (0x3e) → CC encrypted, algid
  read (key id / message indicator left undecoded).
- **Site & neighbor status flags** surfaced in the text report and RR
  submission markdown: RFSS_STS_BCST "A" bit (false ⇒ failsoft site);
  ADJ_STS_BCST C/F/V/A flags (conventional / failure / stale / network-active);
  IDEN_UP_VU receiver bandwidth (6.25 / 12.5 kHz) distinct from channel spacing.

**FDMA vs TDMA classification:** A channel is TDMA only with ≥2 slots/carrier
(`is_tdma = slots_per_carrier > 1`). Channel types 0/1/2 in IDEN_UP_TDMA (0x33)
describe FDMA channels; systems that advertise an all-FDMA band plan via 0x33
no longer render as "TDMA x1".

**Why parse it ourselves:** op25 emits only the broadcasts it acts on. The data
that's valuable for cataloging a system (services, encryption, failsoft state,
neighbor liveness) is dropped before it reaches the message queue, so we decode
the raw bytes. The MBT layouts and bit offsets are direct ports — kept in lockstep
with `tk_p25.py` via header comments so re-vendoring stays mechanical.

**Compatibility:** All new survey-record fields default to None/empty and
round-trip through the survey JSON; older survey files load unchanged.

---

## 2026-05-31 — accept RR's MM-DD-YYYY premium-expiry format (v0.4.0)

**Decision:** `_parse_expire_date` accepts both ISO `YYYY-MM-DD` and the
`MM-DD-YYYY` form RadioReference's getUserData actually returns (PHP
`date("m-d-Y")`).

**Why:** The startup `--rr` guard only matched ISO, so an expired subscription
parsed as `None` and slipped through. Every premium lookup then AUTH-faulted and
documented sites got mislabeled "NEW SYS (not in RR)". Expired accounts now hit
the renew-your-subscription refusal up front. (See also the auto-memory note on
getUserData returning MM-DD-YYYY.)

---

## 2026-05-31 — `--step` accepts a list of tuning grids (v0.4.0)

**Decision:** `--step` takes a comma-separated list of grids
(e.g. `5,6.25,7.5,12.5`). One PSD per chunk; each detected peak snaps to
whichever listed grid lands closest.

**Why:** VHF/UHF bands mix channel spacings, so a single grid misses CCs on the
other raster. Snapping per-peak against multiple grids catches them all without
changing Phase 1 capture cost (still one FFT per chunk).

---

## Open questions / deferred decisions

These will be resolved as implementation forces the issue. Listed here so we don't forget:

1. **SDR auto-detection** — should the tool autoprobe attached devices, or always require `--sdr rtlsdr|airspy|hackrf`? Leaning toward explicit + autoprobe as a courtesy if `--sdr` omitted.
2. **Calibration / PPM correction** — accept `--ppm` from CLI; no auto-cal in v1.
3. **Multiple SDRs in parallel** — deferred to v2.
4. **Continuous mode** — `--continuous` to loop forever and update existing entries; deferred to v2.
5. **Geo enrichment** — knowing site coordinates means cross-referencing RR or FCC ULS; deferred.
6. **Linux op25 host details** — ssh hostname, deploy path, op25 install location, expected SDR hardware. User will provide when implementation reaches the integration milestone.
