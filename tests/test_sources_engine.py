import socket
import struct
import threading
import time

import numpy as np
import pytest
from scipy.io import wavfile

from sdrscanner.activity import export_csv
from sdrscanner.audio import LinearResampler, pan_gains
from sdrscanner.config import SOURCE_FILE, ChannelConfig, RadioConfig
from sdrscanner.dsp import complex_to_u8
from sdrscanner.engine import Engine
from sdrscanner.sources import EndOfStream, FileSource, RtlTcpSource, inspect_iq_file
from sdrscanner.tools.gen_iq import SynthChannel, generate, make_profile


def tone_iq(n=4096):
    return (0.5 * np.exp(1j * 2 * np.pi * 0.01 * np.arange(n))).astype(np.complex64)


def test_raw_cu8_with_frequency_in_name(tmp_path):
    p = tmp_path / "capture_146520000Hz_2.4Msps.cu8"
    complex_to_u8(tone_iq()).tofile(p)
    info = inspect_iq_file(p)
    assert info.center == 146_520_000 and info.sample_rate == 2.4e6
    src = FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p), file_loop=False), realtime=False)
    src.open(0)
    x = src.read(4096)
    assert np.max(np.abs(x - tone_iq())) < 0.01
    with pytest.raises(EndOfStream):
        src.read(10)


def test_cf32_requires_manual_rate(tmp_path):
    p = tmp_path / "x.cf32"
    tone_iq().tofile(p)
    src = FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p)), realtime=False)
    with pytest.raises(RuntimeError):
        src.open(0)
    src = FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p), file_center=1e8, file_rate=1e6), realtime=False)
    src.open(0)
    assert np.allclose(src.read(100), tone_iq()[:100])
    # Looping wraps around to the start.
    src.read(4096 - 100)
    assert np.allclose(src.read(10), tone_iq()[:10])


def test_wav_iq(tmp_path):
    x = tone_iq()
    data = (np.stack([x.real, x.imag], axis=1) * 32767).astype(np.int16)
    p = tmp_path / "SDRSharp_20240101_000000Z_162550000Hz_IQ.wav"
    wavfile.write(p, 250_000, data)
    info = inspect_iq_file(p)
    assert info.center == 162_550_000 and info.sample_rate == 250_000
    src = FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p)), realtime=False)
    src.open(0)
    assert np.max(np.abs(src.read(4096) - x)) < 1e-3


def test_rtl_tcp_client_against_fake_server():
    received = []
    payload = complex_to_u8(tone_iq(8192)).tobytes()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
        conn.settimeout(1)
        conn.sendall(payload)
        try:
            while True:
                d = conn.recv(5)
                if not d:
                    break
                received.append(struct.unpack(">BI", d))
        except OSError:
            pass
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    src = RtlTcpSource(RadioConfig(tcp_host="127.0.0.1", tcp_port=port, gain_db=20.7, sample_rate=2.4e6))
    src.open(460_100_000)
    x = src.read(8192)
    assert np.max(np.abs(x - tone_iq(8192))) < 0.01
    src.close()
    t.join(2)
    srv.close()
    cmds = dict(received)
    assert cmds[1] == 460_100_000 and cmds[2] == 2_400_000 and cmds[4] == 207


def test_pan_and_resampler():
    assert pan_gains(0) == (1.0, 1.0)
    assert pan_gains(-1) == (1.0, 0.0)
    assert pan_gains(0.5) == (0.5, 1.0)
    r = LinearResampler(44_100 / 48_000)
    total = sum(len(r.process(np.ones((768, 2), np.float32))) for _ in range(250))
    assert abs(total - 768 * 250 * 44_100 / 48_000) <= 2


def test_engine_end_to_end_file(tmp_path):
    fs, center = 2_400_000.0, 155_000_000.0
    chans = [
        SynthChannel("A", -500_000, -40, ctcss=100.0, bursts=[(0.5, 2.0), (3.5, 4.5)]),
        SynthChannel("B", 300_000, -45, bursts=[(1.0, 3.0)]),
        SynthChannel("Quiet", 600_000, -45, bursts=[]),
    ]
    data, chans = generate(str(tmp_path / "t"), 5.5, center, fs, channels=chans)
    cfg = make_profile(data, chans, center, fs)
    cfg.radio.file_loop = False
    cfg.recordings_dir = str(tmp_path / "rec")
    cfg.channels[0].record = True
    cfg.channels[0].hang_time = 0.3
    # Wrong tone: should never open.
    cfg.channels.append(ChannelConfig(name="A wrong tone", frequency=center - 500_000, squelch_db=-60, ctcss_hz=123.0))
    eng = Engine(cfg)
    eng.start()
    events = []
    deadline = time.time() + 20
    while eng.running and time.time() < deadline:
        time.sleep(0.2)
        events += eng.poll().events
    events += eng.poll().events
    eng.stop()
    by = {}
    for e in events:
        by.setdefault(e.channel_name, []).append(e)
    assert len(by["A"]) == 2
    assert by["A"][0].tone == 100.0
    # Tone squelch opens only once the CTCSS tone is confirmed (~0.5 s), so A is shorter.
    assert 0.8 < by["A"][0].duration < 1.8
    assert all(e.recording for e in by["A"])
    assert len(by["B"]) == 1 and by["B"][0].peak_dbfs == pytest.approx(-45, abs=1.5)
    assert by["B"][0].duration == pytest.approx(2.0 + 0.5, abs=0.4)   # burst + hang time
    assert "Quiet" not in by and "A wrong tone" not in by
    rate, audio = wavfile.read(by["A"][0].recording)
    assert rate == 16_000 and len(audio) > 16_000
    n = export_csv(events, tmp_path / "log.csv")
    assert n == len(events)
    assert len(eng.history_snapshot()) == len(events)
