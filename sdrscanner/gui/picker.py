"""Pick channels to add from a CSV import or a built-in preset list."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..config import MAX_CHANNELS, SOURCE_FILE, AppConfig, ChannelConfig
from ..planner import plan_center, usable_half_span
from ..presets import PRESETS
from . import theme
from .channels import bandwidth_label

COLUMNS = ["", "Name", "Frequency (MHz)", "Mode", "Bandwidth", "CTCSS", "Status"]


class ChannelPickerDialog(QDialog):
    """Shows candidate channels with a live 'fits in the capture band' check."""

    def __init__(self, config: AppConfig, parent=None, *, title: str = "Add channels",
                 candidates: Optional[list[ChannelConfig]] = None, use_presets: bool = False,
                 source_text: str = "", problems: Optional[list[str]] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 620)
        self.config = config
        self.candidates: list[ChannelConfig] = list(candidates or [])
        self._updating = False

        lay = QVBoxLayout(self)
        self.preset_combo = None
        self.preset_info = None
        if use_presets:
            row = QHBoxLayout()
            row.addWidget(QLabel("Channel list:"))
            self.preset_combo = QComboBox()
            for p in PRESETS:
                self.preset_combo.addItem(f"{p.name}  [{p.region}]", p.key)
            row.addWidget(self.preset_combo, 1)
            lay.addLayout(row)
            self.preset_info = QLabel()
            self.preset_info.setTextFormat(Qt.PlainText)
            self.preset_info.setWordWrap(True)
            lay.addWidget(self.preset_info)
            row = QHBoxLayout()
            row.addWidget(QLabel("Squelch for these channels:"))
            self.preset_squelch = QDoubleSpinBox(decimals=0, minimum=-150, maximum=0, suffix=" dBFS")
            self.preset_squelch.setValue(-50)
            self.preset_squelch.setToolTip("You can fine-tune each channel later (or use Auto on its strip).")
            row.addWidget(self.preset_squelch)
            row.addStretch(1)
            lay.addLayout(row)
            self.preset_combo.currentIndexChanged.connect(self._load_preset)
        if source_text:
            src = QLabel(source_text)
            src.setTextFormat(Qt.PlainText)
            src.setWordWrap(True)
            lay.addWidget(src)
        if problems:
            box = QPlainTextEdit("\n".join(problems))
            box.setReadOnly(True)
            box.setMaximumHeight(90)
            lbl = QLabel(f"{len(problems)} problem(s) while reading the file (affected rows were "
                         f"skipped or repaired):")
            lay.addWidget(lbl)
            lay.addWidget(box)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn in (("Select all", lambda: self._select(lambda i: True)),
                         ("Select none", lambda: self._select(lambda i: False)),
                         ("Select only those that fit", self._select_fitting)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.replace = QCheckBox("Replace my current channel list")
        self.replace.toggled.connect(self._refresh_status)
        row.addWidget(self.replace)
        lay.addLayout(row)

        self.summary = QLabel()
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        if use_presets:
            self._load_preset()
        else:
            self._populate()

    # ---------------------------------------------------------------- data
    def _load_preset(self) -> None:
        key = self.preset_combo.currentData()
        preset = next(p for p in PRESETS if p.key == key)
        self.preset_info.setText(preset.description)
        self.candidates = preset.make_channels(self.preset_squelch.value())
        self._populate()

    def _populate(self) -> None:
        self._updating = True
        existing = {(round(c.frequency), c.mode) for c in self.config.channels}
        self.table.setRowCount(len(self.candidates))
        for r, c in enumerate(self.candidates):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            dup = (round(c.frequency), c.mode) in existing
            chk.setCheckState(Qt.Unchecked if dup else Qt.Checked)
            chk.setData(Qt.UserRole, dup)
            self.table.setItem(r, 0, chk)
            for col, text in enumerate((c.name, f"{c.frequency / 1e6:.5f}", c.mode,
                                        bandwidth_label(c.bandwidth),
                                        f"{c.ctcss_hz:.1f}" if c.ctcss_hz else ""), start=1):
                item = QTableWidgetItem(text)     # QTableWidgetItem text is always plain
                self.table.setItem(r, col, item)
            self.table.setItem(r, 6, QTableWidgetItem(""))
        self._updating = False
        self._refresh_status()

    def _checked_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.Checked]

    def _select(self, pred) -> None:
        self._updating = True
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(Qt.Checked if pred(r) else Qt.Unchecked)
        self._updating = False
        self._refresh_status()

    def _select_fitting(self) -> None:
        # Plan for everything, then keep the rows that land inside the chosen band.
        self._select(lambda r: not self.table.item(r, 0).data(Qt.UserRole))
        plan = self._plan(self._checked_rows())
        if plan is None:
            return
        half = usable_half_span(plan.sample_rate)
        self._select(lambda r: not self.table.item(r, 0).data(Qt.UserRole)
                     and self._fits(self.candidates[r], plan.center, half))

    def _on_item_changed(self, item) -> None:
        if not self._updating and item.column() == 0:
            self._refresh_status()

    @staticmethod
    def _fits(c: ChannelConfig, center: float, half: float) -> bool:
        return abs(c.frequency - center) + c.bandwidth / 2 <= half

    def _plan(self, rows: list[int]):
        r = self.config.radio
        base = [] if self.replace.isChecked() else [c for c in self.config.channels if c.enabled]
        chans = base + [self.candidates[i] for i in rows]
        if not chans:
            return None
        if r.source == SOURCE_FILE and r.file_center and r.file_rate:
            return plan_center(chans, r.file_rate, fixed_center=r.file_center)
        return plan_center(chans, r.sample_rate, r.auto_center, r.manual_center)

    def _refresh_status(self) -> None:
        rows = self._checked_rows()
        plan = self._plan(rows)
        t = theme.tokens()
        self._updating = True
        half = usable_half_span(plan.sample_rate) if plan else 0
        out = 0
        for r, c in enumerate(self.candidates):
            item = self.table.item(r, 6)
            dup = self.table.item(r, 0).data(Qt.UserRole)
            if plan is None:
                text, color = "", t["text"]
            elif self._fits(c, plan.center, half):
                text, color = "fits", t["good"]
            else:
                text, color = "outside capture band", t["bad"]
                if r in rows:
                    out += 1
            if dup:
                text, color = "already in your list", t["warn"]
            item.setText(text)
            item.setForeground(QColor(color))
        self._updating = False
        existing = 0 if self.replace.isChecked() else len(self.config.channels)
        total = existing + len(rows)
        msg = f"{len(rows)} selected"
        if plan is not None:
            msg += (f" · centre would be {plan.center / 1e6:.4f} MHz "
                    f"(usable {plan.usable_lo / 1e6:.3f}-{plan.usable_hi / 1e6:.3f} MHz)")
        if out:
            msg += f" · {out} selected channel(s) won't fit and will show as 'out of band'"
        ok = self.buttons.button(QDialogButtonBox.Ok)
        if total > MAX_CHANNELS:
            msg += f" · too many channels (limit {MAX_CHANNELS})"
            ok.setEnabled(False)
        else:
            ok.setEnabled(bool(rows) or self.replace.isChecked())
        ok.setText(f"Add {len(rows)} channel(s)" if not self.replace.isChecked()
                   else f"Replace with {len(rows)} channel(s)")
        self.summary.setText(msg)

    # ---------------------------------------------------------------- result
    def selected_channels(self) -> list[ChannelConfig]:
        return [self.candidates[r] for r in self._checked_rows()]

    def replace_existing(self) -> bool:
        return self.replace.isChecked()


def describe_import(path: str, fmt: str, n: int, rows: int) -> str:
    kind = "CHIRP export" if fmt == "chirp" else "channel CSV"
    return f"Read {n} channel(s) from {rows} row(s) of {path} ({kind})."

