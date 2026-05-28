"""Pure-Python tests for decoder._process_msg state machine.

Avoids the GNU Radio flowgraph by constructing op25 frame_assembler-style
byte payloads directly and feeding them through _process_msg via a stub
message object.
"""

from __future__ import annotations

import sys
import types

import pytest

from p25_survey.decoder import (
    Op25NotInstalledError,
    _DwellState,
    _FRAME_TYPE_MBT,
    _FRAME_TYPE_TSBK,
    _process_msg,
    ensure_op25_importable,
)
from tests.test_tsbk import (
    pack_iden_up_legacy,
    pack_iden_up_vu,
    pack_mbt_adj_sts,
    pack_sccb,
)


class _StubMsg:
    """Mimics the gr.msg.message API: type() + to_string()."""

    def __init__(self, type_: int, payload: bytes) -> None:
        self._type = type_
        self._payload = payload

    def type(self) -> int:
        return self._type

    def to_string(self) -> bytes:
        return self._payload


def _tsbk_msg(tsbk96: int, nac: int = 0x293) -> _StubMsg:
    """Frame_assembler TSBK payload: NAC (2 bytes) + 80-bit wire TSBK."""
    wire = (tsbk96 >> 16).to_bytes(10, "big")
    return _StubMsg(_FRAME_TYPE_TSBK, nac.to_bytes(2, "big") + wire)


def _mbt_adj_msg(syid: int, rfid: int, stid: int, ch1: int, nac: int = 0x293) -> _StubMsg:
    """Frame_assembler MBT (m_type=12) payload for ADJ_STS_BCST.

    Layout matches op25 tk_p25.decode_msg's m_type==12 path:
      NAC (2 bytes) + 10-byte header + 2 CRC bytes + 8-byte data block.
    """
    header_int = 0
    header_int |= 0x17 << 72              # fmt=Extended
    header_int |= 0x3C << 16              # opcode=ADJ_STS_BCST
    header_int |= (syid & 0xFFF) << 32    # original bits 32..43 → shifted 48..59
    header_int |= (rfid & 0xFF) << 8      # original bits 8..15 → shifted 24..31
    header_int |= (stid & 0xFF) << 0      # original bits 0..7 → shifted 16..23
    header_bytes = header_int.to_bytes(10, "big")
    data_int = (ch1 & 0xFFFF) << 48       # original bits 48..63 → shifted 80..95
    data_bytes = data_int.to_bytes(8, "big")
    body = header_bytes + b"\x00\x00" + data_bytes
    return _StubMsg(_FRAME_TYPE_MBT, nac.to_bytes(2, "big") + body)


class TestSccbProcessing:
    def test_resolves_both_channels_when_iden_known(self):
        state = _DwellState()
        # IDEN_UP first so the band plan is available.
        iden_tsbk = pack_iden_up_legacy(
            iden=0, freq_5hz=170_201_250, spac=50, toff_signed=-180
        )
        _process_msg(_tsbk_msg(iden_tsbk), state)

        # SCCB with two distinct secondary CC channel-ids on table 0.
        sccb_tsbk = pack_sccb(rfid=1, stid=7, ch1=0x0010, ch2=0x0020)
        _process_msg(_tsbk_msg(sccb_tsbk), state)

        # Channel 0x10 = 16, step 6.25 kHz → base + 16*6250
        expected_1 = 851_006_250 + 16 * 6_250
        expected_2 = 851_006_250 + 32 * 6_250
        assert state.secondary_cc == {0x0010: expected_1, 0x0020: expected_2}
        assert state.pending_secondary_cc == set()

    def test_pending_resolves_when_iden_arrives_later(self):
        state = _DwellState()
        # SCCB arrives first — channel-id table 0 not in freq table yet.
        sccb_tsbk = pack_sccb(rfid=1, stid=7, ch1=0x0010, ch2=0x0020)
        _process_msg(_tsbk_msg(sccb_tsbk), state)
        assert state.secondary_cc == {}
        assert state.pending_secondary_cc == {0x0010, 0x0020}

        # IDEN_UP arrives — both pending entries should resolve.
        iden_tsbk = pack_iden_up_legacy(
            iden=0, freq_5hz=170_201_250, spac=50, toff_signed=-180
        )
        _process_msg(_tsbk_msg(iden_tsbk), state)
        assert state.pending_secondary_cc == set()
        assert set(state.secondary_cc.keys()) == {0x0010, 0x0020}

    def test_null_channel_0xffff_ignored(self):
        state = _DwellState()
        iden_tsbk = pack_iden_up_legacy(
            iden=0, freq_5hz=170_201_250, spac=50, toff_signed=-180
        )
        _process_msg(_tsbk_msg(iden_tsbk), state)
        # Only one secondary advertised; cc2 = 0xFFFF means "unused".
        sccb_tsbk = pack_sccb(rfid=1, stid=7, ch1=0x0010, ch2=0xFFFF)
        _process_msg(_tsbk_msg(sccb_tsbk), state)
        assert list(state.secondary_cc.keys()) == [0x0010]

    def test_duplicate_sccb_dedupes(self):
        state = _DwellState()
        iden_tsbk = pack_iden_up_legacy(
            iden=0, freq_5hz=170_201_250, spac=50, toff_signed=-180
        )
        _process_msg(_tsbk_msg(iden_tsbk), state)
        sccb_tsbk = pack_sccb(rfid=1, stid=7, ch1=0x0010, ch2=0x0020)
        _process_msg(_tsbk_msg(sccb_tsbk), state)
        _process_msg(_tsbk_msg(sccb_tsbk), state)  # repeat
        assert len(state.secondary_cc) == 2


class TestEnsureOp25Importable:
    """Cover the diagnostic paths in ensure_op25_importable().

    The success path needs a real op25 install; we can't reasonably mock
    that without re-implementing half of gnuradio. So we exercise the
    failure paths — which are the user-facing ones anyway.
    """

    def _stub_gnuradio_without_op25(self, monkeypatch):
        """Install a fake `gnuradio` package whose op25_repeater is missing."""
        gr_mod = types.ModuleType("gnuradio")
        gr_mod.__path__ = []  # namespace-package-style; no submodules
        monkeypatch.setitem(sys.modules, "gnuradio", gr_mod)
        # Prevent the real `from gnuradio import op25_repeater` from succeeding
        # by ensuring the submodule isn't cached.
        sys.modules.pop("gnuradio.op25_repeater", None)
        return gr_mod

    def test_raises_when_op25_missing_and_no_install_found(self, monkeypatch):
        self._stub_gnuradio_without_op25(monkeypatch)
        # Point the search globs at a path that won't exist.
        monkeypatch.setattr(
            "p25_survey.decoder._OP25_SEARCH_GLOBS",
            ("/nonexistent/path/op25_repeater*.so",),
        )
        with pytest.raises(Op25NotInstalledError) as info:
            ensure_op25_importable()
        msg = str(info.value)
        # The error should be actionable: name the missing piece and
        # suggest something concrete.
        assert "op25_repeater" in msg

    def test_numpy_v2_diagnosis_mentions_break_system_packages(self, monkeypatch):
        """When numpy>=2 is detected, the error should mention the workaround."""
        self._stub_gnuradio_without_op25(monkeypatch)
        monkeypatch.setattr(
            "p25_survey.decoder._OP25_SEARCH_GLOBS",
            ("/nonexistent/path/op25_repeater*.so",),
        )
        monkeypatch.setattr("p25_survey.decoder._numpy_major_version", lambda: 2)
        with pytest.raises(Op25NotInstalledError) as info:
            ensure_op25_importable()
        msg = str(info.value)
        assert "numpy" in msg.lower()
        assert "numpy<2" in msg or "'numpy<2'" in msg

    def test_missing_install_diagnosis_mentions_boatbod(self, monkeypatch):
        """Numpy 1.x + no install found → point user at boatbod install."""
        self._stub_gnuradio_without_op25(monkeypatch)
        monkeypatch.setattr(
            "p25_survey.decoder._OP25_SEARCH_GLOBS",
            ("/nonexistent/path/op25_repeater*.so",),
        )
        monkeypatch.setattr("p25_survey.decoder._numpy_major_version", lambda: 1)
        with pytest.raises(Op25NotInstalledError) as info:
            ensure_op25_importable()
        msg = str(info.value)
        assert "boatbod" in msg.lower() or "install.sh" in msg


class TestMbtNeighborProcessing:
    """End-to-end: MBT-encoded ADJ_STS_BCST flows through _process_msg the
    same way TSBK-encoded ADJ_STS_BCST does. This covers the WISCOM De Pere
    regression — neighbors broadcast via MBT used to be dropped on the
    floor, leaving the live table empty.
    """

    def test_mbt_adj_sts_resolves_neighbor_after_iden(self):
        state = _DwellState()
        # IDEN_UP first (VHF band): base 150.815 MHz, 7.5 kHz step.
        iden_tsbk = pack_iden_up_vu(
            iden=1, freq_5hz=int(150_815_000 / 5), spac=60, toff_signed=-180
        )
        _process_msg(_tsbk_msg(iden_tsbk), state)
        # Now an MBT ADJ_STS_BCST on table=1, channel=0x10.
        _process_msg(_mbt_adj_msg(syid=0xB04, rfid=1, stid=204, ch1=0x1010), state)
        assert 0x1010 in state.neighbors
        n = state.neighbors[0x1010]
        assert n.rfss_id == 1
        assert n.site_id == 204
        assert n.sysid == 0xB04
        assert n.freq_hz == 150_815_000 + 16 * 7_500

    def test_mbt_adj_sts_pending_when_iden_unknown(self):
        """No IDEN_UP yet → neighbor queues as pending, resolves when iden lands.
        Mirrors the TSBK pending path so MBT doesn't skip the queue."""
        state = _DwellState()
        _process_msg(_mbt_adj_msg(syid=0xB04, rfid=1, stid=204, ch1=0x1010), state)
        assert 0x1010 not in state.neighbors
        assert len(state.pending_neighbors) == 1

        iden_tsbk = pack_iden_up_vu(
            iden=1, freq_5hz=int(150_815_000 / 5), spac=60, toff_signed=-180
        )
        _process_msg(_tsbk_msg(iden_tsbk), state)
        assert 0x1010 in state.neighbors
        assert state.pending_neighbors == []

    def test_mbt_broadcast_counts_toward_settle(self):
        """state.broadcast_count must include MBT broadcasts — otherwise
        a single-site WISCOM dwell would never reach the early-exit threshold
        and would always burn the full max_dwell on TSBK-only sites."""
        state = _DwellState()
        before = state.broadcast_count
        _process_msg(_mbt_adj_msg(syid=0xB04, rfid=1, stid=204, ch1=0x1010), state)
        assert state.broadcast_count == before + 1
