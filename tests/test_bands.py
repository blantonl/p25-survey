from p25_survey.bands import (
    FALLBACK_STEP_HZ,
    default_step_hz,
    describe_band,
    find_band,
)


class TestFindBand:
    def test_vhf_lower_edge(self):
        assert find_band(150_000_000).name.startswith("VHF")

    def test_vhf_upper_edge(self):
        assert find_band(174_000_000).name.startswith("VHF")

    def test_uhf_middle(self):
        assert find_band(450_000_000).name.startswith("UHF")

    def test_700_ps_downlink(self):
        # 769-775 MHz is the narrowband DOWNLINK (where CCs are)
        assert "downlink" in find_band(770_000_000).name
        assert "700 MHz" in find_band(770_000_000).name

    def test_700_ps_uplink(self):
        # 799-805 MHz is the narrowband UPLINK
        assert "uplink" in find_band(800_000_000).name
        assert "700 MHz" in find_band(800_000_000).name

    def test_800_band(self):
        assert find_band(851_012_500).name.startswith("800 MHz")

    def test_outside_known_bands_returns_none(self):
        assert find_band(28_500_000) is None  # 10m ham
        assert find_band(2_400_000_000) is None  # 2.4 GHz


class TestDefaultStepHz:
    def test_vhf_default_is_7500(self):
        assert default_step_hz(155_000_000, 160_000_000) == 7_500

    def test_uhf_default_is_12500(self):
        assert default_step_hz(450_000_000, 460_000_000) == 12_500

    def test_700ps_downlink_default_is_6250(self):
        assert default_step_hz(770_000_000, 775_000_000) == 6_250

    def test_700ps_uplink_default_is_6250(self):
        assert default_step_hz(800_000_000, 803_000_000) == 6_250

    def test_800_default_is_12500(self):
        assert default_step_hz(851_000_000, 869_000_000) == 12_500

    def test_unknown_band_falls_back(self):
        assert default_step_hz(28_500_000, 29_000_000) == FALLBACK_STEP_HZ

    def test_start_band_wins_when_straddling(self):
        # 800 (12.5) → into a hypothetical other; start side wins
        assert default_step_hz(851_000_000, 900_000_000) == 12_500


class TestDescribeBand:
    def test_known_band_describes(self):
        assert "800 MHz" in describe_band(851_012_500)

    def test_unknown_band_mentions_fallback(self):
        msg = describe_band(28_500_000)
        assert "unknown" in msg.lower()
        assert str(FALLBACK_STEP_HZ) in msg
