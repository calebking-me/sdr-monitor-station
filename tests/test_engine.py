"""Engine behaviour under live changes, retuning, audio routing and restarts."""

import socket
import struct
import threading
import time

import numpy as np
import pytest
from scipy.io import wavfile

import sdrscanner.engine as engine_mod
from sdrscanner.config import MODE_AM, AppConfig, ChannelConfig, RadioConfig
from sdrscanner.dsp import complex_to_u8
from sdrscanner.engine import Engine
from sdrscanner.sources import IQSource
from sdrscanner.tools.gen_iq import SynthChannel, generate, make_profile

FS = 2_400_000.0


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def collect(eng, seconds):
    events, messages = [], []
    end = time.time() + seconds
    while time.time() < end:
        s = eng.poll()
        events += s.events
        messages += s.messages
        time.sleep(0.05)
    return events, messages


class FakeAudio:
    """Stands in for AudioManager: records what each device would play."""

    def __init__(self, on_error=None):
        self.enabled = True
        self.written: dict[str, list[np.ndarray]] = {}
        self.closed = False

    def write(self, key, stereo):
        self.written.setdefault(key, []).append(stereo.copy())

    def close_unused(self, keys):
        pass

    def close_all(self):
        self.closed = True


class ToneSource(IQSource):
    """Tunable fake radio: FM carriers at fixed absolute frequencies, real-time paced."""

    tunable = True
    name = "fake"

    def __init__(self, carriers):
        super().__init__()
        self.carriers = carriers          # list of (abs_freq, amplitude, audio_tone_hz)
        self.n = 0
        self.retunes = []

    def open(self, center):
        self.center, self.sample_rate = float(center), FS
        self.t0 = time.monotonic()

    def set_center(self, center):
        self.retunes.append(center)
        self.center = float(center)

    def read(self, n):
        t = (self.n + np.arange(n)) / FS
        x = (np.random.default_rng(self.n).standard_normal(n) * 0.003).astype(np.complex64)
        for f, amp, tone in self.carriers:
            ph = 2 * np.pi * (f - self.center) * t + 2500 / tone * np.sin(2 * np.pi * tone * t)
            x += (amp * np.exp(1j * ph)).astype(np.complex64)
        self.n += n
        due = self.t0 + self.n / FS
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return x

    def close(self):
        pass


@pytest.fixture
def fake_audio(monkeypatch):
    holder = {}

    def make(on_error):
        holder["audio"] = FakeAudio(on_error)
        return holder["audio"]
    monkeypatch.setattr(engine_mod, "AudioManager", make)
    return holder


def test_audio_routing_pan_mute_solo(monkeypatch, fake_audio):
    src = ToneSource([(146.0e6, 0.05, 700.0), (146.3e6, 0.05, 1300.0)])
    monkeypatch.setattr(engine_mod, "make_source", lambda cfg: src)
    left = ChannelConfig(name="L", frequency=146.0e6, squelch_db=-60, pan=-1.0)
    right = ChannelConfig(name="R", frequency=146.3e6, squelch_db=-60, pan=1.0, audio_device="dev2")
    cfg = AppConfig(channels=[left, right])
    eng = Engine(cfg)
    eng.start()
    try:
        assert wait_for(lambda: "dev2" in fake_audio.get("audio", FakeAudio()).written and
                        len(fake_audio["audio"].written.get("", [])) > 60)
        audio = fake_audio["audio"]
        a = np.concatenate(audio.written[""][-40:])
        b = np.concatenate(audio.written["dev2"][-40:])
        assert np.std(a[:, 0]) > 0.05 and np.std(a[:, 1]) < 1e-6      # L channel: left only, default device
        assert np.std(b[:, 1]) > 0.05 and np.std(b[:, 0]) < 1e-6      # R channel: right only, dev2
        left.mute = True
        time.sleep(0.6)
        n = len(audio.written[""])
        time.sleep(0.4)
        assert np.max(np.abs(np.concatenate(audio.written[""][n:]))) == 0   # muted
        left.mute, right.solo = False, True
        time.sleep(0.6)
        n = len(audio.written[""])
        time.sleep(0.4)
        assert np.max(np.abs(np.concatenate(audio.written[""][n:]))) == 0   # other channel soloed
    finally:
        eng.stop()
    assert fake_audio["audio"].closed and eng.audio is None


def test_live_channel_changes_and_retune(monkeypatch, fake_audio):
    src = ToneSource([(146.0e6, 0.05, 700.0), (146.4e6, 0.05, 1000.0), (151.0e6, 0.05, 900.0)])
    monkeypatch.setattr(engine_mod, "make_source", lambda cfg: src)
    a = ChannelConfig(name="A", frequency=146.0e6, squelch_db=-60)
    b = ChannelConfig(name="B", frequency=146.4e6, squelch_db=-60)
    cfg = AppConfig(channels=[a, b])
    eng = Engine(cfg)
    eng.start()
    try:
        assert wait_for(lambda: all(s.open for s in eng.poll().channels.values()) and eng.poll().channels)
        # Rapid structural edits from the "GUI" thread.
        for i in range(12):
            c = ChannelConfig(name=f"tmp{i}", frequency=146.1e6 + i * 12_500, squelch_db=-60)
            cfg.channels.append(c)
            eng.channels_changed()
            time.sleep(0.05)
            b.enabled = not b.enabled
            eng.channels_changed()
            time.sleep(0.05)
            cfg.channels.remove(c)
            eng.channels_changed()
        b.enabled = True
        a.frequency = 146.0e6 + 6_250          # retune one channel (key change)
        eng.channels_changed()
        time.sleep(0.5)
        # Move everything 5 MHz away: the engine must retune the (fake) dongle.
        a.frequency, b.frequency = 151.0e6, 151.0e6 + 25_000
        eng.channels_changed()
        assert wait_for(lambda: src.retunes and abs(eng.center - 151.0e6) < 1.1e6)
        assert wait_for(lambda: eng.poll().channels.get(a.id) and eng.poll().channels[a.id].open)
        events, messages = collect(eng, 0.5)
        assert not any("crashed" in m for m in messages), messages
        assert eng.running
    finally:
        eng.stop()
    # Every transmission that started has been closed exactly once.
    assert all(end is not None for _, _, end in eng.history_snapshot())


def test_am_end_to_end_with_recording(tmp_path, fake_audio):
    center = 120e6
    chans = [SynthChannel("Tower", -300_000, -40, mode=MODE_AM, bursts=[(0.4, 2.0)]),
             SynthChannel("FM", 250_000, -45, bursts=[(0.6, 1.8)])]
    data, chans = generate(str(tmp_path / "am"), 2.6, center, FS, channels=chans)
    cfg = make_profile(data, chans, center, FS)
    cfg.radio.file_loop = False
    cfg.recordings_dir = str(tmp_path / "rec")
    cfg.channels[0].record = True
    eng = Engine(cfg)
    eng.start()
    events = []
    assert wait_for(lambda: (events.extend(eng.poll().events) or not eng.running), 20)
    eng.stop()
    events += eng.poll().events
    by = {e.channel_name: e for e in events}
    assert set(by) == {"Tower", "FM"}
    assert by["Tower"].mode == MODE_AM and by["Tower"].tone is None
    assert by["Tower"].duration == pytest.approx(1.6 + 0.5, abs=0.4)
    rate, audio = wavfile.read(by["Tower"].recording)
    assert rate == 16_000 and np.std(audio) > 1000          # real audio, not silence


def test_iq_recording_plays_back(tmp_path, fake_audio):
    center = 460e6
    chans = [SynthChannel("A", -200_000, -40, bursts=[(0.0, 3.0)])]
    data, chans = generate(str(tmp_path / "src"), 3.0, center, FS, channels=chans)
    cfg = make_profile(data, chans, center, FS)
    cfg.radio.file_loop = False
    eng = Engine(cfg)
    eng.start()
    assert wait_for(lambda: eng.poll().source_ok)
    eng.start_iq_recording(str(tmp_path / "copy"))
    time.sleep(1.5)
    eng.stop_iq_recording()
    time.sleep(0.3)
    eng.stop()
    meta = tmp_path / "copy.sigmf-meta"
    assert meta.exists() and (tmp_path / "copy.sigmf-data").stat().st_size > 1_000_000
    cfg2 = make_profile(tmp_path / "copy.sigmf-data", chans, center, FS)
    cfg2.radio.file_center = cfg2.radio.file_rate = 0.0       # must come from the SigMF metadata
    cfg2.radio.file_loop = False
    eng2 = Engine(cfg2)
    eng2.start()
    events = []
    assert wait_for(lambda: (events.extend(eng2.poll().events) or not eng2.running), 20)
    eng2.stop()
    events += eng2.poll().events
    assert eng2.center == pytest.approx(center) and any(e.channel_name == "A" for e in events)


def test_restart_after_file_ends(tmp_path, fake_audio):
    data, chans = generate(str(tmp_path / "s"), 0.8, 460e6, FS,
                           channels=[SynthChannel("A", 100_000, -40, bursts=[(0.1, 0.6)])])
    cfg = make_profile(data, chans, 460e6, FS)
    cfg.radio.file_loop = False
    eng = Engine(cfg)
    for _ in range(3):
        eng.start()
        assert wait_for(lambda: not eng.running, 10)
        first = fake_audio["audio"]
        eng.stop()                       # what the GUI does when it notices the end
        assert first.closed and eng.audio is None
        assert all(not t.is_alive() for t in threading.enumerate() if t.name.startswith("sdr-"))


def test_rtl_tcp_reconnects_after_server_drop(monkeypatch, fake_audio):
    monkeypatch.setattr(engine_mod, "RECONNECT_DELAY", 0.2)
    payload = complex_to_u8((0.3 * np.exp(1j * 0.01 * np.arange(200_000))).astype(np.complex64)).tobytes()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    connections = []

    def serve():
        for _ in range(2):
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            connections.append(conn)
            conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
            conn.sendall(payload)            # ~0.75 chunks, then hang up
            time.sleep(0.2)
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cfg = AppConfig(radio=RadioConfig(source="rtl_tcp", tcp_host="127.0.0.1", tcp_port=port),
                    channels=[ChannelConfig(frequency=146e6)])
    eng = Engine(cfg)
    eng.start()
    msgs = []
    assert wait_for(lambda: (msgs.extend(eng.poll().messages) or len(connections) >= 2), 15)
    eng.stop()
    srv.close()
    text = "\n".join(msgs + eng.poll().messages)
    assert "reconnecting" in text and text.count("started") >= 1
