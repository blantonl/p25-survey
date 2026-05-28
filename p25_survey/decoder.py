"""Phase 2 P25 control channel decoder.

Builds a GNU Radio flowgraph that runs the op25 P25 demodulator + frame
assembler against a single candidate frequency, collects TSBK + TDMA
broadcast frames via the message queue, parses them with our tsbk module,
and assembles a SurveyRecord.

Adaptive dwell:
  - confirm_timeout (default 2 s): if no P25 frames arrive, this is not a CC
  - early-exit: as soon as we have WACN + sysid + RFSS + site + a band plan
    and at least one full ADJ_STS_BCST cycle settled, return success
  - max_dwell (default 12 s): hard cap; return whatever we have

GNU Radio + op25 imports are lazy so the package stays importable on hosts
without GNU Radio.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from p25_survey.survey import (
    IdenUpEntry,
    NeighborSite,
    SignalQuality,
    SurveyRecord,
)
from p25_survey.tsbk import (
    AdjStsBcst,
    FreqTable,
    IdenUp,
    NetStsBcst,
    RfssStsBcst,
    Sccb,
    parse_fdma_mbt,
    parse_fdma_tsbk,
    parse_tdma_pdu,
)


# Frame types emitted by op25_repeater.frame_assembler via msg.type():
#   7  = TSBK   (FDMA control channel single-block)
#   12 = MBT    (FDMA multi-block trunking — bcst opcodes parsed; needed for
#               systems like WISCOM De Pere that send ADJ/RFSS/NET bcsts via MBT)
#   18 = TDMA   (Phase 2 broadcast PDU)
#   19 = LCW   (FDMA voice channel link-control word — voice; ignored)
_FRAME_TYPE_TSBK = 7
_FRAME_TYPE_MBT = 12
_FRAME_TYPE_TDMA = 18


@dataclass
class _DwellState:
    """Per-candidate accumulator. The dwell loop owns one of these."""
    nac: int | None = None
    wacn: int | None = None
    sysid: int | None = None
    rfss_id: int | None = None
    site_id: int | None = None
    freq_table: FreqTable = field(default_factory=FreqTable)
    neighbors: dict[int, NeighborSite] = field(default_factory=dict)  # keyed by channel_id
    pending_neighbors: list[AdjStsBcst] = field(default_factory=list)
    # Secondary control channels advertised via SCCB (opcode 0x39). Rare in
    # practice — most sites never transmit SCCB — but useful for re-acquisition
    # on systems that rotate CCs. Keyed by channel_id → resolved freq_hz.
    secondary_cc: dict[int, int] = field(default_factory=dict)
    pending_secondary_cc: set[int] = field(default_factory=set)  # channel_ids
    last_neighbor_ts: float = 0.0
    frame_count: int = 0
    broadcast_count: int = 0   # NET_STS / RFSS_STS / IDEN_UP / ADJ_STS only
    rssi_samples: list[float] = field(default_factory=list)  # dBFS
    tsbk_attempted: int = 0   # cumulative from frame_assembler.control({"cmd":"fec_stats"})
    tsbk_passed: int = 0
    notes: list[str] = field(default_factory=list)

    def has_core_id(self) -> bool:
        return all(v is not None for v in (self.wacn, self.sysid, self.rfss_id, self.site_id))

    def neighbors_settled(self, now: float, dwell_started: float,
                          settle_window_s: float = 2.0,
                          single_site_grace_s: float = 4.0) -> bool:
        """True when we should stop waiting for more ADJ_STS_BCST broadcasts.

        Two ways to settle:
          1. We've seen at least one neighbor and `settle_window_s` has passed
             since the last one (the system has finished its broadcast cycle).
          2. We've seen NO neighbors but the dwell has been alive for at least
             `single_site_grace_s` since the first frame — single-site systems
             don't transmit ADJ_STS_BCST, so absence of neighbors is the
             correct answer for them.
        """
        if self.last_neighbor_ts > 0.0:
            return (now - self.last_neighbor_ts) >= settle_window_s
        # No neighbors seen — accept silence after grace window.
        return (now - dwell_started) >= single_site_grace_s


def _add_vendored_op25_to_path() -> None:
    """Make the vendored op25 helper modules importable as bare names.

    p25_demodulator.py does `import rms_agc` and `import op25_c4fm_mod`
    without a package prefix, so the vendored directory must be on sys.path.
    """
    vendored = Path(__file__).parent / "_vendored" / "op25"
    p = str(vendored)
    if p not in sys.path:
        sys.path.insert(0, p)


# Candidate directories that may contain a boatbod op25_repeater extension.
# We search these when the bare `from gnuradio import op25_repeater` fails,
# which is the common shiv-on-fresh-Linux case — system gnuradio lives in
# /usr/lib while op25 installs under /usr/local/lib (or a user's ~/op25
# build tree), so Python finds gnuradio but not the op25 submodule.
_OP25_SEARCH_GLOBS: tuple[str, ...] = (
    "/usr/local/lib/python*/dist-packages/gnuradio/op25_repeater*.so",
    "/usr/local/lib/python*/site-packages/gnuradio/op25_repeater*.so",
    "/usr/lib/python*/dist-packages/gnuradio/op25_repeater*.so",
    str(Path.home() / "op25/install/lib/python*/dist-packages/gnuradio/op25_repeater*.so"),
    str(Path.home() / "op25/op25/gr-op25_repeater/build/python/op25_repeater*.so"),
)


class Op25NotInstalledError(RuntimeError):
    """Raised when we can't import the boatbod op25_repeater extension.

    The message is composed specifically to help users hit by the shiv
    install pattern: gnuradio is importable but `op25_repeater` lives in
    a different prefix than the running gnuradio package.
    """


def _find_op25_extension() -> Path | None:
    """Search common boatbod install paths for op25_repeater.*.so."""
    import glob  # noqa: PLC0415
    for pattern in _OP25_SEARCH_GLOBS:
        matches = glob.glob(pattern)
        if matches:
            return Path(matches[0]).parent
    return None


def _numpy_major_version() -> int | None:
    try:
        return int(np.__version__.split(".", 1)[0])
    except (AttributeError, ValueError, IndexError):
        return None


def ensure_op25_importable() -> None:
    """Verify the op25 P25 decoder is importable; raise a helpful error if not.

    Strategy:
      1. Try `from gnuradio import op25_repeater` directly. If that works,
         we're done — the user has a sane install.
      2. If it fails, locate `op25_repeater*.so` under known boatbod install
         paths and extend `gnuradio.__path__` to make it visible. Retry.
      3. On persistent failure, raise `Op25NotInstalledError` with a
         message that names the most likely root cause (numpy ABI mismatch
         vs missing/misplaced install).

    Designed to be called once before Phase 2 starts so the failure surfaces
    before any candidate decode, instead of buried in the decode traceback.
    """
    try:
        from gnuradio import op25_repeater  # noqa: F401, PLC0415
        return
    except ImportError:
        pass

    ext_dir = _find_op25_extension()
    if ext_dir is not None:
        try:
            import gnuradio  # noqa: PLC0415
            if str(ext_dir) not in list(gnuradio.__path__):
                gnuradio.__path__.append(str(ext_dir))
            from gnuradio import op25_repeater  # noqa: F401, PLC0415
            return
        except ImportError:
            pass

    # Still broken — pick the most informative diagnostic.
    np_major = _numpy_major_version()
    lines = ["op25_repeater is not importable from your gnuradio install."]
    if np_major is not None and np_major >= 2:
        lines.append(
            f"  Likely cause: numpy {np.__version__} is installed, but boatbod "
            f"op25 is built against the numpy 1.x ABI. Install numpy<2:"
        )
        lines.append("    pip install --break-system-packages 'numpy<2'")
        lines.append("  (or run inside a venv with numpy<2 installed)")
    elif ext_dir is None:
        lines.append(
            "  Likely cause: boatbod op25 isn't installed, or installed to a "
            "prefix Python doesn't search. Install per the project README:"
        )
        lines.append("    git clone https://github.com/boatbod/op25.git && cd op25 && ./install.sh")
    else:
        lines.append(
            f"  Found {ext_dir / 'op25_repeater*.so'} but importing it still "
            f"failed — usually an ABI mismatch (numpy version, Python version, "
            f"or gnuradio version)."
        )
        lines.append("  Try: pip install --break-system-packages 'numpy<2'")
    raise Op25NotInstalledError("\n".join(lines))


def _process_msg(msg, state: _DwellState) -> None:
    """Decode one frame_assembler message and update dwell state."""
    state.frame_count += 1
    payload = msg.to_string()
    duid = ctypes.c_int16(msg.type() & 0xFFFF).value

    # First two bytes are NAC (per tk_p25.process_qmsg).
    if len(payload) < 2:
        return
    nac = (payload[0] << 8) | payload[1]
    if state.nac is None and nac != 0 and nac != 0xFFFF:
        state.nac = nac
    body = payload[2:]

    parsed = None
    if duid == _FRAME_TYPE_TSBK:
        parsed = parse_fdma_tsbk(body)
    elif duid == _FRAME_TYPE_MBT:
        parsed = parse_fdma_mbt(body)
    elif duid == _FRAME_TYPE_TDMA:
        parsed = parse_tdma_pdu(body)
    if parsed is None:
        return

    state.broadcast_count += 1

    if isinstance(parsed, IdenUp):
        state.freq_table.add(parsed)
        # Re-resolve any neighbors that were waiting on this iden table.
        if state.pending_neighbors:
            still_pending = []
            for adj in state.pending_neighbors:
                f = state.freq_table.channel_id_to_frequency(adj.channel_id)
                if f is not None:
                    state.neighbors[adj.channel_id] = NeighborSite(
                        freq_hz=f, rfss_id=adj.rfss_id, site_id=adj.site_id,
                        sysid=adj.sysid, wacn=adj.wacn,
                    )
                else:
                    still_pending.append(adj)
            state.pending_neighbors = still_pending
        if state.pending_secondary_cc:
            still_pending_cc: set[int] = set()
            for cid in state.pending_secondary_cc:
                f = state.freq_table.channel_id_to_frequency(cid)
                if f is not None:
                    state.secondary_cc[cid] = f
                else:
                    still_pending_cc.add(cid)
            state.pending_secondary_cc = still_pending_cc
    elif isinstance(parsed, NetStsBcst):
        state.wacn = parsed.wacn
        state.sysid = parsed.sysid
    elif isinstance(parsed, RfssStsBcst):
        state.rfss_id = parsed.rfss_id
        state.site_id = parsed.site_id
        if state.sysid is None:
            state.sysid = parsed.sysid
    elif isinstance(parsed, Sccb):
        # 0xFFFF is a null channel slot — only one secondary advertised.
        for cid in (parsed.cc1_channel_id, parsed.cc2_channel_id):
            if cid == 0xFFFF or cid in state.secondary_cc or cid in state.pending_secondary_cc:
                continue
            f = state.freq_table.channel_id_to_frequency(cid)
            if f is not None:
                state.secondary_cc[cid] = f
            else:
                state.pending_secondary_cc.add(cid)
    elif isinstance(parsed, AdjStsBcst):
        if parsed.channel_id in state.neighbors:
            return  # already recorded — don't reset settle timer
        # New neighbor only; reset timer so settle window measures
        # "time since LAST NEW neighbor" not "time since any ADJ_STS".
        state.last_neighbor_ts = time.monotonic()
        f = state.freq_table.channel_id_to_frequency(parsed.channel_id)
        if f is not None:
            state.neighbors[parsed.channel_id] = NeighborSite(
                freq_hz=f, rfss_id=parsed.rfss_id, site_id=parsed.site_id,
                sysid=parsed.sysid, wacn=parsed.wacn,
            )
        else:
            state.pending_neighbors.append(parsed)


def _state_to_record(state: _DwellState, freq_hz: int, dwell_ms: int,
                     sdr_driver: str, sdr_gain_db: float | None,
                     sdr_ppm: float, complete: bool) -> SurveyRecord:
    iden_up = [
        IdenUpEntry(
            iden=i.iden,
            base_freq_hz=i.base_freq_hz,
            step_hz=i.step_hz,
            offset_hz=i.offset_hz,
            is_tdma=i.is_tdma,
            slots_per_carrier=i.slots_per_carrier,
        )
        for i in state.freq_table.all_idens()
    ]
    notes = list(state.notes)
    if state.pending_neighbors:
        notes.append(
            f"{len(state.pending_neighbors)} neighbor(s) unresolved: "
            "no IDEN_UP for their channel-id table"
        )
    if state.pending_secondary_cc:
        notes.append(
            f"{len(state.pending_secondary_cc)} secondary CC(s) unresolved: "
            "no IDEN_UP for their channel-id table"
        )
    signal_kwargs: dict[str, float] = {}
    if state.rssi_samples:
        signal_kwargs["rssi_dbfs_mean"] = round(sum(state.rssi_samples) / len(state.rssi_samples), 2)
        signal_kwargs["rssi_dbfs_peak"] = round(max(state.rssi_samples), 2)
    if state.tsbk_attempted > 0:
        # Block-level error rate from CRC failures. A reasonable BER proxy:
        # P25 trellis-coded TSBK blocks fail CRC when post-FEC residual errors
        # remain. Healthy: <5%. Marginal: 5-15%. Lossy: >15%.
        signal_kwargs["ber_pct_mean"] = round(
            100.0 * (state.tsbk_attempted - state.tsbk_passed) / state.tsbk_attempted, 2
        )
        signal_kwargs["decode_rate_pct"] = round(
            100.0 * state.tsbk_passed / state.tsbk_attempted, 2
        )
    signal = SignalQuality(**signal_kwargs)
    return SurveyRecord(
        freq_hz=freq_hz,
        ts=datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
        complete=complete,
        wacn=state.wacn,
        sysid=state.sysid,
        nac=state.nac,
        rfss_id=state.rfss_id,
        site_id=state.site_id,
        neighbors=sorted(state.neighbors.values(), key=lambda n: n.freq_hz),
        secondary_cc=sorted(state.secondary_cc.values()),
        iden_up=iden_up,
        signal=signal,  # BER + decode_rate still null — needs op25 stats plumbing
        dwell_ms=dwell_ms,
        sdr_driver=sdr_driver,
        sdr_gain_db=sdr_gain_db,
        sdr_ppm=sdr_ppm,
        notes=notes,
    )


def decode_candidate(
    freq_hz: int,
    sdr_driver: str,
    device_args: str,
    sample_rate_hz: int,
    gain_db: float | None,
    ppm: float,
    confirm_timeout_s: float = 2.0,
    max_dwell_s: float = 12.0,
    debug: int = 0,
) -> SurveyRecord:
    """Run the op25 P25 decoder against one candidate frequency.

    Returns a SurveyRecord with `complete=True` if we collected a full system
    identity (WACN, sysid, RFSS, site, band plan + neighbors settled), or
    `complete=False` with whatever was captured at the deadline.
    """
    _add_vendored_op25_to_path()

    # Lazy GNU Radio + op25 imports.
    from gnuradio import blocks, gr  # noqa: PLC0415
    import osmosdr  # noqa: PLC0415
    from gnuradio import op25_repeater  # noqa: PLC0415

    import p25_demodulator  # noqa: PLC0415  (vendored)
    from p25_survey._stderr import suppress_c_stderr  # noqa: PLC0415

    rx_q = gr.msg_queue(2048)
    msgq_id = 0

    state = _DwellState()
    complete = False

    # Suppress all C-level stderr for the duration of the decode. op25's
    # C++ blocks emit periodic chatter (IMBE codeword dumps, "two-stage
    # decimator" notices, websocket lifecycle, etc.) on debug>=10 and even
    # at debug=0 they leak some lines. Survey runs don't care about voice
    # decoder internals; suppressing keeps the live table clean. Real
    # GR/osmosdr errors (device disconnect, etc.) are also suppressed —
    # acceptable trade-off for now.
    with suppress_c_stderr():
        tb = gr.top_block()
        src = osmosdr.source(args=device_args)
        src.set_sample_rate(sample_rate_hz)
        src.set_center_freq(int(freq_hz))
        src.set_freq_corr(float(ppm), 0)
        if gain_db is not None:
            src.set_gain_mode(False, 0)
            src.set_gain(float(gain_db), 0)
        else:
            src.set_gain_mode(True, 0)

        demod = p25_demodulator.p25_demod_cb(
            msgq_id=msgq_id,
            debug=debug,
            input_rate=sample_rate_hz,
            demod_type="cqpsk",
            filter_type="rrc",
            usable_bw=int(sample_rate_hz * 0.8),
            excess_bw=0.2,
            relative_freq=0,
            offset=0,
            if_rate=24_000,
            symbol_rate=4_800,
        )
        fa = op25_repeater.frame_assembler("", debug, msgq_id, rx_q)

        # RSSI tap — magnitude-squared of source IQ averaged over ~10 ms,
        # readable via probe.level(). Branched off the source so it doesn't
        # disturb the demod chain.
        rssi_window = max(1024, sample_rate_hz // 100)
        rssi_mag = blocks.complex_to_mag_squared()
        rssi_avg = blocks.moving_average_ff(rssi_window, 1.0 / rssi_window)
        rssi_probe = blocks.probe_signal_f()

        tb.connect(src, demod, fa)
        tb.connect(src, rssi_mag, rssi_avg, rssi_probe)
        tb.start()

        started = time.monotonic()
        deadline = started + max_dwell_s
        confirm_deadline = started + confirm_timeout_s
        next_rssi_sample = started + 0.25  # let AGC + flowgraph settle

        try:
            while True:
                now = time.monotonic()
                if now >= next_rssi_sample:
                    level = float(rssi_probe.level())
                    if level > 0:
                        state.rssi_samples.append(10.0 * math.log10(level))
                    fec = json.loads(fa.control('{"cmd":"fec_stats"}'))["data"]["control"]
                    state.tsbk_attempted = int(fec["tsbk_attempted"])
                    state.tsbk_passed = int(fec["tsbk_crc_passed"])
                    next_rssi_sample = now + 0.1
                if not rx_q.empty_p():
                    msg = rx_q.delete_head()
                    if msg is None:
                        break
                    _process_msg(msg, state)
                else:
                    if state.broadcast_count == 0 and now >= confirm_deadline:
                        break
                    if state.has_core_id() and len(state.freq_table) > 0 and \
                            state.neighbors_settled(now, started):
                        complete = True
                        break
                    if now >= deadline:
                        break
                    time.sleep(0.05)
        finally:
            tb.stop()
            tb.wait()

    dwell_ms = int((time.monotonic() - started) * 1000)
    return _state_to_record(
        state=state,
        freq_hz=freq_hz,
        dwell_ms=dwell_ms,
        sdr_driver=sdr_driver,
        sdr_gain_db=gain_db,
        sdr_ppm=ppm,
        complete=complete,
    )
