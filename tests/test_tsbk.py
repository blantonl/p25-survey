"""TSBK parser tests.

Test strategy: bit-pack a TSBK with known field values using the same
shifts the decoder uses, run it through the parser, assert the round-trip.
This validates each opcode without needing real radio captures.
"""

import pytest

from p25_survey.tsbk import (
    AdjStsBcst,
    FreqTable,
    IdenUp,
    NetStsBcst,
    RfssStsBcst,
    Sccb,
    parse_fdma_tsbk,
    parse_fdma_tsbk_int,
    parse_tdma_pdu,
)


# ---------------------------------------------------------------------------
# Helpers — bit-pack TSBKs / TDMA PDUs the same way op25 unpacks them.
# ---------------------------------------------------------------------------


def pack_fdma(opcode: int, fields: dict[str, tuple[int, int]]) -> int:
    """Pack a 96-bit TSBK with opcode at bits 88..94. fields = {name: (shift, value)}."""
    tsbk = (opcode & 0x3F) << 88
    for _, (shift, value) in fields.items():
        tsbk |= (value & ((1 << 64) - 1)) << shift
    return tsbk


def pack_iden_up_vu(iden: int, freq_5hz: int, spac: int, toff_signed: int) -> int:
    """Build a 0x34 TSBK. toff_signed is the signed offset multiplier
    (per op25 convention: stored sign bit, then magnitude; sign=0 means negative)."""
    if toff_signed < 0:
        sign_bit = 0
        magnitude = -toff_signed
    else:
        sign_bit = 1
        magnitude = toff_signed
    toff0 = (sign_bit << 13) | (magnitude & 0x1FFF)
    return pack_fdma(0x34, {
        "iden": (76, iden),
        "toff0": (58, toff0),
        "spac": (48, spac),
        "freq": (16, freq_5hz),
    })


def pack_iden_up_tdma(iden: int, ch_type: int, freq_5hz: int, spac: int, toff_signed: int) -> int:
    if toff_signed < 0:
        sign_bit, magnitude = 0, -toff_signed
    else:
        sign_bit, magnitude = 1, toff_signed
    toff0 = (sign_bit << 13) | (magnitude & 0x1FFF)
    return pack_fdma(0x33, {
        "mfrid": (80, 0),
        "iden": (76, iden),
        "ch_type": (72, ch_type),
        "toff0": (58, toff0),
        "spac": (48, spac),
        "freq": (16, freq_5hz),
    })


def pack_iden_up_legacy(iden: int, freq_5hz: int, spac: int, toff_signed: int) -> int:
    if toff_signed < 0:
        sign_bit, magnitude = 0, -toff_signed
    else:
        sign_bit, magnitude = 1, toff_signed
    toff0 = (sign_bit << 8) | (magnitude & 0xFF)
    return pack_fdma(0x3D, {
        "iden": (76, iden),
        "toff0": (58, toff0),
        "spac": (48, spac),
        "freq": (16, freq_5hz),
    })


def pack_rfss_sts(syid: int, rfid: int, stid: int, chan: int) -> int:
    return pack_fdma(0x3A, {
        "syid": (56, syid),
        "rfid": (48, rfid),
        "stid": (40, stid),
        "chan": (24, chan),
    })


def pack_net_sts(wacn: int, syid: int, ch1: int) -> int:
    return pack_fdma(0x3B, {
        "wacn": (52, wacn),
        "syid": (40, syid),
        "ch1": (24, ch1),
    })


def pack_adj_sts(rfid: int, stid: int, ch1: int) -> int:
    return pack_fdma(0x3C, {
        "rfid": (48, rfid),
        "stid": (40, stid),
        "ch1": (24, ch1),
    })


def pack_sccb(rfid: int, stid: int, ch1: int, ch2: int) -> int:
    return pack_fdma(0x39, {
        "rfid": (72, rfid),
        "stid": (64, stid),
        "ch1": (48, ch1),
        "ch2": (24, ch2),
    })


# ---------------------------------------------------------------------------
# FDMA opcode tests
# ---------------------------------------------------------------------------


class TestNetStsBcst:
    def test_round_trip(self):
        tsbk = pack_net_sts(wacn=0xBEE00, syid=0x1A4, ch1=0x1234)
        result = parse_fdma_tsbk_int(0x3B, tsbk)
        assert isinstance(result, NetStsBcst)
        assert result.wacn == 0xBEE00
        assert result.sysid == 0x1A4
        assert result.cc_channel_id == 0x1234

    def test_max_values(self):
        tsbk = pack_net_sts(wacn=0xFFFFF, syid=0xFFF, ch1=0xFFFF)
        result = parse_fdma_tsbk_int(0x3B, tsbk)
        assert result.wacn == 0xFFFFF
        assert result.sysid == 0xFFF
        assert result.cc_channel_id == 0xFFFF


class TestRfssStsBcst:
    def test_round_trip(self):
        tsbk = pack_rfss_sts(syid=0x1A4, rfid=1, stid=7, chan=0x1023)
        result = parse_fdma_tsbk_int(0x3A, tsbk)
        assert isinstance(result, RfssStsBcst)
        assert result.sysid == 0x1A4
        assert result.rfss_id == 1
        assert result.site_id == 7
        assert result.cc_channel_id == 0x1023


class TestAdjStsBcst:
    def test_round_trip(self):
        tsbk = pack_adj_sts(rfid=2, stid=15, ch1=0x2055)
        result = parse_fdma_tsbk_int(0x3C, tsbk)
        assert isinstance(result, AdjStsBcst)
        assert result.rfss_id == 2
        assert result.site_id == 15
        assert result.channel_id == 0x2055
        assert result.sysid is None  # FDMA 0x3c doesn't carry sysid
        assert result.wacn is None


class TestSccb:
    def test_round_trip(self):
        tsbk = pack_sccb(rfid=1, stid=7, ch1=0x1010, ch2=0x1011)
        result = parse_fdma_tsbk_int(0x39, tsbk)
        assert isinstance(result, Sccb)
        assert result.rfss_id == 1
        assert result.site_id == 7
        assert result.cc1_channel_id == 0x1010
        assert result.cc2_channel_id == 0x1011


class TestIdenUpVu:
    def test_basic(self):
        # 850 MHz example: freq encoded in 5 Hz units.
        # base = 851.00625 MHz → freq = 851006250 // 5 = 170201250
        # spac = 100 → step 12.5 kHz
        # toff = -3600 → -45 MHz at 12.5 kHz step
        tsbk = pack_iden_up_vu(iden=3, freq_5hz=170_201_250, spac=100, toff_signed=-3600)
        result = parse_fdma_tsbk_int(0x34, tsbk)
        assert isinstance(result, IdenUp)
        assert result.iden == 3
        assert result.base_freq_hz == 851_006_250
        assert result.step_hz == 12_500
        assert result.offset_hz == -45_000_000
        assert result.is_tdma is False

    def test_positive_offset(self):
        tsbk = pack_iden_up_vu(iden=0, freq_5hz=170_201_250, spac=100, toff_signed=3600)
        result = parse_fdma_tsbk_int(0x34, tsbk)
        assert result.offset_hz == 45_000_000


class TestIdenUpTdma:
    def test_phase2_2slot(self):
        # ch_type=3 → slots_per_carrier=2 (per op25 lookup)
        tsbk = pack_iden_up_tdma(iden=2, ch_type=3, freq_5hz=170_201_250, spac=100, toff_signed=-3600)
        result = parse_fdma_tsbk_int(0x33, tsbk)
        assert result.is_tdma is True
        assert result.slots_per_carrier == 2
        assert result.step_hz == 12_500
        assert result.base_freq_hz == 851_006_250

    def test_ch_type_4_is_4slot(self):
        tsbk = pack_iden_up_tdma(iden=1, ch_type=4, freq_5hz=170_201_250, spac=100, toff_signed=-3600)
        assert parse_fdma_tsbk_int(0x33, tsbk).slots_per_carrier == 4

    def test_mfg_specific_ignored(self):
        tsbk = pack_iden_up_tdma(iden=1, ch_type=3, freq_5hz=1, spac=1, toff_signed=0)
        # forge a non-zero mfrid
        tsbk |= 0x90 << 80
        assert parse_fdma_tsbk_int(0x33, tsbk) is None


class TestIdenUpLegacy:
    def test_800mhz(self):
        # 800 MHz legacy uses 250 kHz offset units
        tsbk = pack_iden_up_legacy(iden=0, freq_5hz=170_201_250, spac=50, toff_signed=-180)
        result = parse_fdma_tsbk_int(0x3D, tsbk)
        assert result.base_freq_hz == 851_006_250
        assert result.step_hz == 6_250  # 50 * 125
        assert result.offset_hz == -45_000_000  # -180 * 250000
        assert result.is_tdma is False


class TestUnsupportedOpcodes:
    def test_returns_none(self):
        assert parse_fdma_tsbk_int(0x00, 0) is None
        assert parse_fdma_tsbk_int(0x01, 0) is None
        assert parse_fdma_tsbk_int(0x40, 0) is None  # opcode is 6 bits, > 0x3f impossible


# ---------------------------------------------------------------------------
# Wire-format (bytes) parser
# ---------------------------------------------------------------------------


class TestParseFdmaTsbkBytes:
    def test_net_sts_via_bytes(self):
        # Construct a 96-bit packed integer, then encode the upper 80 bits as 10 bytes.
        tsbk96 = pack_net_sts(wacn=0xBEE00, syid=0x1A4, ch1=0x1234)
        # Wire format = 80 bits = (tsbk96 >> 16) as 10 bytes big-endian
        wire = (tsbk96 >> 16).to_bytes(10, "big")
        result = parse_fdma_tsbk(wire)
        assert isinstance(result, NetStsBcst)
        assert result.wacn == 0xBEE00
        assert result.sysid == 0x1A4

    def test_too_short_returns_none(self):
        assert parse_fdma_tsbk(b"\x00" * 5) is None


# ---------------------------------------------------------------------------
# TDMA broadcast PDU tests
# ---------------------------------------------------------------------------


def _make_tdma_msg(op: int, payload: bytes, total_len: int = 16) -> bytes:
    """Build a TDMA PDU: opcode byte + payload, padded with zeros."""
    out = bytearray(total_len)
    out[0] = op
    out[2:2 + len(payload)] = payload
    return bytes(out)


class TestTdmaRfssSts:
    def test_round_trip(self):
        # bytes 2..3: syid (12 bits in low part of 2 bytes)
        # byte 4: rfid; byte 5: stid; bytes 6..7: ch_t
        payload = bytearray(8)
        payload[0:2] = (0x1A4).to_bytes(2, "big")        # syid
        payload[2] = 1                                     # rfid
        payload[3] = 7                                     # stid
        payload[4:6] = (0x1023).to_bytes(2, "big")        # ch_t
        msg = _make_tdma_msg(0xFA, bytes(payload))
        result = parse_tdma_pdu(msg)
        assert isinstance(result, RfssStsBcst)
        assert result.sysid == 0x1A4
        assert result.rfss_id == 1
        assert result.site_id == 7
        assert result.cc_channel_id == 0x1023


class TestTdmaNetSts:
    def test_round_trip(self):
        # Bytes 2..4: top 20 bits = wacn (the upper 20 of a 24-bit field >> 4)
        # Bytes 4..5: low 12 bits = sysid
        # Pack: wacn=0xBEE00, syid=0x1A4
        # 24-bit int with wacn in top 20 + 4 bits low = (wacn << 4) | top_4_of_syid
        # That overlaps with byte 4 so the same byte-4 high nibble is "low 4 of wacn shift"
        # Simpler: write wacn << 4 | (sysid >> 8) into bytes 2..4 and sysid & 0xff into byte 5
        wacn, syid = 0xBEE00, 0x1A4
        b2_4 = (wacn << 4) | (syid >> 8)
        payload = bytearray(8)
        payload[0:3] = b2_4.to_bytes(3, "big")
        payload[3] = syid & 0xFF
        payload[4:6] = (0x1234).to_bytes(2, "big")  # ch_t
        msg = _make_tdma_msg(0xFB, bytes(payload))
        result = parse_tdma_pdu(msg)
        assert isinstance(result, NetStsBcst)
        assert result.wacn == 0xBEE00
        assert result.sysid == 0x1A4
        assert result.cc_channel_id == 0x1234


class TestTdmaAdjStsExplicit:
    def test_carries_sysid_no_wacn(self):
        payload = bytearray(8)
        payload[0:2] = (0x1A4).to_bytes(2, "big")
        payload[2] = 2
        payload[3] = 9
        payload[4:6] = (0x2055).to_bytes(2, "big")
        msg = _make_tdma_msg(0xFC, bytes(payload))
        result = parse_tdma_pdu(msg)
        assert isinstance(result, AdjStsBcst)
        assert result.sysid == 0x1A4
        assert result.rfss_id == 2
        assert result.site_id == 9
        assert result.channel_id == 0x2055
        assert result.wacn is None


class TestTdmaAdjStsExtended:
    def test_carries_wacn_and_sysid(self):
        # Bytes 12..14 hold wacn in top 20 bits.
        payload = bytearray(13)
        payload[0:2] = (0x1A4).to_bytes(2, "big")    # syid
        payload[2] = 2                                 # rfid
        payload[3] = 9                                 # stid
        payload[4:6] = (0x2055).to_bytes(2, "big")   # ch_t
        # bytes 12..14 of msg = bytes 10..12 of payload (since payload starts at msg[2])
        wacn = 0xBEE00
        payload[10:13] = (wacn << 4).to_bytes(3, "big")
        msg = _make_tdma_msg(0xFE, bytes(payload), total_len=20)
        result = parse_tdma_pdu(msg)
        assert isinstance(result, AdjStsBcst)
        assert result.wacn == 0xBEE00
        assert result.sysid == 0x1A4


class TestTdmaUnsupported:
    def test_returns_none(self):
        assert parse_tdma_pdu(b"\x00" + b"\x00" * 15) is None
        assert parse_tdma_pdu(b"") is None


# ---------------------------------------------------------------------------
# FreqTable tests
# ---------------------------------------------------------------------------


class TestFreqTable:
    def test_empty_returns_none(self):
        ft = FreqTable()
        assert ft.channel_id_to_frequency(0x0000) is None

    def test_fdma_channel_resolution(self):
        ft = FreqTable()
        # Table 0: base 851.00625 MHz, step 12.5 kHz
        ft.add(IdenUp(iden=0, base_freq_hz=851_006_250, step_hz=12_500, offset_hz=0))
        # channel_id = (table << 12) | channel
        assert ft.channel_id_to_frequency(0x0000) == 851_006_250
        assert ft.channel_id_to_frequency(0x0001) == 851_018_750
        assert ft.channel_id_to_frequency(0x0010) == 851_006_250 + 16 * 12_500

    def test_tdma_channel_division(self):
        ft = FreqTable()
        # 2-slot TDMA: channel index divided by slots_per_carrier
        ft.add(IdenUp(
            iden=2, base_freq_hz=851_000_000, step_hz=12_500,
            offset_hz=0, is_tdma=True, slots_per_carrier=2,
        ))
        # channel 0,1 → slot 0 of carrier 0
        # channel 2,3 → slot 0 of carrier 1
        cid_base = 2 << 12
        assert ft.channel_id_to_frequency(cid_base | 0) == 851_000_000
        assert ft.channel_id_to_frequency(cid_base | 1) == 851_000_000
        assert ft.channel_id_to_frequency(cid_base | 2) == 851_012_500
        assert ft.channel_id_to_frequency(cid_base | 3) == 851_012_500

    def test_unknown_table_returns_none(self):
        ft = FreqTable()
        ft.add(IdenUp(iden=0, base_freq_hz=851_006_250, step_hz=12_500, offset_hz=0))
        assert ft.channel_id_to_frequency(0x5000) is None  # table 5 not in plan

    def test_replaces_on_readd(self):
        ft = FreqTable()
        ft.add(IdenUp(iden=0, base_freq_hz=100_000_000, step_hz=12_500, offset_hz=0))
        ft.add(IdenUp(iden=0, base_freq_hz=200_000_000, step_hz=12_500, offset_hz=0))
        assert ft.channel_id_to_frequency(0x0000) == 200_000_000


# ---------------------------------------------------------------------------
# End-to-end: a fake CC's worth of TSBKs decoded and resolved together.
# ---------------------------------------------------------------------------


class TestFullSiteCharacterization:
    """Simulate one CC dwell: pull NET_STS, RFSS_STS, IDEN_UP, ADJ_STS;
    resolve neighbor frequencies via the freq table."""

    def test_collects_full_site(self):
        ft = FreqTable()

        # IDEN_UP arrives first: 800 MHz band plan, table 0.
        iden_tsbk = pack_iden_up_legacy(
            iden=0, freq_5hz=170_201_250, spac=50, toff_signed=-180
        )
        iden = parse_fdma_tsbk_int(0x3D, iden_tsbk)
        assert isinstance(iden, IdenUp)
        ft.add(iden)

        # NET_STS_BCST: WACN, SYSID
        net_tsbk = pack_net_sts(wacn=0xBEE00, syid=0x1A4, ch1=0x0001)
        net = parse_fdma_tsbk_int(0x3B, net_tsbk)
        assert net.wacn == 0xBEE00
        cc_freq = ft.channel_id_to_frequency(net.cc_channel_id)
        assert cc_freq == 851_006_250 + 6_250  # ch 1 at 6.25 kHz step

        # RFSS_STS_BCST
        rfss_tsbk = pack_rfss_sts(syid=0x1A4, rfid=1, stid=7, chan=0x0001)
        rfss = parse_fdma_tsbk_int(0x3A, rfss_tsbk)
        assert rfss.rfss_id == 1
        assert rfss.site_id == 7

        # Two ADJ_STS_BCST broadcasts, neighbors on different channels.
        n1 = parse_fdma_tsbk_int(0x3C, pack_adj_sts(rfid=1, stid=8, ch1=0x0010))
        n2 = parse_fdma_tsbk_int(0x3C, pack_adj_sts(rfid=1, stid=9, ch1=0x0020))
        n1_freq = ft.channel_id_to_frequency(n1.channel_id)
        n2_freq = ft.channel_id_to_frequency(n2.channel_id)
        assert n1_freq == 851_006_250 + 16 * 6_250
        assert n2_freq == 851_006_250 + 32 * 6_250
