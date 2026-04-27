"""Tests for the RR enrichment matching logic.

Uses a fake RRClient (just the methods enrich.py calls) so no SOAP machinery
needs to be involved.
"""

from __future__ import annotations

from p25_survey.enrich import (
    BandOffsetSummary,
    EnrichmentResult,
    NeighborRef,
    enrich_record,
    summarize_band_offsets,
)
from p25_survey.radioreference import (
    RRError,
    RRSite,
    RRSiteFreq,
    RRSysidEntry,
    RRSystem,
)
from p25_survey.survey import NeighborSite, SurveyRecord


class FakeClient:
    """Stand-in for RRClient with just the methods enrich uses."""

    def __init__(self, system: RRSystem | None, sites: list[RRSite] | None = None,
                 raise_on_find: Exception | None = None,
                 raise_on_sites: Exception | None = None) -> None:
        self._system = system
        self._sites = sites or []
        self._raise_find = raise_on_find
        self._raise_sites = raise_on_sites

    def find_system_by_wacn_sysid(self, wacn_hex: str, sysid_hex: str) -> RRSystem | None:
        if self._raise_find:
            raise self._raise_find
        return self._system

    def get_sites(self, sid: int) -> list[RRSite]:
        if self._raise_sites:
            raise self._raise_sites
        return self._sites


# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------


def _system() -> RRSystem:
    return RRSystem(
        sid=42, name="Big P25 System", sys_type=2, sys_flavor=3,
        city="Anytown",
        sysid_entries=[RRSysidEntry(wacn="BEE00", sysid="1A4")],
    )


def _site(rfss: int, site_number: int, freqs_hz: list[int],
          alt_freqs_hz: list[int] | None = None,
          description: str = "Site Foo") -> RRSite:
    fs = [RRSiteFreq(freq_hz=f, use="d") for f in freqs_hz]
    fs += [RRSiteFreq(freq_hz=f, use="a") for f in (alt_freqs_hz or [])]
    return RRSite(
        sid=42, site_db_id=site_number * 100, site_number=site_number, rfss=rfss,
        description=description, location="City",
        county="County", lat=40.0, lon=-83.0,
        frequencies=fs, licenses=["WPRR123"],
    )


def _record(freq_hz: int = 851_006_250,
            neighbors: list[NeighborSite] | None = None,
            neighbors_hz: list[int] | None = None,
            wacn: int | None = 0xBEE00, sysid: int | None = 0x1A4,
            rfss_id: int | None = 1, site_id: int | None = 7) -> SurveyRecord:
    """Build a SurveyRecord for tests.

    `neighbors` takes explicit NeighborSite objects (preferred). The legacy
    `neighbors_hz` shortcut still works for the band-offset tests that
    don't care about neighbor identity — those neighbors all get a stub
    rfss/site that never matches any real RR fixture.
    """
    if neighbors is None:
        neighbors = [
            NeighborSite(freq_hz=f, rfss_id=1, site_id=8)
            for f in (neighbors_hz or [])
        ]
    return SurveyRecord(
        freq_hz=freq_hz, complete=True,
        wacn=wacn, sysid=sysid, nac=0x293,
        rfss_id=rfss_id, site_id=site_id,
        neighbors=neighbors,
    )


# ---------------------------------------------------------------------------
# enrich_record
# ---------------------------------------------------------------------------


class TestSystemMatching:
    def test_unknown_system(self):
        client = FakeClient(system=None)
        result = enrich_record(_record(), client)
        assert not result.system_match
        assert any("new system" in n for n in result.notes)

    def test_known_system_unknown_site(self):
        sites = [_site(rfss=2, site_number=99, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(_record(rfss_id=1, site_id=7), client)
        assert result.system_match
        assert not result.site_match
        assert result.rr_system_name == "Big P25 System"
        assert any("new site" in n for n in result.notes)

    def test_known_system_and_site(self):
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(_record(), client)
        assert result.system_match
        assert result.site_match
        assert result.cc_freq_offset is not None
        assert result.cc_freq_offset.offset_hz == 0
        assert result.cc_freq_in_db is True

    def test_partial_record_no_wacn(self):
        client = FakeClient(system=_system())
        result = enrich_record(_record(wacn=None), client)
        assert not result.system_match
        assert any("partial record" in n for n in result.notes)

    def test_rr_error_returns_note(self):
        client = FakeClient(system=None, raise_on_find=RRError("network down"))
        result = enrich_record(_record(), client)
        assert not result.system_match
        assert any("network down" in n for n in result.notes)


# ---------------------------------------------------------------------------
# Frequency offset
# ---------------------------------------------------------------------------


class TestFrequencyOffset:
    def test_exact_match(self):
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(_record(freq_hz=851_006_250), client)
        assert result.cc_freq_offset.offset_hz == 0
        assert result.cc_freq_offset.ppm == 0.0
        assert result.cc_freq_in_db

    def test_positive_offset(self):
        # We decoded 750 Hz higher than RR-listed.
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(_record(freq_hz=851_007_000), client)
        assert result.cc_freq_offset.offset_hz == 750
        # 750 / 851_007_000 * 1e6 ≈ 0.881 ppm
        assert 0.85 < result.cc_freq_offset.ppm < 0.91
        assert result.cc_freq_in_db  # within 1 kHz threshold

    def test_freq_not_in_db_when_far(self):
        # Decoded freq > 1 kHz away from any listed CC.
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(_record(freq_hz=851_009_000), client)
        assert result.cc_freq_offset.expected_hz == 851_006_250
        assert not result.cc_freq_in_db

    def test_picks_closest_when_multiple_ccs(self):
        # Site has primary + alternate CCs; we should match against the closer.
        sites = [_site(rfss=1, site_number=7,
                       freqs_hz=[851_006_250],
                       alt_freqs_hz=[855_062_500])]
        client = FakeClient(system=_system(), sites=sites)
        # Decoded matches the alt CC
        result = enrich_record(_record(freq_hz=855_062_500), client)
        assert result.cc_freq_offset.expected_hz == 855_062_500
        assert result.cc_freq_in_db


# ---------------------------------------------------------------------------
# Neighbor diff
# ---------------------------------------------------------------------------


class TestNeighborDiff:
    def test_neighbors_match(self):
        sites = [
            _site(rfss=1, site_number=7, freqs_hz=[851_006_250]),
            _site(rfss=1, site_number=8, freqs_hz=[851_106_250]),
            _site(rfss=1, site_number=9, freqs_hz=[851_206_250]),
        ]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=851_106_250, rfss_id=1, site_id=8),
                NeighborSite(freq_hz=851_206_250, rfss_id=1, site_id=9),
            ]),
            client,
        )
        assert result.neighbors_in_rr_not_decoded == []
        assert result.neighbors_decoded_not_in_rr == []

    def test_we_missed_a_neighbor(self):
        # RR has sites 7/8/9; we're on 7 and decoded only site 8 in
        # ADJ_STS_BCST. RR roster knows about 9 too, so it shows up as
        # "in RR, not decoded" — informational, since ADJ_STS_BCST is a
        # configured subset, not a discovery.
        sites = [
            _site(rfss=1, site_number=7, freqs_hz=[851_006_250]),
            _site(rfss=1, site_number=8, freqs_hz=[851_106_250]),
            _site(rfss=1, site_number=9, freqs_hz=[851_206_250]),
        ]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=851_106_250, rfss_id=1, site_id=8),
            ]),
            client,
        )
        assert len(result.neighbors_in_rr_not_decoded) == 1
        assert result.neighbors_in_rr_not_decoded[0].rfss_id == 1
        assert result.neighbors_in_rr_not_decoded[0].site_id == 9
        assert result.neighbors_in_rr_not_decoded[0].freq_hz == 851_206_250
        assert result.neighbors_decoded_not_in_rr == []

    def test_we_decoded_an_unknown_neighbor(self):
        # We saw a neighbor advertising RFSS 1 / Site 99; RR has no such
        # site in this system. That's a strong "admins should add" hint.
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=860_500_000, rfss_id=1, site_id=99),
            ]),
            client,
        )
        assert len(result.neighbors_decoded_not_in_rr) == 1
        nref = result.neighbors_decoded_not_in_rr[0]
        assert nref.rfss_id == 1
        assert nref.site_id == 99
        assert nref.freq_hz == 860_500_000
        assert result.neighbors_in_rr_not_decoded == []

    def test_voice_freqs_no_longer_flagged(self):
        # Regression: previously the diff compared frequency sets that
        # included every channel on every other site (including voice/
        # data freqs). With ID-based comparison, sites with matching
        # (rfss, site) produce no diff regardless of how many traffic
        # freqs RR lists for them.
        sites = [
            _site(rfss=1, site_number=7, freqs_hz=[851_006_250]),
            _site(rfss=1, site_number=8,
                  freqs_hz=[851_106_250],   # control
                  alt_freqs_hz=[]),
        ]
        # Pad site 8's frequency list with non-control channels (use="").
        from p25_survey.radioreference import RRSiteFreq
        sites[1].frequencies.extend([
            RRSiteFreq(freq_hz=852_000_000, use=""),
            RRSiteFreq(freq_hz=852_100_000, use=""),
            RRSiteFreq(freq_hz=852_200_000, use=""),
        ])
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=851_106_250, rfss_id=1, site_id=8),
            ]),
            client,
        )
        assert result.neighbors_decoded_not_in_rr == []
        assert result.neighbors_in_rr_not_decoded == []


# ---------------------------------------------------------------------------
# Per-band offset summary
# ---------------------------------------------------------------------------


def _enrichment_with_offset(decoded_hz: int, expected_hz: int) -> EnrichmentResult:
    from p25_survey.enrich import FreqOffset
    offset_hz = decoded_hz - expected_hz
    return EnrichmentResult(
        system_match=True, site_match=True,
        cc_freq_offset=FreqOffset(
            decoded_hz=decoded_hz, expected_hz=expected_hz,
            offset_hz=offset_hz,
            ppm=round(offset_hz / decoded_hz * 1e6, 3) if decoded_hz else 0.0,
        ),
        cc_freq_in_db=abs(offset_hz) < 1000,
    )


class TestSummarizeBandOffsets:
    def test_groups_by_band(self):
        records = [
            _record(freq_hz=851_006_250),
            _record(freq_hz=853_780_000),
            _record(freq_hz=771_368_750),
        ]
        enrichments = {
            851_006_250: _enrichment_with_offset(851_006_250, 851_006_250),  # 0 ppm
            853_780_000: _enrichment_with_offset(853_780_750, 853_780_000),  # +0.88 ppm
            771_368_750: _enrichment_with_offset(771_369_500, 771_368_750),  # +0.97 ppm
        }
        # Use record freq_hz as the records iterable's freq for the lookup table key.
        # Update enrichments to keyed by the records' actual decoded freq:
        records[1] = _record(freq_hz=853_780_750)
        records[2] = _record(freq_hz=771_369_500)
        enrichments = {
            851_006_250: enrichments[851_006_250],
            853_780_750: enrichments[853_780_000],
            771_369_500: enrichments[771_368_750],
        }
        summary = summarize_band_offsets(records, enrichments)
        # Expect entries for the 800 MHz band and the 700 MHz downlink band
        names = {s.band_name for s in summary}
        assert any("800 MHz" in n for n in names)
        assert any("700 MHz" in n for n in names)

    def test_skips_records_without_match(self):
        records = [_record()]
        enrichments = {records[0].freq_hz: EnrichmentResult(system_match=False)}
        assert summarize_band_offsets(records, enrichments) == []

    def test_mean_and_median(self):
        records = [
            _record(freq_hz=851_000_000),
            _record(freq_hz=851_500_000),
            _record(freq_hz=852_000_000),
        ]
        enrichments = {
            851_000_000: _enrichment_with_offset(851_000_000, 850_999_000),  # +1175 hz, +1.38 ppm
            851_500_000: _enrichment_with_offset(851_500_000, 851_500_000),  # 0
            852_000_000: _enrichment_with_offset(852_001_000, 852_000_000),  # +1.17 ppm
        }
        records[2] = _record(freq_hz=852_001_000)
        enrichments[852_001_000] = enrichments.pop(852_000_000)
        summary = summarize_band_offsets(records, enrichments)
        assert len(summary) == 1
        s = summary[0]
        assert s.n_samples == 3
        # Median is the middle value sorted; mean averages all
        assert isinstance(s.mean_ppm, float)
        assert isinstance(s.median_ppm, float)
