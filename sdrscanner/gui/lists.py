"""Manage named channel lists ("scan lists")."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..config import MAX_GROUP_NAME, MAX_GROUPS, AppConfig, ChannelGroup, clean_text, unique_group_name


class ListsDialog(QDialog):
    """Create, rename and delete channel lists and choose which channels belong to each.

    Works on a copy; `result_groups()` returns the edited lists when accepted.
    """

    def __init__(self, config: AppConfig, parent=None, select: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Channel lists")
        self.resize(760, 520)
        self.config = config
        self.groups: list[ChannelGroup] = copy.deepcopy(config.groups)
        self._updating = False

        self.list_view = QListWidget()
        self.list_view.currentRowChanged.connect(self._show_group)
        new_b, ren_b, dup_b, del_b = (QPushButton(t) for t in ("New…", "Rename…", "Duplicate", "Delete"))
        new_b.clicked.connect(self._new)
        ren_b.clicked.connect(self._rename)
        dup_b.clicked.connect(self._duplicate)
        del_b.clicked.connect(self._delete)
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Lists</b>"))
        left.addWidget(self.list_view, 1)
        for b in (new_b, ren_b, dup_b, del_b):
            left.addWidget(b)

        self.members = QListWidget()
        for c in config.channels:
            item = QListWidgetItem(f"{c.name}  ({c.frequency / 1e6:.4f} MHz {c.mode})")
            item.setData(Qt.UserRole, c.id)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Unchecked)
            self.members.addItem(item)
        self.members.itemChanged.connect(self._member_changed)
        all_b, none_b = QPushButton("All"), QPushButton("None")
        all_b.clicked.connect(lambda: self._check_all(True))
        none_b.clicked.connect(lambda: self._check_all(False))
        self.members_label = QLabel()
        self.members_label.setTextFormat(Qt.PlainText)
        right = QVBoxLayout()
        right.addWidget(self.members_label)
        right.addWidget(self.members, 1)
        row = QHBoxLayout()
        row.addWidget(all_b)
        row.addWidget(none_b)
        row.addStretch(1)
        right.addLayout(row)

        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("A channel can be in any number of lists. Pick the list to monitor from the "
                             "toolbar; only its channels are received (and the radio retunes for them)."))
        lay.addLayout(body, 1)
        lay.addWidget(buttons)
        self._refresh_lists(select)

    # ---------------------------------------------------------------- helpers
    def _current(self) -> ChannelGroup | None:
        row = self.list_view.currentRow()
        return self.groups[row] if 0 <= row < len(self.groups) else None

    def _refresh_lists(self, select: str = "") -> None:
        self.list_view.blockSignals(True)
        self.list_view.clear()
        for g in self.groups:
            item = QListWidgetItem(f"{g.name} ({len(g.ids)})")
            self.list_view.addItem(item)
        self.list_view.blockSignals(False)
        names = [g.name for g in self.groups]
        row = names.index(select) if select in names else (0 if names else -1)
        self.list_view.setCurrentRow(row)
        self._show_group(row)

    def _show_group(self, row: int) -> None:
        g = self._current()
        self._updating = True
        for i in range(self.members.count()):
            item = self.members.item(i)
            item.setCheckState(Qt.Checked if g and item.data(Qt.UserRole) in g.ids else Qt.Unchecked)
            item.setFlags((Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) if g else Qt.NoItemFlags)
        self._updating = False
        self.members_label.setText(f"Channels in '{g.name}'" if g else "Create a list to choose its channels")

    def _member_changed(self, item) -> None:
        g = self._current()
        if self._updating or g is None:
            return
        cid = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked and cid not in g.ids:
            g.ids.append(cid)
        elif item.checkState() != Qt.Checked and cid in g.ids:
            g.ids.remove(cid)
        self.list_view.item(self.list_view.currentRow()).setText(f"{g.name} ({len(g.ids)})")

    def _check_all(self, on: bool) -> None:
        for i in range(self.members.count()):
            self.members.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _ask_name(self, title: str, initial: str = "") -> str:
        text, ok = QInputDialog.getText(self, title, "List name:", text=initial)
        return clean_text(text, MAX_GROUP_NAME) if ok else ""

    def _exists(self, name: str, ignore: ChannelGroup | None = None) -> bool:
        return any(g.name.casefold() == name.casefold() and g is not ignore for g in self.groups)

    # ---------------------------------------------------------------- actions
    def add_group(self, name: str, ids=()) -> ChannelGroup | None:
        name = clean_text(name, MAX_GROUP_NAME)
        if not name or self._exists(name) or len(self.groups) >= MAX_GROUPS:
            return None
        g = ChannelGroup(name, list(ids))
        self.groups.append(g)
        self._refresh_lists(name)
        return g

    def _new(self) -> None:
        if len(self.groups) >= MAX_GROUPS:
            QMessageBox.information(self, "Channel lists", f"At most {MAX_GROUPS} lists.")
            return
        name = self._ask_name("New list")
        if name and self._exists(name):
            QMessageBox.information(self, "Channel lists", "A list with that name already exists.")
        elif name:
            self.add_group(name)

    def _rename(self) -> None:
        g = self._current()
        if g is None:
            return
        name = self._ask_name("Rename list", g.name)
        if name and not self._exists(name, ignore=g):
            g.name = name
            self._refresh_lists(name)

    def _duplicate(self) -> None:
        g = self._current()
        if g is None or len(self.groups) >= MAX_GROUPS:
            return
        tmp = AppConfig(groups=self.groups)
        self.add_group(unique_group_name(tmp, f"{g.name} copy"), g.ids)

    def _delete(self) -> None:
        g = self._current()
        if g is None:
            return
        self.groups.remove(g)
        self._refresh_lists()

    def result_groups(self) -> list[ChannelGroup]:
        return self.groups
