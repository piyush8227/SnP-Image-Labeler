# SnP Image Labeler
# Copyright (C) 2026 Piyush More
#
# Licensed under the GNU General Public License, version 3 or later.
# See the LICENSE file at the repository root for the full text.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

"""
SnP Image Labeler - Duplicate Scan Dialog

Scans the queue (and, optionally, everything already recorded in
processed_images.csv) for the same picture stored more than once, then lets the
user tick the redundant copies and act on them.

The scan runs in a worker thread — hashing thousands of images must never
freeze the window — and only QImage crosses the thread boundary, never QPixmap.

Acting on the ticks is deliberately NOT done here: the dialog emits what the
user chose and the main window carries it out, so the queue, the dataset record
and the _rejected folder all stay in step. Handled rows are then dropped from
the tree with mark_handled().
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QSize, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QIcon, QImage, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core import theme

from core.duplicate_finder import (
    DEFAULT_SENSITIVITY, SENSITIVITY, Signature, build_signature,
    group_duplicates, group_headline, human_size, summarise,
)
from core.thumbnail_cache import ThumbnailService

PREVIEW_PX = 148
THUMB_PX   = 48


# ------------------------------------------------------------------ #
#  Worker                                                              #
# ------------------------------------------------------------------ #

class ScanWorker(QThread):
    """Builds signatures for every candidate image, then groups them."""

    progress = pyqtSignal(int, int, str)          # done, total, current name
    finished_ok = pyqtSignal(list, dict)          # groups, {record key: phash}

    def __init__(
        self,
        queue_paths: List[str],
        dataset_entries: List[Dict[str, Any]],
        threshold: int,
        parent=None,
    ):
        super().__init__(parent)
        self._queue     = list(queue_paths)
        self._dataset   = list(dataset_entries)
        self._threshold = threshold
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        want_phash = self._threshold > 0
        signatures: List[Signature] = []
        learned: Dict[str, int] = {}

        queue_keys = {self._norm(p) for p in self._queue}
        # A dataset row for an image that is also in the queue would pair with
        # itself; the queue copy already carries the record note.
        dataset = [e for e in self._dataset
                   if self._norm(e.get("path", "")) not in queue_keys]

        total = len(self._queue) + len(dataset)
        done  = 0

        for path in self._queue:
            if self._cancelled:
                return
            done += 1
            self.progress.emit(done, total, Path(path).name)
            sig = build_signature(path, want_phash=want_phash)
            if sig:
                signatures.append(sig)

        for entry in dataset:
            if self._cancelled:
                return
            done += 1
            self.progress.emit(done, total, Path(entry.get("path", "")).name)
            sig = build_signature(
                entry.get("path", ""),
                want_phash=want_phash,
                in_dataset=True,
                note=entry.get("note", ""),
                key=entry.get("key", ""),
                known_sha=entry.get("sha", ""),
                known_phash=entry.get("phash"),
                probe_path=entry.get("probe_path", ""),
            )
            if not sig:
                continue
            signatures.append(sig)
            if sig.phash is not None and entry.get("phash") is None and sig.key:
                learned[sig.key] = sig.phash

        if self._cancelled:
            return
        self.progress.emit(total, total, "Comparing…")
        self.finished_ok.emit(group_duplicates(signatures, self._threshold), learned)

    @staticmethod
    def _norm(path: str) -> str:
        return os.path.normcase(os.path.abspath(path)) if path else ""


# ------------------------------------------------------------------ #
#  Dialog                                                             #
# ------------------------------------------------------------------ #

class DuplicateScanDialog(QDialog):

    remove_from_queue_requested = pyqtSignal(list)   # [path, ...]
    reject_requested            = pyqtSignal(list)   # [path, ...]
    phashes_learned             = pyqtSignal(dict)   # {record key: phash}
    open_requested              = pyqtSignal(str)    # show this image in the queue

    def __init__(
        self,
        queue_paths: List[str],
        dataset_entries: Optional[List[Dict[str, Any]]] = None,
        parent=None,
        sensitivity_index: int = DEFAULT_SENSITIVITY,
    ):
        super().__init__(parent)
        self.setWindowTitle("Find Duplicate Images")
        self.setMinimumSize(880, 620)
        self.setModal(False)                 # keep it open while fixing the queue

        self._queue_paths     = list(queue_paths)
        self._dataset_entries = list(dataset_entries or [])
        self._groups: List[Dict[str, Any]] = []
        self._worker: Optional[ScanWorker]  = None
        self._loader: Optional[ThumbnailService] = None

        self._build()
        self._combo.setCurrentIndex(
            min(max(sensitivity_index, 0), len(SENSITIVITY) - 1))
        self._update_actions()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # ── Controls ──────────────────────────────────────────────
        controls = QHBoxLayout()
        controls.setSpacing(6)
        controls.addWidget(QLabel("Match:"))

        self._combo = QComboBox()
        for label, distance, hint in SENSITIVITY:
            self._combo.addItem(label, distance)
        self._combo.setMinimumWidth(190)
        self._combo.currentIndexChanged.connect(self._on_sensitivity)
        controls.addWidget(self._combo)

        self._chk_dataset = QCheckBox("Also compare with already-sorted images")
        self._chk_dataset.setChecked(bool(self._dataset_entries))
        self._chk_dataset.setEnabled(bool(self._dataset_entries))
        self._chk_dataset.setToolTip(
            "Compare against every image in processed_images.csv, so a re-import "
            "of something already sorted is caught even under a new name."
        )
        controls.addWidget(self._chk_dataset)
        controls.addStretch()

        self._btn_scan = QPushButton("Scan")
        self._btn_scan.setMinimumHeight(28)
        self._btn_scan.setMinimumWidth(90)
        self._btn_scan.clicked.connect(lambda _: self._toggle_scan())
        controls.addWidget(self._btn_scan)
        layout.addLayout(controls)

        self._hint = QLabel(SENSITIVITY[DEFAULT_SENSITIVITY][2])
        self._hint.setObjectName("empty_label")
        layout.addWidget(self._hint)

        # ── Progress ──────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setVisible(False)
        self._progress.setMinimumHeight(theme.px(16))
        layout.addWidget(self._progress)

        self._summary = QLabel(
            f"{len(self._queue_paths)} image(s) in the queue. Press Scan.")
        self._summary.setObjectName("info_label")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        # ── Results tree ──────────────────────────────────────────
        self._tree = QTreeWidget()
        self._tree.setObjectName("dup_tree")
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Image", "Folder", "Size", "Status"])
        self._tree.setIconSize(QSize(theme.px(THUMB_PX), theme.px(THUMB_PX)))
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setAlternatingRowColors(False)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self._tree.setColumnWidth(1, 230)
        self._tree.setColumnWidth(3, 180)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.currentItemChanged.connect(self._on_current_changed)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._tree, 1)

        # ── Preview strip ─────────────────────────────────────────
        self._preview_frame = QFrame()
        self._preview_frame.setObjectName("dup_preview_frame")
        self._preview_frame.setMinimumHeight(PREVIEW_PX + 34)
        self._preview_row = QHBoxLayout(self._preview_frame)
        self._preview_row.setContentsMargins(8, 6, 8, 6)
        self._preview_row.setSpacing(10)
        self._preview_hint = QLabel("Select a group to compare the images side by side.")
        self._preview_hint.setObjectName("empty_label")
        self._preview_row.addWidget(self._preview_hint)
        self._preview_row.addStretch()
        layout.addWidget(self._preview_frame)

        # ── Selection helpers ─────────────────────────────────────
        helpers = QHBoxLayout()
        helpers.setSpacing(6)
        self._btn_auto = QPushButton("Tick every extra copy")
        self._btn_auto.setToolTip(
            "Keep the first file of each group (already-sorted, else oldest) "
            "and tick the rest."
        )
        self._btn_auto.clicked.connect(lambda _: self._select_extras())
        self._btn_none = QPushButton("Clear ticks")
        self._btn_none.clicked.connect(lambda _: self._select_none())
        for b in (self._btn_auto, self._btn_none):
            b.setMinimumHeight(26)
        helpers.addWidget(self._btn_auto)
        helpers.addWidget(self._btn_none)
        helpers.addStretch()
        self._ticked = QLabel("")
        self._ticked.setObjectName("info_label")
        helpers.addWidget(self._ticked)
        layout.addLayout(helpers)

        # ── Actions ───────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._btn_remove = QPushButton("Remove Ticked from Queue")
        self._btn_remove.setToolTip(
            "Take them out of this session's queue. No file is touched.")
        self._btn_reject = QPushButton("Move Ticked to _rejected")
        self._btn_reject.setObjectName("dup_reject_btn")
        self._btn_reject.setToolTip(
            "Move the files into the _rejected folder and drop any copies they "
            "have in the dataset. Nothing is permanently deleted."
        )
        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self.close)
        for b in (self._btn_remove, self._btn_reject, self._btn_close):
            b.setMinimumHeight(30)
        self._btn_remove.clicked.connect(lambda _: self._emit_action(reject=False))
        self._btn_reject.clicked.connect(lambda _: self._emit_action(reject=True))
        actions.addStretch()
        actions.addWidget(self._btn_remove)
        actions.addWidget(self._btn_reject)
        actions.addWidget(self._btn_close)
        layout.addLayout(actions)

    # ------------------------------------------------------------------ #
    #  Scanning                                                            #
    # ------------------------------------------------------------------ #

    def _on_sensitivity(self, index: int):
        if 0 <= index < len(SENSITIVITY):
            self._hint.setText(SENSITIVITY[index][2])

    def sensitivity_index(self) -> int:
        return self._combo.currentIndex()

    def _toggle_scan(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._set_scanning(False)
            self._summary.setText("Scan cancelled.")
            return
        self.start_scan()

    def start_scan(self):
        if not self._queue_paths and not self._dataset_entries:
            self._summary.setText("Nothing to scan — import some images first.")
            return

        threshold = self._combo.currentData() or 0
        entries   = self._dataset_entries if self._chk_dataset.isChecked() else []

        self._tree.clear()
        self._clear_preview()
        self._groups = []
        self._set_scanning(True)
        self._progress.setRange(0, max(len(self._queue_paths) + len(entries), 1))
        self._progress.setValue(0)

        self._worker = ScanWorker(self._queue_paths, entries, threshold, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_results)
        self._worker.start()

    def _set_scanning(self, scanning: bool):
        self._btn_scan.setText("Stop" if scanning else "Scan")
        self._progress.setVisible(scanning)
        self._combo.setEnabled(not scanning)
        self._chk_dataset.setEnabled(not scanning and bool(self._dataset_entries))
        for b in (self._btn_remove, self._btn_reject, self._btn_auto, self._btn_none):
            b.setEnabled(not scanning)

    @pyqtSlot(int, int, str)
    def _on_progress(self, done: int, total: int, name: str):
        self._progress.setMaximum(max(total, 1))
        self._progress.setValue(done)
        self._progress.setFormat(f"%v / %m — {name}")

    @pyqtSlot(list, dict)
    def _on_results(self, groups: List[Dict[str, Any]], learned: Dict[str, int]):
        self._set_scanning(False)
        self._groups = groups
        self._populate(groups)
        self._summary.setText(summarise(groups))
        if learned:
            self.phashes_learned.emit(learned)

    # ------------------------------------------------------------------ #
    #  Tree                                                                #
    # ------------------------------------------------------------------ #

    def _populate(self, groups: List[Dict[str, Any]]):
        self._tree.blockSignals(True)
        self._tree.clear()
        thumb_paths: List[str] = []

        for number, group in enumerate(groups, start=1):
            top = QTreeWidgetItem(self._tree)
            top.setFirstColumnSpanned(True)
            top.setText(0, f"{number}.  {group_headline(group)}")
            top.setForeground(0, QColor("#89b4fa" if group["kind"] == "exact"
                                        else "#f9e2af"))
            top.setFlags(Qt.ItemIsEnabled)
            top.setData(0, Qt.UserRole + 1, group)

            for position, member in enumerate(group["members"]):
                child = QTreeWidgetItem(top)
                child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                               | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if position else Qt.Unchecked)
                child.setText(0, member.name)
                child.setText(1, member.folder)
                child.setText(2, human_size(member.size_bytes))
                child.setText(3, self._status_text(member, position))
                child.setToolTip(0, member.path)
                child.setToolTip(1, member.path)
                if position == 0:
                    child.setForeground(0, QColor("#a6e3a1"))
                if member.in_dataset:
                    child.setForeground(3, QColor("#f9e2af"))
                if not Path(member.path).is_file():
                    child.setForeground(0, QColor("#6c7086"))
                child.setData(0, Qt.UserRole, member)
                thumb_paths.append(member.path)

            top.setExpanded(True)

        self._tree.blockSignals(False)
        self._update_actions()
        self._start_thumbnails(thumb_paths)

    @staticmethod
    def _status_text(member: Signature, position: int) -> str:
        if not Path(member.path).is_file():
            return "file missing"
        bits = []
        if position == 0:
            bits.append("keep")
        if member.in_dataset:
            bits.append(f"in dataset: {member.note}" if member.note else "in dataset")
        return "  ·  ".join(bits)

    def _start_thumbnails(self, paths: List[str]):
        alive = [p for p in paths if Path(p).is_file()]
        if not alive:
            return
        if self._loader is None:
            # Shared service: scaled decode, small thread pool, disk cache.
            self._loader = ThumbnailService(THUMB_PX, "dup", self)
            self._loader.thumbnail_ready.connect(self._on_thumbnail)
        else:
            self._loader.cancel_pending()
        for path in alive:
            self._loader.request(path)

    @pyqtSlot(str, QImage, QSize)
    def _on_thumbnail(self, path: str, image: QImage, _size: QSize):
        icon = QIcon(QPixmap.fromImage(image))
        for top in self._top_items():
            for child in self._children(top):
                member = child.data(0, Qt.UserRole)
                if member and member.path == path:
                    child.setIcon(0, icon)

    def _stop_loader(self):
        if self._loader:
            self._loader.shutdown()
            self._loader = None

    def _top_items(self) -> List[QTreeWidgetItem]:
        return [self._tree.topLevelItem(i)
                for i in range(self._tree.topLevelItemCount())]

    @staticmethod
    def _children(top: QTreeWidgetItem) -> List[QTreeWidgetItem]:
        return [top.child(i) for i in range(top.childCount())]

    # ------------------------------------------------------------------ #
    #  Selection                                                           #
    # ------------------------------------------------------------------ #

    def _select_extras(self):
        self._tree.blockSignals(True)
        for top in self._top_items():
            for position, child in enumerate(self._children(top)):
                child.setCheckState(0, Qt.Checked if position else Qt.Unchecked)
        self._tree.blockSignals(False)
        self._update_actions()

    def _select_none(self):
        self._tree.blockSignals(True)
        for top in self._top_items():
            for child in self._children(top):
                child.setCheckState(0, Qt.Unchecked)
        self._tree.blockSignals(False)
        self._update_actions()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if column == 0:
            self._update_actions()

    def checked_paths(self) -> List[str]:
        out: List[str] = []
        for top in self._top_items():
            for child in self._children(top):
                if child.checkState(0) == Qt.Checked:
                    member = child.data(0, Qt.UserRole)
                    if member:
                        out.append(member.path)
        return out

    def _fully_ticked_groups(self) -> int:
        """Groups where nothing at all would be left behind."""
        count = 0
        for top in self._top_items():
            children = self._children(top)
            if children and all(c.checkState(0) == Qt.Checked for c in children):
                count += 1
        return count

    def _update_actions(self):
        ticked = self.checked_paths()
        total_bytes = 0
        for top in self._top_items():
            for child in self._children(top):
                if child.checkState(0) == Qt.Checked:
                    member = child.data(0, Qt.UserRole)
                    if member:
                        total_bytes += member.size_bytes
        self._ticked.setText(
            f"{len(ticked)} ticked  ·  {human_size(total_bytes)}" if ticked else "")
        self._btn_remove.setEnabled(bool(ticked))
        self._btn_reject.setEnabled(bool(ticked))

    # ------------------------------------------------------------------ #
    #  Preview                                                             #
    # ------------------------------------------------------------------ #

    def _on_current_changed(self, current: QTreeWidgetItem, _previous):
        if current is None:
            return
        top = current.parent() or current
        group = top.data(0, Qt.UserRole + 1)
        if group:
            self._show_preview(group, current.data(0, Qt.UserRole))

    def _clear_preview(self):
        while self._preview_row.count():
            entry = self._preview_row.takeAt(0)
            widget = entry.widget()
            if widget:
                widget.deleteLater()

    def _show_preview(self, group: Dict[str, Any], selected: Optional[Signature]):
        self._clear_preview()
        for member in group["members"][:5]:
            self._preview_row.addWidget(self._preview_tile(member, selected))
        self._preview_row.addStretch()

    def _preview_tile(self, member: Signature, selected: Optional[Signature]) -> QWidget:
        tile = QWidget()
        column = QVBoxLayout(tile)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        picture = QLabel()
        picture.setAlignment(Qt.AlignCenter)
        picture.setFixedSize(theme.px(PREVIEW_PX), theme.px(PREVIEW_PX))
        picture.setObjectName(
            "dup_preview_current" if (selected and selected.path == member.path)
            else "dup_preview_tile"
        )
        pixmap = QPixmap(member.path)
        if pixmap.isNull():
            picture.setText("unreadable")
        else:
            picture.setPixmap(pixmap.scaled(
                PREVIEW_PX - 8, PREVIEW_PX - 8,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        column.addWidget(picture)

        name = QLabel(member.name)
        name.setObjectName("empty_label")
        name.setAlignment(Qt.AlignCenter)
        name.setFixedWidth(theme.px(PREVIEW_PX))
        name.setToolTip(member.path)
        metrics = name.fontMetrics()
        name.setText(metrics.elidedText(member.name, Qt.ElideMiddle, PREVIEW_PX - 6))
        column.addWidget(name)
        return tile

    def _on_double_click(self, item: QTreeWidgetItem, _column: int):
        member = item.data(0, Qt.UserRole)
        if member and not member.in_dataset:
            self.open_requested.emit(member.path)

    # ------------------------------------------------------------------ #
    #  Acting on the ticks                                                 #
    # ------------------------------------------------------------------ #

    def _emit_action(self, reject: bool):
        paths = self.checked_paths()
        if not paths:
            return

        stranded = self._fully_ticked_groups()
        if stranded and QMessageBox.question(
            self, "Nothing kept",
            f"{stranded} group(s) have every copy ticked, so no version of "
            f"those images would be left.\n\nContinue anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        if reject:
            self.reject_requested.emit(paths)
        else:
            self.remove_from_queue_requested.emit(paths)

    def mark_handled(self, paths: List[str]):
        """Drop rows the main window dealt with, and tidy up empty groups."""
        handled = {str(p) for p in paths}
        if not handled:
            return
        self._tree.blockSignals(True)
        for top in self._top_items():
            for child in self._children(top):
                member = child.data(0, Qt.UserRole)
                if member and member.path in handled:
                    top.removeChild(child)
            if top.childCount() < 2:
                self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(top))
        self._tree.blockSignals(False)
        self._clear_preview()
        remaining = self._tree.topLevelItemCount()
        self._summary.setText(
            f"Handled {len(handled)} image(s). "
            + (f"{remaining} group(s) still listed." if remaining
               else "No duplicate groups left.")
        )
        self._update_actions()

    def update_queue_paths(self, paths: List[str]):
        """Keep the scan source in step with the queue between scans."""
        self._queue_paths = list(paths)

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(1500)
        self._stop_loader()
        super().closeEvent(event)
