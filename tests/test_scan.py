"""Scanning between sets of channels: planner, scan state machine and engine end to end."""

import random
import time

import pytest

import sdrscanner.engine as engine_mod
from sdrscanner.config import AppConfig, ChannelConfig, ScanConfig
from sdrscanner.engine import Engine
from sdrscanner.planner import DC_GUARD_HZ, _dc_clearance, plan_center, plan_scan, usable_half_span
from sdrscanner.presets import PRESETS
from sdrscanner.scan import TONE_WAIT_CTCSS, ScanController
from sdrscanner.tools.sim_source import SimTransmitter, SimulatedSource

FS = 2_400_000.0
CHUNK = 131_072 / FS          # seconds per engine chunk


# --------------------------------------------------------------------------- planner

def check_plan(chans, fs=FS):
    sp = plan_scan(chans, fs)
    half = usable_half_span(fs)
    by = {c.id: c for c in chans}
    seen = []
    for n, s in enumerate(sp.sets, 1):
        assert s.number == n
        group = [by[i] for i in s.channel_ids]
        assert group
        for c in group:
            assert c.frequency - c.bandwidth / 2 >= s.center - half - 1e-6
            assert c.frequency + c.bandwidth / 2 <= s.center + half + 1e-6
        if len(group) > 1:
            assert _dc_clearance(s.center, group) >= DC_GUARD_HZ / 3
        seen += s.channel_ids
    assert sorted(seen) == sorted(c.id for c in chans if c.enabled)       # every channel exactly once
    lows = [s.lo for s in sp.sets]
    assert lows == sorted(lows)
    return sp


def test_far_apart_channels_get_their_own_sets():
    chans = [ChannelConfig(name="2m", frequency=146.52e6), ChannelConfig(name="rpt", frequency=147.0e6),
             ChannelConfig(name="NOAA", frequency=162.55e6, bandwidth=25_000),
             ChannelConfig(name="FRS1", frequency=462.5625e6), ChannelConfig(name="air", frequency=121.5e6,
                                                                           mode="AM", bandwidth=8_000),
             ChannelConfig(name="off", frequency=440e6, enabled=False)]
    sp = check_plan(chans)
    names = [[c.name for c in chans if c.id in s.channel_ids] for s in sp.sets]
    assert names == [["air"], ["2m", "rpt"], ["NOAA"], ["FRS1"]]
    assert sp.set_index_of(chans[3].id) == 3 and sp.set_index_of(chans[5].id) is None
    assert sp.cycle_seconds(0.5) == pytest.approx(2.0)


def test_channels_that_fit_make_one_set_like_the_fixed_planner():
    chans = [ChannelConfig(frequency=146.0e6 + i * 50_000) for i in range(10)]
    sp = check_plan(chans)
    assert len(sp.sets) == 1
    assert sp.sets[0].center == plan_center(chans, FS).center
    assert sp.cycle_seconds(0.5) == 0.0


def test_frs_needs_two_sets_and_dense_blocks_are_split():
    frs = next(p for p in PRESETS if p.key == "frs").make_channels()
    sp = check_plan(frs)
    assert len(sp.sets) == 2 and sp.sets[0].hi < 463e6 < sp.sets[1].lo
    dense = [ChannelConfig(frequency=150e6 + i * 25e3) for i in range(100)]        # 2.5 MHz of channels
    assert len(check_plan(dense).sets) == 2
    assert len(check_plan(dense, fs=1_024_000.0).sets) >= 3


def test_plan_scan_properties_on_random_layouts():
    rng = random.Random(3)
    for _ in range(150):
        base = rng.uniform(30e6, 900e6)
        spread = rng.choice([30e3, 1e6, 3e6, 40e6, 600e6])
        chans = [ChannelConfig(frequency=round(base + rng.uniform(0, spread), -2),
                               bandwidth=rng.choice([8_000.0, 12_500.0, 25_000.0]),
                               enabled=rng.random() > 0.1) for _ in range(rng.randint(0, 40))]
        check_plan(chans, rng.choice([1.024e6, 2.048e6, 2.4e6, 3.2e6]))


# --------------------------------------------------------------------------- state machine

def run(ctl, seconds, any_open=False, tone_wait=0.0, dt=CHUNK):
    """Feed `seconds` of identical chunks; return the time at which it asked to move on (or None)."""
    t = 0.0
    while t < seconds - 1e-9:
        t += dt
        if ctl.update(dt, any_open, tone_wait):
            return t
    return None


def test_quiet_set_moves_on_after_listen_time():
    cfg = ScanConfig(listen_time=0.5)
    ctl = ScanController(cfg, 3)
    t = run(ctl, 5)
    assert t == pytest.approx(0.5, abs=CHUNK) and t >= 0.5
    assert ctl.describe() == "scanning"
    cfg.listen_time = 2.0                       # the GUI changed the speed: applies to the next visit too
    ctl.enter(ctl.next_index())
    assert ctl.index == 1
    assert run(ctl, 5) == pytest.approx(2.0, abs=CHUNK)


def test_activity_then_dwell_then_resume():
    ctl = ScanController(ScanConfig(listen_time=0.3, dwell_time=2.0), 3)
    assert run(ctl, 0.1) is None
    assert run(ctl, 3.0, any_open=True) is None          # stays while a channel is open
    assert ctl.state == "active"
    t = run(ctl, 5)
    assert ctl.state == "dwell" and t == pytest.approx(2.0, abs=CHUNK)
    # A reply within the dwell time keeps it there.
    ctl.enter(0)
    run(ctl, 0.5, any_open=True)
    assert run(ctl, 1.5) is None
    assert run(ctl, 0.5, any_open=True) is None
    assert run(ctl, 1.9) is None
    assert run(ctl, 0.3) is not None


def test_max_dwell_leaves_a_busy_set():
    ctl = ScanController(ScanConfig(dwell_time=2.0, max_dwell=5.0), 2)
    t = run(ctl, 20, any_open=True)
    assert t == pytest.approx(5.0, abs=CHUNK)
    ctl.cfg.max_dwell = 0.0
    ctl.enter(1)
    assert run(ctl, 20, any_open=True) is None


def test_tone_wait_extends_listening_and_dwell():
    ctl = ScanController(ScanConfig(listen_time=0.3, dwell_time=0.5), 3)
    # A carrier with an undecided tone appears 0.2 s into the visit.
    assert run(ctl, 0.2) is None
    t = run(ctl, 3, tone_wait=TONE_WAIT_CTCSS)
    assert t is not None and 0.2 + t == pytest.approx(0.2 + TONE_WAIT_CTCSS, abs=2 * CHUNK)
    # The carrier goes away before the tone is decoded: the normal listen time applies again.
    ctl.enter(0)
    run(ctl, 0.1, tone_wait=TONE_WAIT_CTCSS)
    assert run(ctl, 0.4) == pytest.approx(0.2, abs=CHUNK)
    # During the dwell time, a new carrier waiting for its tone delays leaving.
    ctl.enter(0)
    run(ctl, 0.5, any_open=True)
    assert run(ctl, 0.4) is None
    assert run(ctl, 0.5, tone_wait=TONE_WAIT_CTCSS) is None
    assert run(ctl, 1.5, tone_wait=TONE_WAIT_CTCSS) is not None


def test_hold_and_single_set_never_move():
    ctl = ScanController(ScanConfig(listen_time=0.2), 4)
    ctl.hold = True
    assert run(ctl, 10) is None
    assert ctl.describe() == "hold"
    ctl.hold = False
    assert run(ctl, 0.1) is not None                     # already past the listen time
    single = ScanController(ScanConfig(listen_time=0.2), 1)
    assert run(single, 10) is None


def test_next_index_skips_sets_in_both_directions():
    ctl = ScanController(ScanConfig(), 5, index=0)
    ctl.skip = {1, 2}
    assert [ctl.next_index(1), ctl.next_index(2), ctl.next_index(-1), ctl.next_index(-2)] == [3, 4, 4, 3]
    ctl.skip = set(range(5))
    assert ctl.next_index() == 1                          # all skipped: plain step instead of looping


# --------------------------------------------------------------------------- engine

def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return False


class Watcher:
    """Polls the engine like the GUI does and keeps what it saw."""

    def __init__(self, eng):
        self.eng = eng
        self.events, self.messages, self.visits, self.snaps = [], [], [], []

    def poll(self):
        s = self.eng.poll()
        self.events += s.events
        self.messages += s.messages
        if s.scan and (not self.visits or self.visits[-1] != (s.scan.index, s.scan.state)):
            self.visits.append((s.scan.index, s.scan.state))
        self.snaps.append(s)
        return s

    def run(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.poll()
            time.sleep(0.03)

    def until(self, cond, timeout=15.0):
        return wait_for(lambda: cond(self.poll()), timeout)


@pytest.fixture
def no_audio(monkeypatch):
    class Silent:
        enabled = False

        def __init__(self, *a):
            pass

        def write(self, *a):
            pass

        def close_unused(self, keys):
            pass

        def close_all(self):
            pass
    monkeypatch.setattr(engine_mod, "AudioManager", Silent)


def scan_engine(monkeypatch, transmitters, channels, **scan):
    src = SimulatedSource(transmitters)
    monkeypatch.setattr(engine_mod, "make_source", lambda cfg: src)
    cfg = AppConfig(channels=channels)
    cfg.scan.enabled = True
    cfg.scan.listen_time = scan.get("listen_time", 0.25)
    cfg.scan.dwell_time = scan.get("dwell_time", 0.8)
    for k, v in scan.items():
        setattr(cfg.scan, k, v)
    eng = Engine(cfg)
    return eng, src, cfg


def three_sets():
    return [ChannelConfig(name="2m", frequency=146.52e6, squelch_db=-60, hang_time=0.2),
            ChannelConfig(name="NOAA", frequency=162.55e6, squelch_db=-60, bandwidth=25_000, hang_time=0.2),
            ChannelConfig(name="FRS1", frequency=462.5625e6, squelch_db=-60, hang_time=0.2)]


def test_engine_scans_stops_on_activity_and_resumes(monkeypatch, no_audio, tmp_path):
    chans = three_sets()
    chans[2].record = True
    eng, src, cfg = scan_engine(monkeypatch, [SimTransmitter(462.5625e6, -40, bursts=[(1.5, 3.0)])], chans)
    cfg.recordings_dir = str(tmp_path)
    w = Watcher(eng)
    eng.start()
    try:
        assert w.until(lambda s: s.scan is not None and s.scan.state == "scanning")
        s = w.poll()
        assert len(s.scan.sets) == 3 and [st.scan_set for st in s.channels.values()] == [1, 2, 3]
        waiting = [st.state for cid, st in s.channels.items() if cid != chans[s.scan.index].id]
        assert waiting == ["scanning", "scanning"]
        assert w.until(lambda s: s.channels.get(chans[2].id) and s.channels[chans[2].id].open, 6)
        assert eng.poll().scan.index == 2
        assert w.until(lambda s: any(e.channel_id == chans[2].id for e in w.events), 6)
        w.run(1.5)                                        # dwell (0.8 s) is over: scanning again
        assert w.poll().scan.index != 2 or w.poll().scan.state == "scanning"
    finally:
        eng.stop()
    w.poll()
    ev = [e for e in w.events if e.channel_id == chans[2].id]
    assert len(ev) == 1 and ev[0].recording
    assert 0.9 < ev[0].duration < 2.2                    # started on the first visit after 1.5 s
    assert {i for i, _ in w.visits} == {0, 1, 2}
    states = [st for i, st in w.visits if i == 2]
    assert "active" in states and "dwell" in states
    assert len(src.retunes) >= 4
    assert not any("crashed" in m or "failed" in m for m in w.messages), w.messages
    assert all(end is not None for _, _, end in eng.history_snapshot())


def test_hold_step_and_goto(monkeypatch, no_audio):
    eng, src, cfg = scan_engine(monkeypatch, [], three_sets(), listen_time=0.2)
    w = Watcher(eng)
    eng.start()
    try:
        assert w.until(lambda s: s.scan is not None and s.scan.state == "scanning")
        eng.scan_hold = True
        time.sleep(0.3)
        held = w.poll().scan.index
        n = len(src.retunes)
        w.run(1.2)
        assert w.poll().scan.index == held and w.poll().scan.state == "hold"
        assert len(src.retunes) <= n + 1
        eng.scan_goto(2)
        assert w.until(lambda s: s.scan.index == 2 and abs(s.center - s.scan.sets[2].center) < 1, 3)
        eng.scan_step(-1)
        assert w.until(lambda s: s.scan.index == 1 and abs(s.center - s.scan.sets[1].center) < 1, 3)
        w.run(0.8)
        assert w.poll().scan.index == 1                   # still holding
        eng.scan_hold = False
        assert w.until(lambda s: s.scan.index != 1, 3)
    finally:
        eng.stop()


def test_scan_off_or_one_set_means_fixed_tuning(monkeypatch, no_audio):
    # A carrier that never stops: max_dwell makes the scanner leave it after 1 s.
    eng, src, cfg = scan_engine(monkeypatch, [SimTransmitter(146.52e6, -40)], three_sets(), max_dwell=1.0)
    cfg.scan.enabled = False
    w = Watcher(eng)
    eng.start()
    try:
        assert w.until(lambda s: s.channels and any(st.state == "out_of_band" for st in s.channels.values()))
        assert w.poll().scan is None and not eng.scanning
        # Turning scanning on while running reaches the far channels.
        cfg.scan.enabled = True
        eng.channels_changed()
        assert w.until(lambda s: s.scan is not None and s.scan.state != "single", 5)
        assert w.until(lambda s: s.scan.state == "active", 3)
        assert w.until(lambda s: s.channels[cfg.channels[2].id].state in ("idle", "active"), 5)
        assert w.until(lambda s: s.scan.index == 0 and s.scan.state == "active", 5)       # and back
        # Only channels that fit in one tuning: no retuning, status "single".
        for c in cfg.channels[1:]:
            c.enabled = False
        eng.channels_changed()
        assert w.until(lambda s: s.scan is not None and s.scan.state == "single", 5)
        n = len(src.retunes)
        w.run(0.8)
        assert len(src.retunes) <= n + 1 and not eng.scanning
        assert w.poll().channels[cfg.channels[0].id].open
    finally:
        eng.stop()


def test_unreachable_set_is_skipped(monkeypatch, no_audio):
    chans = three_sets() + [ChannelConfig(name="Too high", frequency=2_400e6, squelch_db=-60)]
    eng, src, cfg = scan_engine(monkeypatch, [], chans, listen_time=0.15)
    w = Watcher(eng)
    eng.start()
    try:
        assert w.until(lambda s: any("cannot tune" in m for m in w.messages), 8)
        w.run(1.5)
        s = w.poll()
        assert s.scan.skipped == [3]
        assert {i for i, _ in w.visits[-6:]} <= {0, 1, 2}
        assert sum("cannot tune" in m for m in w.messages) == 1        # reported once
    finally:
        eng.stop()
    assert eng.poll().running is False


def test_tone_squelch_while_scanning(monkeypatch, no_audio):
    chans = [ChannelConfig(name="Right tone", frequency=146.52e6, squelch_db=-60, ctcss_hz=100.0),
             ChannelConfig(name="Wrong tone", frequency=462.5625e6, squelch_db=-60, ctcss_hz=100.0),
             ChannelConfig(name="Idle", frequency=162.55e6, squelch_db=-60)]
    txs = [SimTransmitter(146.52e6, -40, ctcss=100.0, bursts=[(1.0, 6.0)]),
           SimTransmitter(462.5625e6, -40, ctcss=131.8)]
    eng, src, cfg = scan_engine(monkeypatch, txs, chans, listen_time=0.2, dwell_time=0.5)
    w = Watcher(eng)
    eng.start()
    try:
        # The right tone is decoded although the listen time (0.2 s) is shorter than the decode time.
        assert w.until(lambda s: s.channels.get(chans[0].id) and s.channels[chans[0].id].open, 8)
        w.run(1.0)
    finally:
        eng.stop()
    w.poll()
    assert any(e.channel_id == chans[0].id and e.tone == 100.0 for e in w.events)
    assert not any(e.channel_id == chans[1].id for e in w.events)
    # The scanner did not stay on the wrong-tone carrier: it kept visiting the other sets.
    wrong = eng._scan_plan.set_index_of(chans[1].id)
    assert wrong is not None and wrong in {i for i, _ in w.visits}
    assert not any(i == wrong and st == "active" for i, st in w.visits)


def test_settle_time_setting(monkeypatch):
    cfg = AppConfig()
    eng = Engine(cfg)
    src = SimulatedSource([])
    src.sample_rate = FS
    assert eng._settle_samples(src) == 0                  # simulator: nothing in flight
    src.retune_settle = 0.05
    assert eng._settle_samples(src) == 120_064            # 50 ms rounded up to whole USB packets
    cfg.scan.settle_ms = 200
    assert eng._settle_samples(src) % 256 == 0 and eng._settle_samples(src) >= 480_000
