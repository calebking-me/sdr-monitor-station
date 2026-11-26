from sdrscanner.config import AppConfig, ChannelConfig, load_profile, save_profile
from sdrscanner.planner import DC_GUARD_HZ, plan_center, usable_half_span


def ch(f, name="c", bw=12_500, enabled=True):
    return ChannelConfig(name=name, frequency=f, bandwidth=bw, enabled=enabled)


def test_center_avoids_dc_spike_on_middle_channel():
    chans = [ch(460.0e6), ch(460.5e6), ch(461.0e6)]   # midpoint is exactly on a channel
    plan = plan_center(chans, 2.4e6)
    assert not plan.warnings
    assert len(plan.in_band) == 3
    for c in chans:
        assert abs(plan.center - c.frequency) - c.bandwidth / 2 >= DC_GUARD_HZ
        assert abs(plan.center - c.frequency) + c.bandwidth / 2 <= usable_half_span(2.4e6)


def test_single_channel_is_offset_from_dc():
    plan = plan_center([ch(146.52e6)], 2.4e6)
    assert abs(plan.center - 146.52e6) >= DC_GUARD_HZ
    assert not plan.dc_conflict


def test_warns_when_channels_do_not_fit():
    chans = [ch(150.0e6, "a"), ch(150.2e6, "b"), ch(155.0e6, "far")]
    plan = plan_center(chans, 2.4e6)
    assert plan.warnings
    assert chans[2].id in plan.out_of_band
    assert {chans[0].id, chans[1].id} <= plan.in_band


def test_disabled_channels_ignored_and_fixed_center():
    chans = [ch(100e6), ch(200e6, enabled=False)]
    plan = plan_center(chans, 2.4e6)
    assert not plan.warnings
    plan = plan_center(chans, 2.4e6, fixed_center=102e6)
    assert chans[0].id in plan.out_of_band


def test_profile_roundtrip(tmp_path):
    cfg = AppConfig()
    cfg.channels = [ch(146.52e6, "Simplex"), ChannelConfig(name="Rpt", frequency=147e6, ctcss_hz=100.0, pan=-0.5)]
    cfg.ensure_colors()
    cfg.radio.gain_db = 33.8
    p = tmp_path / "p.json"
    save_profile(cfg, p)
    back = load_profile(p)
    assert back.to_dict() == cfg.to_dict()


def test_profile_tolerates_unknown_and_missing_keys(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{"radio": {"source": "bogus", "future": 1}, "channels": [{"name": "x", "frequency": 1e8, "extra": true}]}')
    cfg = load_profile(p)
    assert cfg.radio.source == "rtlsdr"
    assert cfg.channels[0].name == "x" and cfg.channels[0].color
