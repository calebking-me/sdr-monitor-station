from dataclasses import asdict

import pytest

from sdrscanner.config import MODE_AM, ChannelConfig
from sdrscanner.planner import plan_center
from sdrscanner.presets import PRESETS, PRESETS_BY_KEY


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.key)
def test_presets_are_valid_channels(preset):
    chans = preset.make_channels()
    assert len(chans) == len(preset.channels) > 0
    assert len({c.frequency for c in chans}) == len(chans)          # no duplicates
    for c in chans:
        again = ChannelConfig.from_dict(asdict(c))                   # passes full validation
        assert again.frequency == c.frequency and again.mode == c.mode
        assert 24e6 <= c.frequency <= 1766e6                         # R820T tuning range
        if c.mode == MODE_AM:
            assert c.ctcss_hz is None and c.deemphasis_us == 0


def test_known_frequencies():
    frs = PRESETS_BY_KEY["frs"].make_channels()
    assert len(frs) == 22
    assert frs[0].frequency == 462_562_500 and frs[7].frequency == 467_562_500
    assert frs[14].frequency == 462_550_000 and frs[21].frequency == 462_725_000
    cb = PRESETS_BY_KEY["cb"].make_channels()
    assert len(cb) == 40 and all(c.mode == MODE_AM for c in cb)
    assert [c.frequency for c in cb[21:25]] == [27_225_000, 27_255_000, 27_235_000, 27_245_000]  # ch 22-25
    assert cb[18].frequency == 27_185_000 and "19" in cb[18].name
    murs = PRESETS_BY_KEY["murs"].make_channels()
    assert [c.frequency for c in murs] == [151_820_000, 151_880_000, 151_940_000, 154_570_000, 154_600_000]
    noaa = PRESETS_BY_KEY["noaa"].make_channels()
    assert sorted(c.frequency for c in noaa) == [162_400_000 + 25_000 * i for i in range(7)]
    pmr = PRESETS_BY_KEY["pmr446"].make_channels()
    assert pmr[0].frequency == 446_006_250 and pmr[-1].frequency == 446_193_750
    gmrs = PRESETS_BY_KEY["gmrs"].make_channels()
    assert gmrs[0].bandwidth == 25_000 and gmrs[8].bandwidth == 12_500


def test_whole_preset_fits_where_expected():
    for key in ("cb", "noaa", "marine", "2m", "pmr446", "air"):
        chans = PRESETS_BY_KEY[key].make_channels()
        plan = plan_center(chans, 2.4e6)
        assert plan.in_band == {c.id for c in chans}, key
        assert not plan.dc_conflict, key


def test_split_presets_cover_the_biggest_group():
    frs = PRESETS_BY_KEY["frs"].make_channels()
    plan = plan_center(frs, 2.4e6)
    # FRS 1-7 and 15-22 (462 MHz) fit together; 8-14 (467 MHz) cannot.
    assert len(plan.in_band) == 15 and plan.warnings
    murs = PRESETS_BY_KEY["murs"].make_channels()
    plan = plan_center(murs, 2.4e6)
    assert 0 < len(plan.in_band) < 5
