"""Pure-Python tests for decoder._process_msg state machine.

Avoids the GNU Radio flowgraph by constructing op25 frame_assembler-style
byte payloads directly and feeding them through _process_msg via a stub
message object.
"""

from __future__ import annotations

from p25_survey.decoder import _DwellState, _FRAME_TYPE_TSBK, _process_msg
from tests.test_tsbk import pack_iden_up_legacy, pack_sccb


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
