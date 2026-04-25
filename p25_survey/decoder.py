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
    parse_fdma_tsbk,
    parse_tdma_pdu,
)


# Frame types emitted by op25_repeater.frame_assembler via msg.type():
#   7  = TSBK   (FDMA control channel single-block)
#   12 = MBT    (FDMA multi-block trunking — not parsed in v1)
#   18 = TDMA   (Phase 2 broadcast PDU)
#   19 = LCW   (FDMA voice channel link-control word — voice; ignored)
_FRAME_TYPE_TSBK = 7
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
    last_neighbor_ts: float = 0.0
    frame_count: int = 0
    broadcast_count: int = 0   # NET_STS / RFSS_STS / IDEN_UP / ADJ_STS only
    rssi_samples: list[float] = field(default_factory=list)  # dBFS
    tsbk_attempted: int = 0   # cumulative from frame_assembler.get_decode_stats()
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
    elif isinstance(parsed, NetStsBcst):
        state.wacn = parsed.wacn
        state.sysid = parsed.sysid
    elif isinstance(parsed, RfssStsBcst):
        state.rfss_id = parsed.rfss_id
        state.site_id = parsed.site_id
        if state.sysid is None:
            state.sysid = parsed.sysid
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
        rssi_window = max(1024, sample_rate_hz // 100)  # ~10 ms of samples
        rssi_mag = blocks.complex_to_mag_squared()
        rssi_avg = blocks.moving_average_ff(rssi_window, 1.0 / rssi_window)
        rssi_probe = blocks.probe_signal_f()

        tb.connect(src, demod, fa)
        tb.connect(src, rssi_mag, rssi_avg, rssi_probe)
        tb.start()

    state = _DwellState()
    started = time.monotonic()
    deadline = started + max_dwell_s
    confirm_deadline = started + confirm_timeout_s
    next_rssi_sample = started + 0.25  # let AGC + flowgraph settle
    complete = False

    try:
        while True:
            now = time.monotonic()
            if now >= next_rssi_sample:
                # probe.level() returns a linear power (mean |IQ|^2). Convert
                # to dBFS using normalized full-scale = 1.0.
                level = float(rssi_probe.level())
                if level > 0:
                    state.rssi_samples.append(10.0 * math.log10(level))
                # Refresh TSBK CRC counters from op25 (cheap call).
                stats = fa.get_decode_stats()
                state.tsbk_attempted = int(stats.tsbk_attempted)
                state.tsbk_passed = int(stats.tsbk_passed)
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
