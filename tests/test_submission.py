"""Tests for the markdown submission report."""

from __future__ import annotations

from pathlib import Path

from p25_survey.enrich import (
    EnrichmentResult,
    FreqOffset,
    NeighborCCMismatch,
    NeighborRef,
    SecondaryCCMismatch,
)
from p25_survey.submission import render, render_file
from p25_survey.survey import IdenUpEntry, NeighborSite, SignalQuality, SurveyRecord


def _record(freq_hz=851_006_250, wacn=0xBEE00, sysid=0x1A4, nac=0x293,
            rfss_id=1, site_id=7, neighbors_hz=None, idens=0,
            secondary_cc=None):
    iden_up = [
        IdenUpEntry(iden=i, base_freq_hz=851_006_250, step_hz=12_500, offset_hz=-45_000_000)
        for i in range(idens)
    ]
    return SurveyRecord(
        freq_hz=freq_hz, complete=True,
        wacn=wacn, sysid=sysid, nac=nac,
        rfss_id=rfss_id, site_id=site_id,
        iden_up=iden_up,
        neighbors=[
            NeighborSite(freq_hz=f, rfss_id=1, site_id=8) for f in (neighbors_hz or [])
        ],
        secondary_cc=secondary_cc or [],
        signal=SignalQuality(rssi_dbfs_mean=-15.0, ber_pct_mean=0.0),
    )


# ---------------------------------------------------------------------------
# Empty / clean cases
# ---------------------------------------------------------------------------


class TestNothingToSubmit:
    def test_empty_records(self):
        text = render([], {})
        assert "Records scanned: **0**" in text
        assert "Nothing to submit" in text

    def test_all_match_clean(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X", rr_sid=1,
            rr_site_nac="293",
            cc_freq_offset=FreqOffset(decoded_hz=851_006_250,
                                      expected_hz=851_006_250,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "Nothing to submit" in text
        assert "## New systems" not in text
        assert "## New sites" not in text


# ---------------------------------------------------------------------------
# Section: new systems
# ---------------------------------------------------------------------------


class TestNewSystems:
    def test_renders_unmatched_system(self):
        rec = _record(neighbors_hz=[851_106_250, 851_206_250])
        enr = EnrichmentResult(system_match=False)
        text = render([rec], {rec.freq_hz: enr})
        assert "## New systems" in text
        assert "WACN BEE00" in text
        assert "SYSID 1A4" in text
        assert "NAC 293" in text
        assert "851.10625 MHz" in text  # neighbor listed
        assert "851.20625 MHz" in text

    def test_skips_partial_records(self):
        rec = _record(wacn=None)  # partial: no WACN
        enr = EnrichmentResult()
        text = render([rec], {rec.freq_hz: enr})
        assert "## New systems" not in text

    def test_skips_incomplete_decode(self):
        """Even with WACN/SYSID decoded, complete=False means low confidence — skip."""
        rec = _record()
        rec.complete = False
        enr = EnrichmentResult(system_match=False)
        text = render([rec], {rec.freq_hz: enr})
        assert "## New systems" not in text
        assert "New systems candidates: **0**" in text


# ---------------------------------------------------------------------------
# Section: new sites
# ---------------------------------------------------------------------------


class TestNewSites:
    def test_renders_known_system_unknown_site(self):
        rec = _record(idens=2)
        enr = EnrichmentResult(
            system_match=True, site_match=False,
            rr_system_name="State Public Safety", rr_sid=42,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New sites" in text
        assert "State Public Safety" in text
        assert "RFSS 1 / Site 7" in text
        assert "851.00625 MHz" in text
        assert "Band plan (2 entries)" in text
        # SYSID is included alongside WACN/NAC for unambiguous identification
        assert "SYSID: 1A4" in text

    def test_skips_when_rfss_site_undecoded(self):
        """Russ's case: weak signal, RFSS/Site fields blank — must not be reported as new site."""
        rec = _record(rfss_id=None, site_id=None)
        enr = EnrichmentResult(
            system_match=True, site_match=False,
            rr_system_name="Mississippi Wireless Information Network (MSWIN)",
            rr_sid=4879,
            notes=["RFSS/Site not decoded; can't match site-level data"],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New sites" not in text
        assert "RFSS None / Site None" not in text
        assert "New sites candidates: **0**" in text

    def test_skips_partial_decode(self):
        """Partial decodes (complete=False) are too low-confidence for submission."""
        rec = _record()
        rec.complete = False
        enr = EnrichmentResult(
            system_match=True, site_match=False,
            rr_system_name="State Public Safety", rr_sid=42,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New sites" not in text
        assert "New sites candidates: **0**" in text

    def test_skips_ambiguous_subsystem_match(self):
        """Multi-subsystem ambiguity isn't a new site — don't submit it."""
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=False, ambiguous_site=True,
            rr_system_name="Alabama Interoperable Radio System (AIRS)",
            rr_sid=7258,
            notes=["ambiguous site: RFSS 5 / Site 5 matches multiple subsystems"],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New sites" not in text
        assert "New sites candidates: **0**" in text


# ---------------------------------------------------------------------------
# Section: frequency mismatches
# ---------------------------------------------------------------------------


class TestFreqMismatch:
    def test_renders_offset(self):
        rec = _record(freq_hz=851_009_000)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="State Public Safety",
            rr_site_description="Downtown",
            cc_freq_offset=FreqOffset(decoded_hz=851_009_000,
                                      expected_hz=851_006_250,
                                      offset_hz=2750, ppm=3.231),
            cc_freq_in_db=False,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Frequency mismatches" in text
        assert "Downtown" in text
        assert "RR has:" in text
        assert "851.00625 MHz" in text
        assert "851.00900 MHz" in text
        assert "+2750 Hz" in text
        assert "+3.231 ppm" in text

    def test_annotates_rr_nac_when_it_differs(self):
        """When RR's site NAC disagrees with the decoded NAC, surface it
        in the freq-mismatch entry — this is the diagnostic Russ asked for."""
        rec = _record(freq_hz=851_009_000, nac=0x293)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="State Public Safety",
            rr_site_description="Downtown",
            rr_site_nac="2A0",
            cc_freq_offset=FreqOffset(decoded_hz=851_009_000,
                                      expected_hz=851_006_250,
                                      offset_hz=2750, ppm=3.231),
            cc_freq_in_db=False,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "Decoded WACN/SYSID/NAC" in text
        assert "RR site NAC: 2A0" in text

    def test_omits_rr_nac_when_it_matches(self):
        rec = _record(freq_hz=851_009_000, nac=0x293)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="State Public Safety",
            rr_site_description="Downtown",
            rr_site_nac="293",
            cc_freq_offset=FreqOffset(decoded_hz=851_009_000,
                                      expected_hz=851_006_250,
                                      offset_hz=2750, ppm=3.231),
            cc_freq_in_db=False,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "RR site NAC" not in text


# ---------------------------------------------------------------------------
# Section: site NAC mismatches
# ---------------------------------------------------------------------------


class TestSiteNacMismatch:
    """Russ Mason's request: surface NAC mismatches even when freq matches.

    Two cases: RR has no NAC populated for the site (many systems carry a
    "PLEASE SUBMIT" note about missing NACs), and RR has a NAC that
    disagrees with what we decoded.
    """

    def test_renders_when_rr_nac_differs(self):
        # Russ's MSWIN example: decoded 2A0, RR has 2A1, freq matches.
        rec = _record(freq_hz=854_212_500, nac=0x2A0, rfss_id=4, site_id=1)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="Mississippi Wireless Information Network (MSWIN)",
            rr_site_description="DeSoto County Simulcast",
            rr_site_nac="2A1",
            cc_freq_offset=FreqOffset(decoded_hz=854_212_500,
                                      expected_hz=854_212_500,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" in text
        assert "Mississippi Wireless Information Network" in text
        assert "DeSoto County Simulcast" in text
        assert "RFSS 4 / Site 1" in text
        assert "Decoded NAC: **2A0**" in text
        assert "RR site NAC: **2A1**" in text
        assert "submit a correction" in text
        assert "Site NAC mismatches: **1**" in text

    def test_renders_when_rr_nac_missing(self):
        # Russ's Entergy example: decoded 640, RR has nothing for the site.
        rec = _record(freq_hz=855_262_500, nac=0x640, rfss_id=2, site_id=65)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="Entergy",
            rr_site_description="Cleveland, MS",
            rr_site_nac=None,
            cc_freq_offset=FreqOffset(decoded_hz=855_262_500,
                                      expected_hz=855_262_500,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" in text
        assert "Entergy" in text
        assert "Cleveland, MS" in text
        assert "Decoded NAC: **640**" in text
        assert "*(not listed)*" in text
        assert "submit the decoded NAC" in text

    def test_treats_blank_string_as_missing(self):
        rec = _record(nac=0x640)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            rr_site_nac="   ",  # whitespace-only counts as not listed
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" in text
        assert "*(not listed)*" in text

    def test_omits_when_nacs_match(self):
        rec = _record(nac=0x293)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            rr_site_nac="293",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" not in text
        assert "Site NAC mismatches: **0**" in text

    def test_normalizes_leading_zeros_and_case(self):
        # RR sometimes has "0x293" or "293" with no padding; decoded is always 3-hex.
        rec = _record(nac=0x293)
        enr = EnrichmentResult(
            system_match=True, site_match=True, rr_system_name="X",
            rr_site_nac="0293",  # leading zero — still equal to 0x293
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" not in text

    def test_skips_when_decoded_nac_missing(self):
        rec = _record(nac=None)
        enr = EnrichmentResult(
            system_match=True, site_match=True, rr_system_name="X",
            rr_site_nac="293",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        # Without a decoded NAC, we can't claim mismatch.
        assert "## Site NAC mismatches" not in text

    def test_skips_partial_decode(self):
        rec = _record(nac=0x640)
        rec.complete = False
        enr = EnrichmentResult(
            system_match=True, site_match=True, rr_system_name="X",
            rr_site_nac="2A1",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" not in text
        assert "Site NAC mismatches: **0**" in text

    def test_skips_when_no_site_match(self):
        # New-site case — the NAC mismatch isn't meaningful (no RR site to mismatch with).
        rec = _record(nac=0x640)
        enr = EnrichmentResult(
            system_match=True, site_match=False,
            rr_system_name="X", rr_site_nac=None,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Site NAC mismatches" not in text


# ---------------------------------------------------------------------------
# Section: neighbor diffs
# ---------------------------------------------------------------------------


class TestNewSiteCandidates:
    def test_renders_extra_decoded_neighbor(self):
        rec = _record(neighbors_hz=[851_106_250, 860_500_000])
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            neighbors_decoded_not_in_rr=[
                NeighborRef(rfss_id=1, site_id=99, freq_hz=860_500_000, in_rr=False),
            ],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New-site neighbor candidates" in text
        assert "RFSS 1 / Site 99" in text
        assert "860.50000 MHz" in text


class TestNeighborCCMismatchSection:
    def test_renders_missing_from_rr(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            neighbor_cc_mismatches=[
                NeighborCCMismatch(
                    rfss_id=1, site_id=8, advertised_cc_hz=851_300_000,
                    kind="missing_from_rr", rr_site_description="Hilltop",
                ),
            ],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Neighbor control-channel mismatches" in text
        assert "RFSS 1 / Site 8" in text
        assert "Hilltop" in text
        assert "851.30000 MHz" in text
        assert "not in the site's RR frequency list" in text

    def test_renders_not_marked_control(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            neighbor_cc_mismatches=[
                NeighborCCMismatch(
                    rfss_id=1, site_id=8, advertised_cc_hz=851_106_250,
                    kind="not_marked_control", rr_use_code="",
                ),
            ],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Neighbor control-channel mismatches" in text
        assert "should be \"d\" or \"a\"" in text


class TestObservedNeighborSection:
    def test_lists_observed_neighbors_with_rr_context(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            observed_neighbors=[
                NeighborRef(rfss_id=1, site_id=8, freq_hz=851_106_250, in_rr=True,
                            description="Hilltop", location="Anytown",
                            county="County A"),
                NeighborRef(rfss_id=1, site_id=99, freq_hz=860_500_000, in_rr=False),
            ],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Observed neighbor roster" in text
        assert "Hilltop" in text
        assert "Anytown" in text
        assert "County A" in text
        assert "RFSS 1 / Site 99" in text
        assert "not in RR roster" in text


class TestSecondaryCCMismatchSection:
    """Russ Mason's request: SCCB secondary CCs cross-checked against the
    site's own RR frequency list, surfaced for existing and new sites."""

    def test_renders_missing_from_rr(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True, rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            secondary_cc_mismatches=[
                SecondaryCCMismatch(freq_hz=851_500_000, kind="missing_from_rr"),
            ],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Secondary control-channel mismatches" in text
        assert "851.50000 MHz" in text
        assert "not in the site's RR frequency list" in text
        assert "Sites with secondary CC mismatches: **1**" in text

    def test_renders_not_marked_control(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True, rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            secondary_cc_mismatches=[
                SecondaryCCMismatch(freq_hz=851_500_000, kind="not_marked_control",
                                    rr_use_code=""),
            ],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Secondary control-channel mismatches" in text
        assert "should be \"d\" or \"a\"" in text

    def test_omits_section_when_clean(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True, rr_system_name="X",
            rr_site_nac="293",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Secondary control-channel mismatches" not in text
        assert "Sites with secondary CC mismatches: **0**" in text

    def test_new_site_lists_secondary_cc(self):
        # For a not-yet-listed site, the secondary CCs ride along in the
        # new-site entry so whoever adds the site includes every CC.
        rec = _record(secondary_cc=[851_500_000])
        enr = EnrichmentResult(
            system_match=True, site_match=False, rr_system_name="Statewide",
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New sites" in text
        assert "Secondary control channels (SCCB)" in text
        assert "851.50000 MHz" in text


# ---------------------------------------------------------------------------
# Per-band offset summary
# ---------------------------------------------------------------------------


class TestBandSummary:
    def test_renders_per_band_table(self):
        rec1 = _record(freq_hz=851_007_000)  # 800 MHz, +750 Hz
        rec2 = _record(freq_hz=771_369_500)  # 700 MHz, +750 Hz
        e1 = EnrichmentResult(
            system_match=True, site_match=True,
            cc_freq_offset=FreqOffset(decoded_hz=851_007_000,
                                      expected_hz=851_006_250,
                                      offset_hz=750, ppm=0.881),
            cc_freq_in_db=True,
        )
        e2 = EnrichmentResult(
            system_match=True, site_match=True,
            cc_freq_offset=FreqOffset(decoded_hz=771_369_500,
                                      expected_hz=771_368_750,
                                      offset_hz=750, ppm=0.972),
            cc_freq_in_db=True,
        )
        text = render([rec1, rec2], {rec1.freq_hz: e1, rec2.freq_hz: e2})
        assert "## SDR clock calibration" in text
        assert "800 MHz" in text
        assert "700 MHz" in text
        # 2 sites is below the recommendation threshold
        assert "Not enough matched sites" in text


# ---------------------------------------------------------------------------
# File round-trip
# ---------------------------------------------------------------------------


class TestRenderFile:
    def test_writes_file(self, tmp_path: Path):
        rec = _record()
        enr = EnrichmentResult(system_match=False)
        path = tmp_path / "submissions.md"
        render_file([rec], {rec.freq_hz: enr}, str(path))
        text = path.read_text()
        assert "## New systems" in text
