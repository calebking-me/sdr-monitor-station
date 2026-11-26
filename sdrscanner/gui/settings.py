"""Radio / source settings dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from ..config import RTL_SAMPLE_RATES, SOURCE_FILE, SOURCE_RTLSDR, SOURCE_RTLTCP, AppConfig
from ..sources import inspect_iq_file, list_rtlsdr_devices


class SettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Radio settings")
        self.cfg = cfg
        r = cfg.radio

        self.source = QComboBox()
        self.source.addItem("RTL-SDR (USB)", SOURCE_RTLSDR)
        self.source.addItem("rtl_tcp server", SOURCE_RTLTCP)
        self.source.addItem("IQ file playback", SOURCE_FILE)
        self.source.setCurrentIndex(max(0, self.source.findData(r.source)))

        # --- RTL-SDR page
        rtl_page = QWidget()
        self.device_index = QSpinBox(minimum=0, maximum=15)
        self.device_index.setValue(r.device_index)
        self.device_list = QLabel("")
        self.device_list.setWordWrap(True)
        scan = QPushButton("Detect devices")
        scan.clicked.connect(self._scan)
        f = QFormLayout(rtl_page)
        row = QHBoxLayout()
        row.addWidget(self.device_index)
        row.addWidget(scan)
        f.addRow("Device index", row)
        f.addRow("", self.device_list)

        # --- rtl_tcp page
        tcp_page = QWidget()
        self.host = QLineEdit(r.tcp_host)
        self.port = QSpinBox(minimum=1, maximum=65535)
        self.port.setValue(r.tcp_port)
        f = QFormLayout(tcp_page)
        f.addRow("Host", self.host)
        f.addRow("Port", self.port)

        # --- file page
        file_page = QWidget()
        self.file_path = QLineEdit(r.file_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.file_center = QDoubleSpinBox(decimals=5, minimum=0, maximum=2000, suffix=" MHz")
        self.file_center.setValue(r.file_center / 1e6)
        self.file_rate = QDoubleSpinBox(decimals=4, minimum=0, maximum=20, suffix=" MS/s")
        self.file_rate.setValue(r.file_rate / 1e6)
        self.file_loop = QCheckBox("Loop playback")
        self.file_loop.setChecked(r.file_loop)
        self.file_info = QLabel("Supports SigMF (.sigmf-meta), WAV IQ (SDR#/SDR++), and raw "
                                ".cu8/.bin (rtl_sdr), .cs8, .cs16, .cf32/.cfile. Centre and rate "
                                "are read from metadata or the file name when available.")
        self.file_info.setWordWrap(True)
        f = QFormLayout(file_page)
        row = QHBoxLayout()
        row.addWidget(self.file_path)
        row.addWidget(browse)
        f.addRow("File", row)
        f.addRow("Centre freq", self.file_center)
        f.addRow("Sample rate", self.file_rate)
        f.addRow("", self.file_loop)
        f.addRow(self.file_info)

        self.pages = QStackedWidget()
        for p in (rtl_page, tcp_page, file_page):
            self.pages.addWidget(p)
        self.source.currentIndexChanged.connect(self._on_source)

        # --- tuner
        tuner = QGroupBox("Tuner (RTL-SDR / rtl_tcp)")
        self.rate = QComboBox()
        for sr in RTL_SAMPLE_RATES:
            self.rate.addItem(f"{sr / 1e6:.3f} MS/s", float(sr))
        i = self.rate.findData(float(r.sample_rate))
        self.rate.setCurrentIndex(i if i >= 0 else self.rate.findData(2_400_000.0))
        self.agc = QCheckBox("Automatic gain (AGC)")
        self.agc.setChecked(r.agc)
        self.gain = QDoubleSpinBox(decimals=1, minimum=0, maximum=50, singleStep=1, suffix=" dB")
        self.gain.setValue(r.gain_db)
        self.agc.toggled.connect(lambda v: self.gain.setEnabled(not v))
        self.gain.setEnabled(not r.agc)
        self.ppm = QSpinBox(minimum=-200, maximum=200, suffix=" ppm")
        self.ppm.setValue(r.ppm)
        self.auto_center = QCheckBox("Choose centre frequency automatically")
        self.auto_center.setChecked(r.auto_center)
        self.manual_center = QDoubleSpinBox(decimals=4, minimum=24, maximum=1766, suffix=" MHz")
        self.manual_center.setValue(r.manual_center / 1e6)
        self.auto_center.toggled.connect(lambda v: self.manual_center.setEnabled(not v))
        self.manual_center.setEnabled(not r.auto_center)
        f = QFormLayout(tuner)
        f.addRow("Sample rate", self.rate)
        f.addRow("", self.agc)
        f.addRow("Gain", self.gain)
        f.addRow("Frequency correction", self.ppm)
        f.addRow("", self.auto_center)
        f.addRow("Manual centre", self.manual_center)

        misc = QGroupBox("Recording")
        self.rec_dir = QLineEdit(cfg.recordings_dir)
        self.rec_dir.setPlaceholderText(str(cfg.effective_recordings_dir()))
        rd = QPushButton("Browse…")
        rd.clicked.connect(self._browse_dir)
        f = QFormLayout(misc)
        row = QHBoxLayout()
        row.addWidget(self.rec_dir)
        row.addWidget(rd)
        f.addRow("Recordings folder", row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        src_form = QFormLayout()
        src_form.addRow("Source", self.source)
        lay.addLayout(src_form)
        lay.addWidget(self.pages)
        lay.addWidget(tuner)
        lay.addWidget(misc)
        lay.addWidget(buttons)
        self._on_source()

    def _on_source(self) -> None:
        self.pages.setCurrentIndex(self.source.currentIndex())

    def _scan(self) -> None:
        try:
            devs = list_rtlsdr_devices()
            self.device_list.setText("\n".join(devs) if devs else "No RTL-SDR devices found.")
        except Exception as e:
            self.device_list.setText(f"librtlsdr not available: {e}")

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open IQ recording", self.file_path.text(),
            "IQ recordings (*.sigmf-meta *.sigmf-data *.wav *.cu8 *.u8 *.bin *.cs8 *.cs16 *.cf32 *.cfile *.fc32 *.raw);;All files (*)")
        if not path:
            return
        self.file_path.setText(path)
        try:
            info = inspect_iq_file(path)
            if info.center:
                self.file_center.setValue(info.center / 1e6)
            if info.sample_rate:
                self.file_rate.setValue(info.sample_rate / 1e6)
            missing = [n for n, v in (("centre", info.center), ("rate", info.sample_rate)) if not v]
            self.file_info.setText(f"Format: {info.kind}" + (
                f" — enter {' and '.join(missing)} manually" if missing else " — metadata found"))
        except Exception as e:
            self.file_info.setText(f"Cannot read file: {e}")

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Recordings folder", self.rec_dir.text())
        if d:
            self.rec_dir.setText(d)

    def apply(self) -> bool:
        """Write the dialog values back. Returns True if a restart is needed."""
        r = self.cfg.radio
        before = (r.source, r.device_index, r.tcp_host, r.tcp_port, r.file_path,
                  r.file_center, r.file_rate, r.sample_rate, r.file_loop)
        r.source = self.source.currentData()
        r.device_index = self.device_index.value()
        r.tcp_host = self.host.text().strip() or "127.0.0.1"
        r.tcp_port = self.port.value()
        r.file_path = self.file_path.text().strip()
        r.file_center = self.file_center.value() * 1e6
        r.file_rate = self.file_rate.value() * 1e6
        r.file_loop = self.file_loop.isChecked()
        r.sample_rate = float(self.rate.currentData())
        r.agc = self.agc.isChecked()
        r.gain_db = self.gain.value()
        r.ppm = self.ppm.value()
        r.auto_center = self.auto_center.isChecked()
        r.manual_center = self.manual_center.value() * 1e6
        self.cfg.recordings_dir = self.rec_dir.text().strip()
        after = (r.source, r.device_index, r.tcp_host, r.tcp_port, r.file_path,
                 r.file_center, r.file_rate, r.sample_rate, r.file_loop)
        return before != after
