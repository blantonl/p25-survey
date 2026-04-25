"""P25 TSBK + TDMA broadcast PDU parser.

Bit layouts ported from op25/gr-op25_repeater/apps/tk_p25.py (decode_tsbk
and decode_tdma_ptt). Opcodes handled:

  FDMA TSBK (80-bit, 10-byte wire format; op25 left-shifts 16 to get 96-bit
  working integer with opcode at bits 88..94):
    0x33  IDEN_UP_TDMA           — band plan with TDMA slots-per-carrier
    0x34  IDEN_UP_VU             — band plan (VHF/UHF)
    0x39  SCCB                   — secondary control channel broadcast
    0x3a  RFSS_STS_BCST          — RFSS / site
    0x3b  NET_STS_BCST           — WACN / system ID
    0x3c  ADJ_STS_BCST           — neighbor site
    0x3d  IDEN_UP                — band plan (legacy 800/700)

  TDMA broadcast PDU (op = first byte of msg):
    0xf3  IDEN_UP_TDMA Extended
    0xfa  RFSS_STS_BCST Explicit
    0xfb  NET_STS_BCST Explicit
    0xfc  ADJ_STS_BCST Explicit
    0xfe  ADJ_STS_BCST Extended Explicit (carries WACN)

Channel-ID → frequency resolution lives in FreqTable, which accumulates
IDEN_UP records and reproduces tk_p25.channel_id_to_frequency.
"""

from __future__ import annotations

from dataclasses import dataclass

# slots_per_carrier lookup for TDMA channel_type fields.
# Values 0..5 valid; 6+ reserved. Mirrors tk_p25.py line 919.
_SLOTS_PER_CARRIER = (1, 1, 1, 2, 4, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdenUp:
    """Band plan entry. base_freq_hz + step_hz * channel = downlink freq."""
    iden: int
    base_freq_hz: int
    step_hz: int
    offset_hz: int            # signed; mobile uplink offset
    is_tdma: bool = False
    slots_per_carrier: int = 1
    opcode: int = 0           # source opcode for debugging


@dataclass(frozen=True)
class RfssStsBcst:
    sysid: int
    rfss_id: int
    site_id: int
    cc_channel_id: int


@dataclass(frozen=True)
class NetStsBcst:
    wacn: int
    sysid: int
    cc_channel_id: int


@dataclass(frozen=True)
class AdjStsBcst:
    rfss_id: int
    site_id: int
    channel_id: int
    sysid: int | None = None  # set by TDMA explicit (0xfa-family)
    wacn: int | None = None   # set by TDMA extended explicit (0xfe)


@dataclass(frozen=True)
class Sccb:
    """Secondary control channel broadcast (FDMA opcode 0x39)."""
    rfss_id: int
    site_id: int
    cc1_channel_id: int
    cc2_channel_id: int


ParsedTsbk = IdenUp | RfssStsBcst | NetStsBcst | AdjStsBcst | Sccb


# ---------------------------------------------------------------------------
# Frequency table
# ---------------------------------------------------------------------------


class FreqTable:
    """Accumulates IDEN_UP records and resolves channel IDs to frequencies.

    Mirrors tk_p25.channel_id_to_frequency:
        table   = (channel_id >> 12) & 0xf
        channel =  channel_id        & 0xfff
        freq    = base + step * channel               (FDMA)
        freq    = base + step * (channel // tdma)     (TDMA)
    """

    def __init__(self) -> None:
        self._idens: dict[int, IdenUp] = {}

    def add(self, iden_up: IdenUp) -> None:
        self._idens[iden_up.iden] = iden_up

    def __contains__(self, iden: int) -> bool:
        return iden in self._idens

    def __len__(self) -> int:
        return len(self._idens)

    def get(self, iden: int) -> IdenUp | None:
        return self._idens.get(iden)

    def all_idens(self) -> list[IdenUp]:
        return sorted(self._idens.values(), key=lambda i: i.iden)

    def channel_id_to_frequency(self, channel_id: int) -> int | None:
        table = (channel_id >> 12) & 0xf
        channel = channel_id & 0xfff
        iden = self._idens.get(table)
        if iden is None:
            return None
        if iden.is_tdma and iden.slots_per_carrier > 0:
            return iden.base_freq_hz + iden.step_hz * (channel // iden.slots_per_carrier)
        return iden.base_freq_hz + iden.step_hz * channel


# ---------------------------------------------------------------------------
# FDMA TSBK parser
# ---------------------------------------------------------------------------


def parse_fdma_tsbk(tsbk_bytes: bytes) -> ParsedTsbk | None:
    """Parse a 10-byte FDMA TSBK. Returns None if opcode is not one we track.

    Caller passes the raw 80-bit TSBK as 10 bytes (the format op25's
    frame_assembler emits). Internally we left-shift by 16 to match the
    96-bit working integer convention used by tk_p25.decode_tsbk.
    """
    if len(tsbk_bytes) < 10:
        return None
    tsbk = int.from_bytes(tsbk_bytes[:10], "big") << 16
    opcode = (tsbk >> 88) & 0x3F
    return _parse_fdma_tsbk_int(opcode, tsbk)


def parse_fdma_tsbk_int(opcode: int, tsbk: int) -> ParsedTsbk | None:
    """Parse a pre-shifted 96-bit TSBK integer with caller-supplied opcode.

    Useful for tests where we construct the TSBK by bit-packing rather than
    encoding bytes.
    """
    return _parse_fdma_tsbk_int(opcode, tsbk)


def _parse_fdma_tsbk_int(opcode: int, tsbk: int) -> ParsedTsbk | None:
    if opcode == 0x34:  # IDEN_UP_VU
        iden = (tsbk >> 76) & 0xF
        toff0 = (tsbk >> 58) & 0x3FFF
        spac = (tsbk >> 48) & 0x3FF
        freq = (tsbk >> 16) & 0xFFFFFFFF
        toff_sign = (toff0 >> 13) & 1
        toff = toff0 & 0x1FFF
        if toff_sign == 0:
            toff = -toff
        step_hz = spac * 125
        return IdenUp(
            iden=iden,
            base_freq_hz=freq * 5,
            step_hz=step_hz,
            offset_hz=toff * step_hz,
            opcode=opcode,
        )

    if opcode == 0x33:  # IDEN_UP_TDMA
        mfrid = (tsbk >> 80) & 0xFF
        if mfrid != 0:
            return None  # mfg-specific; tk_p25 ignores
        iden = (tsbk >> 76) & 0xF
        ch_type = (tsbk >> 72) & 0xF
        toff0 = (tsbk >> 58) & 0x3FFF
        spac = (tsbk >> 48) & 0x3FF
        freq = (tsbk >> 16) & 0xFFFFFFFF
        toff_sign = (toff0 >> 13) & 1
        toff = toff0 & 0x1FFF
        if toff_sign == 0:
            toff = -toff
        step_hz = spac * 125
        return IdenUp(
            iden=iden,
            base_freq_hz=freq * 5,
            step_hz=step_hz,
            offset_hz=toff * step_hz,
            is_tdma=True,
            slots_per_carrier=_SLOTS_PER_CARRIER[ch_type],
            opcode=opcode,
        )

    if opcode == 0x3D:  # IDEN_UP (legacy 800/700)
        iden = (tsbk >> 76) & 0xF
        toff0 = (tsbk >> 58) & 0x1FF
        spac = (tsbk >> 48) & 0x3FF
        freq = (tsbk >> 16) & 0xFFFFFFFF
        toff_sign = (toff0 >> 8) & 1
        toff = toff0 & 0xFF
        if toff_sign == 0:
            toff = -toff
        return IdenUp(
            iden=iden,
            base_freq_hz=freq * 5,
            step_hz=spac * 125,
            offset_hz=toff * 250_000,  # legacy: offset in 250 kHz units
            opcode=opcode,
        )

    if opcode == 0x39:  # SCCB
        rfid = (tsbk >> 72) & 0xFF
        stid = (tsbk >> 64) & 0xFF
        ch1 = (tsbk >> 48) & 0xFFFF
        ch2 = (tsbk >> 24) & 0xFFFF
        return Sccb(rfss_id=rfid, site_id=stid, cc1_channel_id=ch1, cc2_channel_id=ch2)

    if opcode == 0x3A:  # RFSS_STS_BCST
        syid = (tsbk >> 56) & 0xFFF
        rfid = (tsbk >> 48) & 0xFF
        stid = (tsbk >> 40) & 0xFF
        chan = (tsbk >> 24) & 0xFFFF
        return RfssStsBcst(sysid=syid, rfss_id=rfid, site_id=stid, cc_channel_id=chan)

    if opcode == 0x3B:  # NET_STS_BCST
        wacn = (tsbk >> 52) & 0xFFFFF
        syid = (tsbk >> 40) & 0xFFF
        ch1 = (tsbk >> 24) & 0xFFFF
        return NetStsBcst(wacn=wacn, sysid=syid, cc_channel_id=ch1)

    if opcode == 0x3C:  # ADJ_STS_BCST
        rfid = (tsbk >> 48) & 0xFF
        stid = (tsbk >> 40) & 0xFF
        ch1 = (tsbk >> 24) & 0xFFFF
        return AdjStsBcst(rfss_id=rfid, site_id=stid, channel_id=ch1)

    return None


# ---------------------------------------------------------------------------
# TDMA broadcast PDU parser
# ---------------------------------------------------------------------------


def parse_tdma_pdu(msg: bytes) -> ParsedTsbk | None:
    """Parse a TDMA broadcast PDU. msg[0] is the opcode byte."""
    if len(msg) < 1:
        return None
    op = msg[0]

    if op == 0xF3:  # IDEN_UP_TDMA Extended
        if len(msg) < 14:
            return None
        iden = (msg[2] >> 4) & 0xF
        ch_type = msg[2] & 0xF
        tx_off_raw = ((msg[3] << 8) | msg[4]) >> 2 & 0x3FFF
        tx_off = -(tx_off_raw & 0x1FFF) if (tx_off_raw >> 13) & 1 else (tx_off_raw & 0x1FFF)
        ch_spac = ((msg[4] << 8) | msg[5]) & 0x3FF
        base_f = int.from_bytes(msg[6:10], "big")
        step_hz = ch_spac * 125
        return IdenUp(
            iden=iden,
            base_freq_hz=base_f * 5,
            step_hz=step_hz,
            offset_hz=tx_off * step_hz,
            is_tdma=True,
            slots_per_carrier=_SLOTS_PER_CARRIER[ch_type],
            opcode=op,
        )

    if op == 0xFA:  # RFSS_STS_BCST Explicit
        if len(msg) < 10:
            return None
        syid = int.from_bytes(msg[2:4], "big") & 0xFFF
        rfid = msg[4]
        stid = msg[5]
        ch_t = int.from_bytes(msg[6:8], "big")
        return RfssStsBcst(sysid=syid, rfss_id=rfid, site_id=stid, cc_channel_id=ch_t)

    if op == 0xFB:  # NET_STS_BCST Explicit
        if len(msg) < 10:
            return None
        wacn = (int.from_bytes(msg[2:5], "big") >> 4) & 0xFFFFF
        syid = int.from_bytes(msg[4:6], "big") & 0xFFF
        ch_t = int.from_bytes(msg[6:8], "big")
        return NetStsBcst(wacn=wacn, sysid=syid, cc_channel_id=ch_t)

    if op == 0xFC:  # ADJ_STS_BCST Explicit
        if len(msg) < 10:
            return None
        syid = int.from_bytes(msg[2:4], "big") & 0xFFF
        rfid = msg[4]
        stid = msg[5]
        ch_t = int.from_bytes(msg[6:8], "big")
        return AdjStsBcst(rfss_id=rfid, site_id=stid, channel_id=ch_t, sysid=syid)

    if op == 0xFE:  # ADJ_STS_BCST Extended Explicit (carries WACN)
        if len(msg) < 15:
            return None
        syid = int.from_bytes(msg[2:4], "big") & 0xFFF
        rfid = msg[4]
        stid = msg[5]
        ch_t = int.from_bytes(msg[6:8], "big")
        wacn = (int.from_bytes(msg[12:15], "big") >> 4) & 0xFFFFF
        return AdjStsBcst(rfss_id=rfid, site_id=stid, channel_id=ch_t, sysid=syid, wacn=wacn)

    return None
