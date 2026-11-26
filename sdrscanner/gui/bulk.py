"""Edit one or more attributes of several channels at once."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from ..audio import list_output_devices
from ..config import MODE_AM, MODE_FM, AppConfig, ChannelConfig
from .channels import ToneSquelchEditor

YES_NO = (("Yes", True), ("No", False))


def _combo(items) -> QComboBox:
    c = QComboBox()
    for label, data in items:
        c.addItem(label, data)
    return c


class BulkEditDialog(QDialog):
    """Tick the settings to change; only those are applied to the ticked channels."""

    def __init__(self, config: AppConfig, preselected: list[str], parent=None,
                 noise_of: Optional[Callable[[str], Optional[float]]] = None):
        super().__init__(parent)
        self.setWindowTitle("Edit several channels")
        self.resize(860, 560)
        self.config = config
        self.noise_of = noise_of or (lambda cid: None)
        self.changed_ids: list[str] = []

        # ---- channels
        self.channels = QListWidget()
        visible = config.visible_channels()
        chosen = set(preselected) or {c.id for c in visible}
        for c in visible:
            item = QListWidgetItem(f"{c.name}  ({c.frequency / 1e6:.4f} MHz {c.mode})")
            item.setData(Qt.UserRole, c.id)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if c.id in chosen else Qt.Unchecked)
            self.channels.addItem(item)
        self.channels.itemChanged.connect(self._update_ok)
        all_btn, none_btn = QPushButton("All"), QPushButton("None")
        all_btn.clicked.connect(lambda: self._check_all(True))
        none_btn.clicked.connect(lambda: self._check_all(False))
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Channels</b>"))
        left.addWidget(self.channels, 1)
        row = QHBoxLayout()
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        left.addLayout(row)

        # ---- attributes: (key, label, editor)
        self.editors: dict[str, tuple[QCheckBox, QWidget]] = {}
        grid = QGridLayout()
        grid.setColumnStretch(2, 1)
        r = 0

        def add(key: str, label: str, editor: QWidget, note: str = "") -> None:
            nonlocal r
            box = QCheckBox(label)
            box.toggled.connect(lambda on, e=editor: e.setEnabled(on))
            box.toggled.connect(self._update_ok)
            editor.setEnabled(False)
            grid.addWidget(box, r, 0)
            grid.addWidget(editor, r, 1, 1, 2)
            if note:
                box.setToolTip(note)
                editor.setToolTip(note)
            self.editors[key] = (box, editor)
            r += 1

        add("enabled", "Enabled", _combo(YES_NO))
        add("mode", "Mode", _combo((("FM", MODE_FM), ("AM", MODE_AM))),
            "Changing the mode also sets the default bandwidth for that mode.")
        self.bw = QDoubleSpinBox(decimals=2, minimum=3, maximum=30, singleStep=0.5, suffix=" kHz")
        self.bw.setValue(12.5)
        add("bandwidth", "Bandwidth", self.bw)
        sq = QWidget()
        sq_lay = QHBoxLayout(sq)
        sq_lay.setContentsMargins(0, 0, 0, 0)
        self.sq_kind = _combo((("Set to", "fixed"), ("Noise floor +", "auto")))
        self.sq_value = QDoubleSpinBox(decimals=0, minimum=-150, maximum=40, suffix=" dB")
        self.sq_value.setValue(-50)
        self.sq_kind.currentIndexChanged.connect(self._sq_kind_changed)
        sq_lay.addWidget(self.sq_kind)
        sq_lay.addWidget(self.sq_value)
        sq_lay.addStretch(1)
        add("squelch", "Squelch", sq, "'Noise floor +' needs the receiver running (uses each channel's "
                                      "measured noise floor).")
        self.hyst = QDoubleSpinBox(decimals=1, minimum=0, maximum=40, suffix=" dB")
        self.hyst.setValue(3)
        add("hysteresis_db", "Hysteresis", self.hyst)
        self.hang = QDoubleSpinBox(decimals=2, minimum=0, maximum=60, singleStep=0.1, suffix=" s")
        self.hang.setValue(0.5)
        add("hang_time", "Hang time", self.hang)
        self.hold = QDoubleSpinBox(decimals=1, minimum=0, maximum=30, singleStep=0.5, suffix=" s")
        self.hold.setValue(2.0)
        add("hold_time", "Transmission hold", self.hold)
        self.tone = ToneSquelchEditor()
        add("tone", "Tone squelch", self.tone, "Applies to FM channels only.")
        add("deemphasis_us", "De-emphasis", _combo((("750 µs", 750.0), ("Off", 0.0))))
        self.volume = QDoubleSpinBox(decimals=0, minimum=0, maximum=200, suffix=" %")
        self.volume.setValue(100)
        add("volume", "Volume", self.volume)
        self.pan = QDoubleSpinBox(decimals=0, minimum=-100, maximum=100)
        add("pan", "Pan (-100 L … +100 R)", self.pan)
        add("mute", "Mute", _combo(YES_NO))
        add("record", "Record", _combo(YES_NO))
        add("priority", "Priority", _combo(YES_NO))
        add("alert", "Alert", _combo(YES_NO))
        dev = QComboBox()
        dev.addItem("System default", "")
        for key, label in list_output_devices():
            dev.addItem(label, key)
        add("audio_device", "Output device", dev)
        lists = QWidget()
        l_lay = QHBoxLayout(lists)
        l_lay.setContentsMargins(0, 0, 0, 0)
        self.list_action = _combo((("Add to", "add"), ("Remove from", "remove")))
        self.list_name = QComboBox()
        for g in config.groups:
            self.list_name.addItem(g.name, g.name)
        l_lay.addWidget(self.list_action)
        l_lay.addWidget(self.list_name, 1)
        add("list", "Channel list", lists)
        if not config.groups:
            self.editors["list"][0].setEnabled(False)
            self.editors["list"][0].setToolTip("Create a channel list first (Channels → Manage lists…)")

        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Change these settings</b> (unticked settings are left as they are)"))
        right.addLayout(grid)
        right.addStretch(1)
        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addLayout(right, 3)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addWidget(self.buttons)
        self._update_ok()

    def _sq_kind_changed(self) -> None:
        if self.sq_kind.currentData() == "auto":
            self.sq_value.setRange(0, 40)
            self.sq_value.setValue(8)
        else:
            self.sq_value.setRange(-150, 0)
            self.sq_value.setValue(-50)

    def _check_all(self, on: bool) -> None:
        for i in range(self.channels.count()):
            self.channels.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def checked_ids(self) -> list[str]:
        return [self.channels.item(i).data(Qt.UserRole) for i in range(self.channels.count())
                if self.channels.item(i).checkState() == Qt.Checked]

    def checked_fields(self) -> list[str]:
        return [k for k, (box, _) in self.editors.items() if box.isChecked()]

    def _update_ok(self, *_) -> None:
        n = len(self.checked_ids())
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(bool(n and self.checked_fields()))
        ok.setText(f"Apply to {n} channel(s)")

    def set_field(self, key: str, on: bool = True) -> None:
        self.editors[key][0].setChecked(on)

    def apply(self) -> int:
        """Apply the ticked settings; returns the number of channels changed."""
        fields = set(self.checked_fields())
        ids = set(self.checked_ids())
        changed = []
        for c in self.config.channels:
            if c.id not in ids:
                continue
            self._apply_to(c, fields)
            changed.append(c.id)
        self.changed_ids = changed
        return len(changed)

    def _apply_to(self, c: ChannelConfig, fields: set[str]) -> None:
        val = {k: self.editors[k][1] for k in self.editors}
        for key in ("enabled", "mute", "record", "priority", "alert"):
            if key in fields:
                setattr(c, key, bool(val[key].currentData()))
        if "mode" in fields:
            new_mode = val["mode"].currentData()
            if new_mode != c.mode:
                c.mode = new_mode
                c.bandwidth = 8_000.0 if new_mode == MODE_AM else 12_500.0
                if new_mode == MODE_AM:
                    c.ctcss_hz, c.dcs_code, c.deemphasis_us = None, None, 0.0
                else:
                    c.deemphasis_us = 750.0
        if "bandwidth" in fields:
            c.bandwidth = float(self.bw.value() * 1000)
        if "squelch" in fields:
            if self.sq_kind.currentData() == "auto":
                noise = self.noise_of(c.id)
                if noise is not None:
                    c.squelch_db = float(min(0.0, round(noise + self.sq_value.value())))
            else:
                c.squelch_db = float(self.sq_value.value())
        for key, editor in (("hysteresis_db", self.hyst), ("hang_time", self.hang), ("hold_time", self.hold)):
            if key in fields:
                setattr(c, key, float(editor.value()))
        if "tone" in fields and c.mode == MODE_FM:
            c.ctcss_hz, c.dcs_code, c.dcs_polarity = self.tone.value()
        if "deemphasis_us" in fields and c.mode == MODE_FM:
            c.deemphasis_us = float(val["deemphasis_us"].currentData())
        if "volume" in fields:
            c.volume = self.volume.value() / 100
        if "pan" in fields:
            c.pan = self.pan.value() / 100
        if "audio_device" in fields:
            c.audio_device = val["audio_device"].currentData() or ""
        if "list" in fields and self.list_name.count():
            g = self.config.group(self.list_name.currentData())
            if g is not None:
                if self.list_action.currentData() == "add" and c.id not in g.ids:
                    g.ids.append(c.id)
                elif self.list_action.currentData() == "remove" and c.id in g.ids:
                    g.ids.remove(c.id)
