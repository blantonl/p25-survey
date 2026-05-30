"""P25 TSBK + MBT + TDMA broadcast PDU parser.

Bit layouts ported from op25/gr-op25_repeater/apps/tk_p25.py (decode_tsbk,
decode_mbt_data, and decode_tdma_ptt). Opcodes handled:

  FDMA TSBK (80-bit, 10-byte wire format; op25 left-shifts 16 to get 96-bit
  working integer with opcode at bits 88..94):
    0x33  IDEN_UP_TDMA           — band plan with TDMA slots-per-carrier
    0x34  IDEN_UP_VU             — band plan (VHF/UHF)
    0x39  SCCB                   — secondary control channel broadcast
    0x3a  RFSS_STS_BCST          — RFSS / site
    0x3b  NET_STS_BCST           — WACN / system ID
    0x3c  ADJ_STS_BCST           — neighbor site
    0x3d  IDEN_UP                — band plan (legacy 800/700)

  FDMA MBT (Multi-Block Trunking — Extended Format only, fmt=0x17). Op25
  frame_assembler emits these as m_type=12; wire layout is a 10-byte header
  (no CRC, then 2-byte CRC gap, then the data block(s)). Some VHF/UHF P25
  systems (notably WISCOM De Pere in Wisconsin) broadcast status / neighbor
  info via MBT instead of TSBK; if we don't parse these the survey shows
  zero neighbors on those sites. Opcodes handled here mirror the bcst set:
    0x3a  RFSS_STS_BCST          — RFSS / site
    0x3b  NET_STS_BCST           — WACN / system ID
    0x3c  ADJ_STS_BCST           — neighbor site (header carries syid too)

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
# NB: channel types 0/1/2 map to ONE slot per carrier — i.e. an FDMA channel.
# IDEN_UP_TDMA (opcode 0x33) can legitimately describe such FDMA channels
# (WISCOM advertises its all-FDMA VHF/700/800 band plan this way), so the
# message arriving as 0x33 does NOT by itself mean the channel is TDMA. A
# channel is only TDMA when it carries 2+ logical slots per carrier.
_SLOTS_PER_CARRIER = (1, 1, 1, 2, 4, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2)

# IDEN_UP_VU "BW VU" receiver-bandwidth field (octet 2, bits 3-0).
# TIA-102.AABC-B §6.2.29: %0100 = 6.25 kHz, %0101 = 12.5 kHz; others reserved.
_BW_VU_HZ = {0x4: 6_250, 0x5: 12_500}


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
    bandwidth_hz: int | None = None  # channel bandwidth (IDEN_UP_VU BW field)
    opcode: int = 0           # source opcode for debugging


@dataclass(frozen=True)
class RfssStsBcst:
    sysid: int
    rfss_id: int
    site_id: int
    cc_channel_id: int
    # A bit (octet 3, bit 4): site has an active network connection to the RFSS
    # controller. False == failsoft / site-trunking. None when not decoded.
    network_active: bool | None = None
    lra: int | None = None  # Location Registration Area (octet 2)


@dataclass(frozen=True)
class NetStsBcst:
    wacn: int
    sysid: int
    cc_channel_id: int
    lra: int | None = None  # Location Registration Area (octet 2)


@dataclass(frozen=True)
class AdjStsBcst:
    rfss_id: int
    site_id: int
    channel_id: int
    sysid: int | None = None  # set by TDMA explicit (0xfa-family)
    wacn: int | None = None   # set by TDMA extended explicit (0xfe)
    # ADJ_STS_BCST octet-3 signaling bits. None when not decoded (e.g. MBT/TDMA
    # paths, whose op25-aligned bit offsets we don't reproduce here).
    conventional: bool | None = None    # C (bit 7): advertising a conventional channel
    site_failure: bool | None = None    # F (bit 6): neighbor is in a failure condition
    valid: bool | None = None           # V (bit 5): info current (0 == last-known/stale)
    network_active: bool | None = None  # A (bit 4): neighbor has active RFSS network conn
    lra: int | None = None              # Location Registration Area (octet 2)


@dataclass(frozen=True)
class SysSrvBcst:
    """System Service Broadcast (FDMA opcode 0x38). Advertises which trunking
    services the site currently offers (available) and is equipped for
    (supported), plus the request-priority threshold. The 24-bit fields are
    decoded to service-name lists by `decode_system_services`."""
    services_available: int
    services_supported: int
    request_priority: int


@dataclass(frozen=True)
class TimeDateAnn:
    """Time and Date Announcement (FDMA opcode 0x35). We keep only the UTC
    offset (minutes, signed) when the message flags it valid — the date/time
    themselves are transient and not useful for cataloging."""
    utc_offset_min: int | None = None


@dataclass(frozen=True)
class ProtParamBcst:
    """Protection Parameter Broadcast (MBT opcode 0x3e). Its mere presence on a
    control channel means the CC is encrypted/protected; algid names the
    algorithm. Carried as a 3-block MBT, so we read only algid (header octet 9),
    which is at a fixed position — not the key id / message indicator."""
    algid: int


@dataclass(frozen=True)
class Sccb:
    """Secondary control channel broadcast (FDMA opcode 0x39)."""
    rfss_id: int
    site_id: int
    cc1_channel_id: int
    cc2_channel_id: int


ParsedTsbk = (IdenUp | RfssStsBcst | NetStsBcst | AdjStsBcst | Sccb
              | SysSrvBcst | TimeDateAnn | ProtParamBcst)


# System service bit names, TIA-102.AABC-B Table 6.2.19-1 (Normal services).
# 24-bit field, b1 (MSB) .. b24 (LSB); service b_n is bit (24 - n). The two
# flag bits and reserved positions are intentionally left unnamed (None) so
# they don't show up as "services".
_SYSTEM_SERVICE_NAMES = (
    None, None, "network active", None,            # b1-b4
    "group voice", "individual voice",             # b5-b6
    "PSTN-unit voice", "unit-PSTN voice",          # b7-b8
    None, "group data", "individual data", None,   # b9-b12
    "unit registration", "group affiliation",      # b13-b14
    "group affiliation query", "authentication",   # b15-b16
    "encryption", "user status", "user message",   # b17-b19
    "unit status", "user status query",            # b20-b21
    "unit status query", "unit page",              # b22-b23
    "emergency alarm",                             # b24
)


def decode_system_services(value: int) -> list[str]:
    """Service names whose bit is set in a 24-bit System Service field."""
    names = []
    for n, name in enumerate(_SYSTEM_SERVICE_NAMES, start=1):
        if name is not None and (value >> (24 - n)) & 1:
            names.append(name)
    return names


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
        bw_vu = (tsbk >> 72) & 0xF  # octet 2, bits 3-0
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
            bandwidth_hz=_BW_VU_HZ.get(bw_vu),
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
        slots = _SLOTS_PER_CARRIER[ch_type]
        return IdenUp(
            iden=iden,
            base_freq_hz=freq * 5,
            step_hz=step_hz,
            offset_hz=toff * step_hz,
            is_tdma=slots > 1,  # 1 slot/carrier == FDMA; see _SLOTS_PER_CARRIER
            slots_per_carrier=slots,
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

    if opcode == 0x38:  # SYS_SRV_BCST
        avail = (tsbk >> 48) & 0xFFFFFF  # octets 3-5
        supp = (tsbk >> 24) & 0xFFFFFF   # octets 6-8
        prio = (tsbk >> 16) & 0xFF       # octet 9
        return SysSrvBcst(services_available=avail, services_supported=supp,
                          request_priority=prio)

    if opcode == 0x35:  # TIME_DATE_ANN
        # octet 2: VD|VT|VL|res|offset(b11-8); octet 3: offset(b7-0). The
        # offset is signed minutes (top bit = subtract-from-UTC).
        vl = (tsbk >> 77) & 1  # octet 2, bit 5 (global bit 72+5 = 77)
        if not vl:
            return TimeDateAnn(utc_offset_min=None)
        raw = (tsbk >> 64) & 0xFFF  # 12-bit Local Time Offset (octet 2 b3-0 + octet 3)
        minutes = raw & 0x7FF
        utc_offset = -minutes if (raw >> 11) & 1 else minutes
        return TimeDateAnn(utc_offset_min=utc_offset)

    if opcode == 0x3A:  # RFSS_STS_BCST
        lra = (tsbk >> 72) & 0xFF
        syid = (tsbk >> 56) & 0xFFF
        rfid = (tsbk >> 48) & 0xFF
        stid = (tsbk >> 40) & 0xFF
        chan = (tsbk >> 24) & 0xFFFF
        # A bit: octet 3 (global bits 64-71), bit 4 -> global bit 68.
        active = bool((tsbk >> 68) & 1)
        return RfssStsBcst(sysid=syid, rfss_id=rfid, site_id=stid,
                           cc_channel_id=chan, network_active=active, lra=lra)

    if opcode == 0x3B:  # NET_STS_BCST
        lra = (tsbk >> 72) & 0xFF
        wacn = (tsbk >> 52) & 0xFFFFF
        syid = (tsbk >> 40) & 0xFFF
        ch1 = (tsbk >> 24) & 0xFFFF
        return NetStsBcst(wacn=wacn, sysid=syid, cc_channel_id=ch1, lra=lra)

    if opcode == 0x3C:  # ADJ_STS_BCST
        lra = (tsbk >> 72) & 0xFF
        rfid = (tsbk >> 48) & 0xFF
        stid = (tsbk >> 40) & 0xFF
        ch1 = (tsbk >> 24) & 0xFFFF
        # Octet-3 signaling bits (octet 3 == global bits 64-71): C=bit7 (71),
        # F=bit6 (70), V=bit5 (69), A=bit4 (68).
        return AdjStsBcst(
            rfss_id=rfid, site_id=stid, channel_id=ch1, lra=lra,
            conventional=bool((tsbk >> 71) & 1),
            site_failure=bool((tsbk >> 70) & 1),
            valid=bool((tsbk >> 69) & 1),
            network_active=bool((tsbk >> 68) & 1),
        )

    return None


# ---------------------------------------------------------------------------
# FDMA MBT parser (Multi-Block Trunking — Extended Format only)
# ---------------------------------------------------------------------------


# Format field value that identifies an Extended Format MBT. Other fmt
# values (e.g. Confirmed Data) exist but op25 doesn't decode them, and the
# bcst opcodes we care about are always Extended.
_MBT_FMT_EXTENDED = 0x17


def parse_fdma_mbt(payload: bytes) -> ParsedTsbk | None:
    """Parse a FDMA MBT PDU as emitted by op25's frame_assembler (m_type=12).

    Wire layout — matches op25 tk_p25.decode_msg:
        bytes [0:10]   = 80-bit MBT header (no CRC)
        bytes [10:12]  = 16-bit CRC of the header (ignored here)
        bytes [12:]    = MBT data block(s); length depends on opcode

    Returns None for non-Extended formats and for opcodes we don't track.
    Caller (decoder._process_msg) treats `None` as "skip this frame".
    """
    if len(payload) < 13:
        return None
    header = int.from_bytes(payload[:10], "big")
    data = int.from_bytes(payload[12:], "big")

    fmt = (header >> 72) & 0x1F
    if fmt != _MBT_FMT_EXTENDED:
        return None
    opcode = (header >> 16) & 0x3F

    # op25 shifts the header/data values to align bit positions for the
    # per-opcode decoders. We match that convention so the bit indices below
    # line up 1:1 with tk_p25.decode_mbt_data.
    header_shifted = header << 16
    data_shifted = data << 32

    return _parse_fdma_mbt_int(opcode, header_shifted, data_shifted)


def parse_fdma_mbt_int(opcode: int, header: int, mbt_data: int) -> ParsedTsbk | None:
    """Test-friendly entry that takes the same pre-shifted ints op25 uses.

    `header` is the 80-bit MBT header left-shifted by 16 (so its top bits
    line up with op25's `header` variable). `mbt_data` is the data block(s)
    left-shifted by 32 (matching op25's `mbt_data` variable). See
    `parse_fdma_mbt` for the byte-level entry.
    """
    return _parse_fdma_mbt_int(opcode, header, mbt_data)


def _parse_fdma_mbt_int(opcode: int, header: int, mbt_data: int) -> ParsedTsbk | None:
    # Bit indices and field widths mirror tk_p25.decode_mbt_data (boatbod op25
    # gr-op25_repeater/apps/tk_p25.py:667-766). The shifted form below is
    # *not* a guess — it's a direct port. If op25 changes these layouts the
    # only correct response is to update both here and the comment.
    if opcode == 0x3C:  # ADJ_STS_BCST (MBT) — neighbor site, with syid in header
        syid = (header >> 48) & 0xFFF
        rfid = (header >> 24) & 0xFF
        stid = (header >> 16) & 0xFF
        ch1 = (mbt_data >> 80) & 0xFFFF
        return AdjStsBcst(rfss_id=rfid, site_id=stid, channel_id=ch1, sysid=syid)

    if opcode == 0x3B:  # NET_STS_BCST (MBT)
        syid = (header >> 48) & 0xFFF
        wacn = (mbt_data >> 76) & 0xFFFFF
        ch1 = (mbt_data >> 56) & 0xFFFF
        return NetStsBcst(wacn=wacn, sysid=syid, cc_channel_id=ch1)

    if opcode == 0x3A:  # RFSS_STS_BCST (MBT)
        syid = (header >> 48) & 0xFFF
        rfid = (mbt_data >> 88) & 0xFF
        stid = (mbt_data >> 80) & 0xFF
        ch1 = (mbt_data >> 64) & 0xFFFF
        return RfssStsBcst(sysid=syid, rfss_id=rfid, site_id=stid, cc_channel_id=ch1)

    if opcode == 0x3E:  # P_PARM_BCST (MBT) — protected/encrypted control channel
        # Algorithm ID is header octet 9. With `header` == raw_header << 16,
        # raw octet 9 (raw bits 0-7) sits at header bits 16-23. Key id / message
        # indicator live in the data blocks and aren't decoded here.
        algid = (header >> 16) & 0xFF
        return ProtParamBcst(algid=algid)

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
        slots = _SLOTS_PER_CARRIER[ch_type]
        return IdenUp(
            iden=iden,
            base_freq_hz=base_f * 5,
            step_hz=step_hz,
            offset_hz=tx_off * step_hz,
            is_tdma=slots > 1,  # 1 slot/carrier == FDMA; see _SLOTS_PER_CARRIER
            slots_per_carrier=slots,
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
