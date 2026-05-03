"""Tests for the RR enrichment matching logic.

Uses a fake RRClient (just the methods enrich.py calls) so no SOAP machinery
needs to be involved.
"""

from __future__ import annotations

from p25_survey.enrich import (
    BandOffsetSummary,
    EnrichmentResult,
    NeighborCCMismatch,
    NeighborRef,
    enrich_record,
    recommend_ppm,
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
    """Stand-in for RRClient with just the methods enrich uses.

    Single-system convenience: pass `system=` and `sites=`. For the
    multi-system case (multiple RR systems sharing a WACN/SYSID), pass
    `systems=[...]` and `sites_by_sid={sid: [RRSite, ...]}`.
    """

    def __init__(self, system: RRSystem | None = None,
                 sites: list[RRSite] | None = None,
                 systems: list[RRSystem] | None = None,
                 sites_by_sid: dict[int, list[RRSite]] | None = None,
                 raise_on_find: Exception | None = None,
                 raise_on_sites: Exception | None = None) -> None:
        if systems is not None:
            self._systems = systems
        elif system is not None:
            self._systems = [system]
        else:
            self._systems = []
        if sites_by_sid is not None:
            self._sites_by_sid = sites_by_sid
        else:
            self._sites_by_sid = {}
            if self._systems and sites is not None:
                self._sites_by_sid[self._systems[0].sid] = sites
        self._sites_default = sites or []
        self._raise_find = raise_on_find
        self._raise_sites = raise_on_sites

    def find_systems_by_wacn_sysid(self, wacn_hex: str, sysid_hex: str) -> list[RRSystem]:
        if self._raise_find:
            raise self._raise_find
        return list(self._systems)

    def get_sites(self, sid: int) -> list[RRSite]:
        if self._raise_sites:
            raise self._raise_sites
        if sid in self._sites_by_sid:
            return self._sites_by_sid[sid]
        return self._sites_default


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
          description: str = "Site Foo",
          nac: str = "") -> RRSite:
    fs = [RRSiteFreq(freq_hz=f, use="d") for f in freqs_hz]
    fs += [RRSiteFreq(freq_hz=f, use="a") for f in (alt_freqs_hz or [])]
    return RRSite(
        sid=42, site_db_id=site_number * 100, site_number=site_number, rfss=rfss,
        nac=nac,
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


class TestMultiSubsystemDisambiguation:
    """Russ's bug: an RR sid covering multiple WACN/SYSID subsystems
    can have the same RFSS/Site number on each subsystem. Without NAC
    disambiguation, _find_site returns whichever subsystem appears first
    and the wrong site is matched."""

    def test_nac_picks_correct_subsystem(self):
        # AIRS-like layout: sid=42 holds two subsystems, both with RFSS=5/Site=5.
        # The decoded NAC should pick the right one.
        sites = [
            _site(rfss=5, site_number=5, freqs_hz=[773_068_750],
                  description="Saraland", nac="2A0"),  # SYSID 00A subsystem
            _site(rfss=5, site_number=5, freqs_hz=[770_356_250],
                  description="Mercedes (Vance)", nac="465"),  # SYSID 46B subsystem
        ]
        client = FakeClient(system=_system(), sites=sites)
        rec = _record(freq_hz=770_356_250, rfss_id=5, site_id=5)
        rec.nac = 0x465
        result = enrich_record(rec, client)
        assert result.site_match
        assert result.rr_site_description == "Mercedes (Vance)"
        assert result.rr_site_nac == "465"
        assert result.cc_freq_in_db  # picked the right site → freq matches

    def test_nac_mismatch_yields_ambiguous_note(self):
        # Both candidate sites exist but neither matches the decoded NAC.
        # We should refuse to pick (no site_match) and explain why.
        sites = [
            _site(rfss=5, site_number=5, freqs_hz=[773_068_750],
                  description="Saraland", nac="2A0"),
            _site(rfss=5, site_number=5, freqs_hz=[770_356_250],
                  description="Mercedes (Vance)", nac="465"),
        ]
        client = FakeClient(system=_system(), sites=sites)
        rec = _record(rfss_id=5, site_id=5)
        rec.nac = 0x999  # matches neither candidate
        result = enrich_record(rec, client)
        assert not result.site_match
        assert result.ambiguous_site
        assert any("ambiguous site" in n for n in result.notes)
        # Note should list candidate NACs so an admin can debug.
        note = " ".join(result.notes)
        assert "2A0" in note and "465" in note

    def test_single_match_still_works_without_nac(self):
        # When only one site matches RFSS/Site, NAC isn't required to pick.
        # Preserves behavior for the (very common) single-subsystem case
        # where RR's site.nac field is blank.
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250], nac="")]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(_record(), client)
        assert result.site_match


class TestMultiSystemSameWacnSysid:
    """Russ's third bug: multiple RR systems can share the same WACN/SYSID
    (e.g. two MS county networks both running 92448/00A). Old code picked
    the first system returned and reported a false-positive "new site"
    submission against it. New behavior: try each candidate, pick the one
    whose sites contain the decoded RFSS/Site, flag ambiguous when
    disambiguation isn't possible."""

    def _two_county_systems(self):
        return [
            RRSystem(sid=8004, name="Oktibbeha County", sys_type=2, sys_flavor=3,
                     sysid_entries=[RRSysidEntry(wacn="92448", sysid="00A")]),
            RRSystem(sid=8005, name="Noxubee County P25", sys_type=2, sys_flavor=3,
                     sysid_entries=[RRSysidEntry(wacn="92448", sysid="00A")]),
        ]

    def test_picks_system_that_has_the_site(self):
        # Russ's actual case: RFSS 9 / Site 9 is the Macon site on Noxubee
        # (sid=8005). Oktibbeha (sid=8004) has different sites. We should
        # match against Noxubee, not falsely report a new site for Oktibbeha.
        systems = self._two_county_systems()
        sites_by_sid = {
            8004: [_site(rfss=1, site_number=1, freqs_hz=[852_400_000],
                         description="Starkville")],
            8005: [_site(rfss=9, site_number=9, freqs_hz=[852_400_000],
                         description="Macon")],
        }
        client = FakeClient(systems=systems, sites_by_sid=sites_by_sid)
        rec = _record(freq_hz=852_400_000, wacn=0x92448, sysid=0x00A,
                      rfss_id=9, site_id=9)
        result = enrich_record(rec, client)
        assert result.system_match
        assert result.site_match
        assert result.rr_sid == 8005
        assert result.rr_system_name == "Noxubee County P25"
        assert result.rr_site_description == "Macon"
        assert result.cc_freq_in_db

    def test_ambiguous_when_no_system_has_the_site(self):
        # Hypothetical: RFSS 9 / Site 9 truly isn't in either system's
        # roster. Old code would've reported "new site for Oktibbeha"
        # (whichever returned first). We can't tell which system the new
        # site belongs to — flag rather than guess.
        systems = self._two_county_systems()
        sites_by_sid = {
            8004: [_site(rfss=1, site_number=1, freqs_hz=[852_400_000])],
            8005: [_site(rfss=2, site_number=2, freqs_hz=[852_500_000])],
        }
        client = FakeClient(systems=systems, sites_by_sid=sites_by_sid)
        rec = _record(wacn=0x92448, sysid=0x00A, rfss_id=9, site_id=9)
        result = enrich_record(rec, client)
        assert result.system_match
        assert not result.site_match
        assert result.ambiguous_site
        note = " ".join(result.notes)
        assert "Oktibbeha County" in note
        assert "Noxubee County P25" in note
        assert "isn't in any" in note

    def test_ambiguous_when_multiple_systems_have_the_site(self):
        # Pathological but worth catching: both candidate systems have a
        # site at RFSS/Site (and NAC matches both). We can't pick between
        # them — refuse to silently match one.
        systems = self._two_county_systems()
        sites_by_sid = {
            8004: [_site(rfss=9, site_number=9, freqs_hz=[852_400_000],
                         description="Some Oktibbeha site", nac="00A")],
            8005: [_site(rfss=9, site_number=9, freqs_hz=[852_500_000],
                         description="Macon", nac="00A")],
        }
        client = FakeClient(systems=systems, sites_by_sid=sites_by_sid)
        rec = _record(wacn=0x92448, sysid=0x00A, rfss_id=9, site_id=9)
        rec.nac = 0x00A
        result = enrich_record(rec, client)
        assert result.ambiguous_site
        note = " ".join(result.notes)
        assert "matches multiple RR systems" in note

    def test_single_system_no_site_still_reports_new_site(self):
        # Regression check: when only ONE candidate system exists and the
        # RFSS/Site isn't there, that's a genuine new-site case (the
        # original v0.2/v0.3 behavior we don't want to break).
        systems = [RRSystem(sid=42, name="Solo System", sys_type=2, sys_flavor=3,
                            sysid_entries=[RRSysidEntry(wacn="BEE00", sysid="1A4")])]
        sites_by_sid = {42: [_site(rfss=1, site_number=1, freqs_hz=[851_006_250])]}
        client = FakeClient(systems=systems, sites_by_sid=sites_by_sid)
        rec = _record(rfss_id=9, site_id=9)
        result = enrich_record(rec, client)
        assert result.system_match
        assert not result.site_match
        assert not result.ambiguous_site
        assert any("new site" in n for n in result.notes)


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


class TestObservedNeighbors:
    def test_neighbors_match(self):
        sites = [
            _site(rfss=1, site_number=7, freqs_hz=[851_006_250]),
            _site(rfss=1, site_number=8, freqs_hz=[851_106_250],
                  description="Site Eight"),
            _site(rfss=1, site_number=9, freqs_hz=[851_206_250],
                  description="Site Nine"),
        ]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=851_106_250, rfss_id=1, site_id=8),
                NeighborSite(freq_hz=851_206_250, rfss_id=1, site_id=9),
            ]),
            client,
        )
        assert len(result.observed_neighbors) == 2
        assert all(n.in_rr for n in result.observed_neighbors)
        assert result.neighbors_decoded_not_in_rr == []
        assert result.neighbor_cc_mismatches == []
        # RR data is attached for spot-checking
        n8 = next(n for n in result.observed_neighbors if n.site_id == 8)
        assert n8.description == "Site Eight"
        assert n8.location == "City"
        assert n8.county == "County"

    def test_unobserved_rr_sites_no_longer_listed(self):
        # Regression: previously RR roster minus ADJ_STS_BCST produced a
        # massive "informational" list. Now we only enumerate observed
        # neighbors; unobserved RR-roster sites do not appear.
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
        assert [n.site_id for n in result.observed_neighbors] == [8]
        assert result.neighbors_decoded_not_in_rr == []

    def test_we_decoded_an_unknown_neighbor(self):
        # ADJ_STS_BCST advertised RFSS 1 / Site 99; RR has no such site.
        # Goes into both observed_neighbors (in_rr=False) and
        # neighbors_decoded_not_in_rr.
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=860_500_000, rfss_id=1, site_id=99),
            ]),
            client,
        )
        assert len(result.observed_neighbors) == 1
        assert result.observed_neighbors[0].in_rr is False
        assert len(result.neighbors_decoded_not_in_rr) == 1
        nref = result.neighbors_decoded_not_in_rr[0]
        assert nref.rfss_id == 1
        assert nref.site_id == 99
        assert nref.freq_hz == 860_500_000

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
        assert result.neighbor_cc_mismatches == []


class TestNeighborCCMismatches:
    def test_advertised_cc_missing_from_rr(self):
        # Neighbor site exists in RR with a CC at 851.10625, but
        # ADJ_STS_BCST advertises a different CC at 851.300 — that
        # frequency isn't in the site's RR frequency list at all.
        sites = [
            _site(rfss=1, site_number=7, freqs_hz=[851_006_250]),
            _site(rfss=1, site_number=8, freqs_hz=[851_106_250],
                  description="Site Eight"),
        ]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=851_300_000, rfss_id=1, site_id=8),
            ]),
            client,
        )
        assert len(result.neighbor_cc_mismatches) == 1
        m = result.neighbor_cc_mismatches[0]
        assert m.kind == "missing_from_rr"
        assert m.rfss_id == 1 and m.site_id == 8
        assert m.advertised_cc_hz == 851_300_000
        assert m.rr_site_description == "Site Eight"

    def test_advertised_cc_listed_but_not_marked_control(self):
        # Neighbor's RR frequency list includes the advertised freq, but
        # it's tagged use="" (traffic) instead of "d"/"a".
        from p25_survey.radioreference import RRSite, RRSiteFreq
        site_8 = RRSite(
            sid=42, site_db_id=800, site_number=8, rfss=1,
            description="Site Eight",
            frequencies=[
                RRSiteFreq(freq_hz=851_106_250, use=""),  # mistagged
            ],
        )
        sites = [
            _site(rfss=1, site_number=7, freqs_hz=[851_006_250]),
            site_8,
        ]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=851_106_250, rfss_id=1, site_id=8),
            ]),
            client,
        )
        assert len(result.neighbor_cc_mismatches) == 1
        m = result.neighbor_cc_mismatches[0]
        assert m.kind == "not_marked_control"
        assert m.rr_use_code == ""

    def test_unknown_neighbor_does_not_produce_cc_mismatch(self):
        # New-site candidates (in_rr=False) shouldn't generate CC
        # mismatches — there's no RR site to compare against.
        sites = [_site(rfss=1, site_number=7, freqs_hz=[851_006_250])]
        client = FakeClient(system=_system(), sites=sites)
        result = enrich_record(
            _record(neighbors=[
                NeighborSite(freq_hz=860_500_000, rfss_id=1, site_id=99),
            ]),
            client,
        )
        assert result.neighbor_cc_mismatches == []
        assert len(result.neighbors_decoded_not_in_rr) == 1


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


# ---------------------------------------------------------------------------
# --ppm recommendation
# ---------------------------------------------------------------------------


def _ppm_record(freq_hz: int, ppm: float) -> tuple[SurveyRecord, EnrichmentResult]:
    """Build a (record, enrichment) pair with a precise per-record ppm."""
    offset_hz = int(round(ppm * freq_hz / 1e6))
    expected_hz = freq_hz - offset_hz
    rec = _record(freq_hz=freq_hz)
    enr = _enrichment_with_offset(freq_hz, expected_hz)
    return rec, enr


class TestRecommendPpm:
    def test_insufficient_samples(self):
        rec1, e1 = _ppm_record(851_000_000, 1.0)
        rec2, e2 = _ppm_record(852_000_000, 1.1)
        rec = recommend_ppm([rec1, rec2], {rec1.freq_hz: e1, rec2.freq_hz: e2})
        assert rec.consistency == "insufficient_data"
        assert rec.median_ppm is None
        assert rec.iqr_ppm is None
        assert rec.n_samples == 2

    def test_high_consistency(self):
        # Three sites tightly clustered near +1.0 ppm.
        pairs = [
            _ppm_record(851_000_000, 0.95),
            _ppm_record(852_000_000, 1.00),
            _ppm_record(853_000_000, 1.05),
        ]
        records = [r for r, _ in pairs]
        enrichments = {r.freq_hz: e for r, e in pairs}
        rec = recommend_ppm(records, enrichments)
        assert rec.n_samples == 3
        assert rec.median_ppm is not None
        assert abs(rec.median_ppm - 1.0) < 0.01
        assert rec.consistency == "high"
        assert rec.cross_band_warning is None

    def test_low_consistency(self):
        # Wide spread → IQR > 0.5 ppm.
        pairs = [
            _ppm_record(851_000_000, -1.5),
            _ppm_record(852_000_000, 0.0),
            _ppm_record(853_000_000, 1.5),
            _ppm_record(854_000_000, 2.0),
        ]
        records = [r for r, _ in pairs]
        enrichments = {r.freq_hz: e for r, e in pairs}
        rec = recommend_ppm(records, enrichments)
        assert rec.consistency == "low"
        assert rec.iqr_ppm > 0.5

    def test_skips_records_without_match(self):
        # Records with no enrichment offset are ignored entirely.
        rec_match, e_match = _ppm_record(851_000_000, 1.0)
        rec_match2, e_match2 = _ppm_record(852_000_000, 1.0)
        rec_match3, e_match3 = _ppm_record(853_000_000, 1.0)
        rec_unmatched = _record(freq_hz=860_000_000)
        e_unmatched = EnrichmentResult(system_match=False)
        records = [rec_match, rec_match2, rec_match3, rec_unmatched]
        enrichments = {
            rec_match.freq_hz: e_match,
            rec_match2.freq_hz: e_match2,
            rec_match3.freq_hz: e_match3,
            rec_unmatched.freq_hz: e_unmatched,
        }
        rec = recommend_ppm(records, enrichments)
        assert rec.n_samples == 3

    def test_cross_band_divergence_warns(self):
        # Two bands, each with several samples, but their medians diverge
        # by >0.5 ppm — suspicious for a single-TCXO SDR.
        pairs_800 = [
            _ppm_record(851_000_000, -1.0),
            _ppm_record(852_000_000, -1.05),
            _ppm_record(853_000_000, -0.95),
        ]
        pairs_700 = [
            _ppm_record(771_000_000, 1.0),
            _ppm_record(772_000_000, 1.05),
            _ppm_record(773_000_000, 0.95),
        ]
        all_pairs = pairs_800 + pairs_700
        records = [r for r, _ in all_pairs]
        enrichments = {r.freq_hz: e for r, e in all_pairs}
        rec = recommend_ppm(records, enrichments)
        assert rec.cross_band_warning is not None
        assert "diverge" in rec.cross_band_warning

    def test_no_warning_when_only_one_band_has_enough(self):
        # 800 MHz has 3 samples; 700 MHz has only 1. With <2 bands at
        # ≥2 samples each, we can't compare reliably — no warning.
        pairs = [
            _ppm_record(851_000_000, -1.0),
            _ppm_record(852_000_000, -1.0),
            _ppm_record(853_000_000, -1.0),
            _ppm_record(771_000_000, 5.0),  # outlier in a band-of-one
        ]
        records = [r for r, _ in pairs]
        enrichments = {r.freq_hz: e for r, e in pairs}
        rec = recommend_ppm(records, enrichments)
        assert rec.cross_band_warning is None
