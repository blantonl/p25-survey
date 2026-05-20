"""Tests for the live console title formatting.

The rich Live table itself is hard to test from outside, but the title
string format — which Russ flagged for under-reporting progress when
--hide-no-cc is on — is a pure function we can exercise directly.
"""

from __future__ import annotations

from p25_survey.console import _phase2_title, _rr_cell
from p25_survey.survey import SurveyRecord


def _record(nac=None, rr=None) -> SurveyRecord:
    return SurveyRecord(freq_hz=851_012_500, wacn=0xBEE00, sysid=0x1B2,
                        nac=nac, rfss_id=2, site_id=29, rr=rr)


class TestPhase2Title:
    def test_basic_format(self):
        title = _phase2_title(checked=27, total=27, controls=6, skipped=0)
        assert title == ("Phase 2 — P25 decode  "
                         "[27/27 done, "
                         "6 control channels found, "
                         "0 skipped via resume]")

    def test_progress_in_flight(self):
        title = _phase2_title(checked=10, total=27, controls=2, skipped=0)
        assert "[10/27 done" in title
        assert "2 control channels found" in title

    def test_with_resume_skips(self):
        title = _phase2_title(checked=20, total=27, controls=4, skipped=7)
        assert "7 skipped via resume" in title

    def test_zero_controls_zero_progress(self):
        title = _phase2_title(checked=0, total=100, controls=0, skipped=0)
        assert "[0/100 done" in title
        assert "0 control channels found" in title


class TestRRCell:
    """Russ flagged that only NEW SYS / new RFSS were annotated. Other
    submission-worthy findings (freq mismatch, NAC mismatch, neighbor
    flags) need to surface in the live row too, not just the markdown
    submission report."""

    def _full_match_rr(self, **overrides):
        rr = {
            "system_match": True,
            "site_match": True,
            "rr_system_name": "Kansas Statewide Interoperability",
            "rr_site_description": "Zurich",
            "rr_site_nac": "1BB",
            "cc_freq_offset": {"offset_hz": 0, "ppm": 0.0,
                               "decoded_hz": 851_012_500, "expected_hz": 851_012_500},
            "cc_freq_in_db": True,
            "neighbors_decoded_not_in_rr": [],
            "neighbor_cc_mismatches": [],
        }
        rr.update(overrides)
        return rr

    def test_no_enrichment(self):
        assert _rr_cell(_record(rr=None)) == ""

    def test_unknown_system(self):
        rr = {"system_match": False}
        assert _rr_cell(_record(rr=rr)) == "NEW SYS (not in RR)"

    def test_new_rfss_site(self):
        rr = {"system_match": True, "site_match": False,
              "rr_system_name": "Kansas Statewide Interoperability"}
        # name is truncated to 30 chars
        cell = _rr_cell(_record(rr=rr))
        assert cell.endswith("· new RFSS/Site")
        assert "Kansas Statewide" in cell

    def test_full_match_clean(self):
        rr = self._full_match_rr()
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert cell.startswith("✓ ")
        assert "Zurich" in cell
        assert "+0.00 ppm" in cell
        assert "NAC" not in cell  # no mismatch flag
        assert "nbr" not in cell

    def test_freq_mismatch_flag(self):
        # decoded freq off the listed CC by 1234 Hz
        rr = self._full_match_rr(cc_freq_in_db=False,
                                 cc_freq_offset={"offset_hz": 1234, "ppm": 1.45,
                                                 "decoded_hz": 851_012_500,
                                                 "expected_hz": 851_011_266})
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "FREQ≠" in cell
        assert "off +1234 Hz" in cell
        # ppm shouldn't appear when we're showing the raw offset
        assert "ppm" not in cell

    def test_nac_missing_in_rr(self):
        # Russ's Zurich case: RR has no NAC listed, decoded NAC is fine
        rr = self._full_match_rr(rr_site_nac="")
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "NAC?" in cell

    def test_nac_differs(self):
        rr = self._full_match_rr(rr_site_nac="2A0")
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "NAC≠" in cell

    def test_new_neighbor_candidates(self):
        rr = self._full_match_rr(neighbors_decoded_not_in_rr=[{"freq_hz": 851_000_000,
                                                                "rfss_id": 2,
                                                                "site_id": 99}])
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "+1 nbr" in cell

    def test_neighbor_cc_mismatch(self):
        rr = self._full_match_rr(neighbor_cc_mismatches=[{"kind": "not_listed"},
                                                         {"kind": "not_marked_control"}])
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "2 nbr CC≠" in cell

    def test_secondary_cc_mismatch(self):
        # Russ's request: SCCB secondary CC vs RR gets its own flag.
        rr = self._full_match_rr(secondary_cc_mismatches=[
            {"freq_hz": 851_500_000, "kind": "missing_from_rr", "rr_use_code": None},
        ])
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "1 sec CC≠" in cell

    def test_multiple_flags_combine(self):
        rr = self._full_match_rr(
            rr_site_nac="",
            secondary_cc_mismatches=[{"freq_hz": 851_500_000, "kind": "missing_from_rr"}],
            neighbors_decoded_not_in_rr=[{"freq_hz": 851_000_000, "rfss_id": 2, "site_id": 99}],
            neighbor_cc_mismatches=[{"kind": "not_listed"}],
        )
        cell = _rr_cell(_record(nac=0x1BB, rr=rr))
        assert "NAC?" in cell
        assert "1 sec CC≠" in cell
        assert "+1 nbr" in cell
        assert "1 nbr CC≠" in cell
