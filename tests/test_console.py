"""Tests for the live console title formatting.

The rich Live table itself is hard to test from outside, but the title
string format — which Russ flagged for under-reporting progress when
--hide-no-cc is on — is a pure function we can exercise directly.
"""

from __future__ import annotations

from p25_survey.console import _phase2_title


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
