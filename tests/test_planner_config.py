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


# ----------------------------------------------------------------- planner edge cases

def test_planner_with_no_channels_and_manual_center():
    from sdrscanner.planner import plan_center as pc
    assert pc([], 2.4e6).in_band == set()
    plan = pc([ch(146.52e6)], 2.4e6, auto_center=False, manual_center=146.52e6)
    assert plan.dc_conflict                  # manual centre right on the channel is flagged


def test_planner_random_channel_sets_keep_invariants():
    import random
    rng = random.Random(7)
    for _ in range(200):
        n = rng.randint(1, 12)
        base = rng.uniform(30e6, 1700e6)
        chans = [ch(base + rng.uniform(0, 1.8e6), bw=rng.choice([8_000, 12_500, 25_000])) for _ in range(n)]
        plan = plan_center(chans, 2.4e6)
        half = usable_half_span(2.4e6)
        for c in chans:
            inside = abs(c.frequency - plan.center) + c.bandwidth / 2 <= half + 1
            assert (c.id in plan.in_band) == inside
        assert plan.in_band | plan.out_of_band == {c.id for c in chans}


# ----------------------------------------------------------------- validation of untrusted profiles

import json  # noqa: E402

import pytest  # noqa: E402

from sdrscanner.config import (  # noqa: E402
    MAX_CHANNELS, MAX_PROFILE_BYTES, MODE_AM, RTL_SAMPLE_RATES, clean_text, snap_ctcss,
)


def _load(tmp_path, data):
    p = tmp_path / "p.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return load_profile(p)


@pytest.mark.parametrize("freq", ["nan", "inf", None, "abc", -5, 0, 1e15, True, [1]])
def test_channels_with_bad_frequency_are_skipped(tmp_path, freq):
    cfg = _load(tmp_path, {"channels": [{"name": "bad", "frequency": freq}, {"name": "ok", "frequency": 1e8}]})
    assert [c.name for c in cfg.channels] == ["ok"]
    assert any("bad" in w for w in cfg.load_warnings)


def test_out_of_range_values_are_clamped_with_warnings(tmp_path):
    cfg = _load(tmp_path, {"channels": [{
        "name": "x" * 500 + "\x00‮", "frequency": 1e8, "bandwidth": 1e9, "squelch_db": 50,
        "volume": 99, "pan": -7, "hang_time": "nan", "ctcss_hz": 123.4, "color": "red; background: url(x)",
        "id": "../../etc", "mode": "AM", "enabled": "maybe"}]})
    c = cfg.channels[0]
    assert len(c.name) == 64 and "\x00" not in c.name and "‮" not in c.name
    assert c.bandwidth == 30_000 and c.squelch_db == 0 and c.volume == 2 and c.pan == -1
    assert c.hang_time == 0.5 and c.ctcss_hz is None and c.color.startswith("#")  # colour re-assigned
    assert c.id != "../../etc" and c.mode == MODE_AM and c.enabled is True
    assert len(cfg.load_warnings) >= 6


def test_radio_values_are_sanitised(tmp_path):
    cfg = _load(tmp_path, {"radio": {"sample_rate": 2.3e6, "tcp_port": 99999, "device_index": -3,
                                     "ppm": 1e9, "gain_db": "loud", "manual_center": "nan",
                                     "tcp_host": "evil host\n", "file_rate": 1e12}})
    r = cfg.radio
    assert r.sample_rate in RTL_SAMPLE_RATES and r.sample_rate == 2_400_000
    assert r.tcp_port == 65535 and r.device_index == 0 and r.ppm == 500 and r.gain_db == 30
    assert r.manual_center == 146e6 and " " not in r.tcp_host and "\n" not in r.tcp_host
    assert r.file_rate == 40e6


def test_duplicate_ids_and_channel_cap(tmp_path):
    chans = [{"name": f"c{i}", "frequency": 1e8 + i * 1e4, "id": "same"} for i in range(MAX_CHANNELS + 20)]
    cfg = _load(tmp_path, {"channels": chans})
    assert len(cfg.channels) == MAX_CHANNELS
    assert len({c.id for c in cfg.channels}) == MAX_CHANNELS
    assert any("only the first" in w for w in cfg.load_warnings)


@pytest.mark.parametrize("text", ["[]", "42", "{not json", "\x00\x01", '{"channels": 5}'])
def test_malformed_profiles(tmp_path, text):
    p = tmp_path / "p.json"
    p.write_text(text)
    if text == '{"channels": 5}':
        assert load_profile(p).channels == []
    else:
        with pytest.raises(ValueError):
            load_profile(p)


def test_deeply_nested_json_is_rejected_cleanly(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(ValueError):
        load_profile(p)


def test_oversized_profile_is_refused(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(" " * (MAX_PROFILE_BYTES + 1))
    with pytest.raises(ValueError, match="larger"):
        load_profile(p)


def test_helpers():
    assert clean_text("  a\tb\n​c  ") == "a b c"
    assert snap_ctcss("100") == 100.0 and snap_ctcss(" off ") is None and snap_ctcss(88.6) == 88.5
    with pytest.raises(ValueError):
        snap_ctcss(125.0)


# ----------------------------------------------------------------- channel lists, DCS, hold, priority

def test_profile_roundtrip_with_lists_and_new_fields(tmp_path):
    from sdrscanner.config import ChannelGroup
    a = ChannelConfig(name="A", frequency=146.52e6, dcs_code="754", dcs_polarity="either", hold_time=3.5,
                      priority=True, alert=True)
    b = ChannelConfig(name="B", frequency=146.55e6, ctcss_hz=100.0)
    cfg = AppConfig(channels=[a, b], groups=[ChannelGroup("Mine", [a.id])], active_group="Mine",
                    priority_duck_db=-25.0, alert_sound=False)
    cfg.ensure_colors()
    p = tmp_path / "p.json"
    save_profile(cfg, p)
    back = load_profile(p)
    assert back.to_dict() == cfg.to_dict()
    assert [c.name for c in back.monitored_channels()] == ["A"]


def test_lists_and_dcs_are_validated(tmp_path):
    cfg = _load(tmp_path, {
        "channels": [{"name": "a", "frequency": 1e8, "id": "aa", "dcs_code": "088"},
                     {"name": "b", "frequency": 1e8, "id": "bb", "dcs_code": "D023I", "ctcss_hz": 100},
                     {"name": "c", "frequency": 1e8, "id": "cc", "mode": "AM", "dcs_code": "023"}],
        "groups": [{"name": "<b>X</b>‮", "ids": ["aa", "zz", 5, "aa"]}, {"name": "x"},
                   "junk", {"name": "Y" * 100, "ids": "notalist"}],
        "active_group": "nope", "priority_duck_db": 50})
    a, b, c = cfg.channels
    assert a.dcs_code is None and (b.dcs_code, b.dcs_polarity, b.ctcss_hz) == ("023", "I", None)
    assert c.dcs_code is None                                   # AM has no DCS
    g = cfg.groups[0]
    assert g.ids == ["aa"] and "‮" not in g.name
    assert [len(x.ids) for x in cfg.groups] == [1, 0, 0]          # 'junk' entry dropped
    assert cfg.groups[2].name == "Y" * 40                          # long names truncated
    assert cfg.active_group == "" and cfg.priority_duck_db == 0
    assert any("DCS" in w for w in cfg.load_warnings)


def test_too_many_lists(tmp_path):
    from sdrscanner.config import MAX_GROUPS
    cfg = _load(tmp_path, {"groups": [{"name": f"L{i}", "ids": []} for i in range(MAX_GROUPS + 5)]})
    assert len(cfg.groups) == MAX_GROUPS
