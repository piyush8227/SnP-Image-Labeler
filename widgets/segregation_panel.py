"""
SnP Image Labeler - Segregation Panel

Right-side panel for the dataset segregation task:
  - One row per class: checkbox + its own Train / Test / Validation selector
  - Optional auto-split ratio (per class, based on what is already in the dataset)
  - Assign & Next  /  Remove from dataset
  - Live per-class counts
"""

from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

from core.segregation_manager import (
    SPLITS, SPLIT_LABELS, class_folder_name, sanitize_class_name,
)


# ------------------------------------------------------------------ #
#  One class row                                                       #
# ------------------------------------------------------------------ #

class ClassRow(QWidget):
    """Checkbox for a class plus an independent split selector for that class."""

    changed = pyqtSignal()

    def __init__(self, class_name: str, parent=None):
        super().__init__(parent)
        self.class_name = class_name
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(3)

        # Long names are elided so the row always fits the narrow panel
        shown = (self.class_name if len(self.class_name) <= 18
                 else self.class_name[:16] + "…")
        self._check = QCheckBox(shown)
        self._check.setToolTip(
            f"{self.class_name}\nFolder: {class_folder_name(self.class_name)}/")
        self._check.toggled.connect(self._on_toggled)
        layout.addWidget(self._check)

        split_row = QHBoxLayout()
        split_row.setContentsMargins(18, 0, 0, 0)
        split_row.setSpacing(4)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: Dict[str, QPushButton] = {}
        for split in SPLITS:
            btn = QPushButton(SPLIT_LABELS[split][:5])
            btn.setObjectName("split_btn")
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setMinimumWidth(44)
            btn.setToolTip(f"{SPLIT_LABELS[split]} → {class_folder_name(self.class_name)}/{split}/")
            btn.clicked.connect(lambda _, s=split: self._on_split_clicked(s))
            self._group.addButton(btn)
            self._buttons[split] = btn
            split_row.addWidget(btn)
        split_row.addStretch()
        layout.addLayout(split_row)

        self._set_split_enabled(False)

    # -- internal ------------------------------------------------------- #

    def _on_toggled(self, checked: bool):
        self._set_split_enabled(checked)
        self.changed.emit()

    def _on_split_clicked(self, split: str):
        # Clicking a split is also a convenient way to select the class
        if not self._check.isChecked():
            self._check.setChecked(True)      # emits changed via _on_toggled
        self.changed.emit()

    def _set_split_enabled(self, enabled: bool):
        for btn in self._buttons.values():
            btn.setEnabled(enabled)

    # -- public --------------------------------------------------------- #

    def is_checked(self) -> bool:
        return self._check.isChecked()

    def set_checked(self, checked: bool):
        self._check.setChecked(checked)

    def toggle(self):
        self._check.setChecked(not self._check.isChecked())

    def split(self) -> Optional[str]:
        for split, btn in self._buttons.items():
            if btn.isChecked():
                return split
        return None

    def set_split(self, split: str):
        btn = self._buttons.get(split)
        if btn:
            btn.setChecked(True)

    def clear(self):
        self._check.blockSignals(True)
        self._check.setChecked(False)
        self._check.blockSignals(False)
        self._group.setExclusive(False)
        for btn in self._buttons.values():
            btn.setChecked(False)
        self._group.setExclusive(True)
        self._set_split_enabled(False)


# ------------------------------------------------------------------ #
#  Panel                                                               #
# ------------------------------------------------------------------ #

class SegregationPanel(QWidget):
    """Right-side panel driving the segregation workflow."""

    assign_requested          = pyqtSignal()
    unassign_requested        = pyqtSignal()
    manage_classes_requested  = pyqtSignal()
    classes_changed           = pyqtSignal(list)   # user added a class inline
    settings_changed          = pyqtSignal()       # auto-split / ratios / sticky

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[ClassRow] = []
        self._classes: List[str] = []
        self._suggester: Optional[Callable[[str], str]] = None
        self._default_split: str = "train"
        self._has_image: bool = False
        self._assigned: bool = False
        self._build()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        hdr = QLabel("Segregation")
        hdr.setObjectName("panel_header")
        hdr.setAlignment(Qt.AlignCenter)
        root.addWidget(hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        inner = QVBoxLayout(container)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.setSpacing(6)

        # -- auto split ratio ------------------------------------------- #
        self._auto_check = QCheckBox("Auto split")
        self._auto_check.setToolTip(
            "Pick the split automatically for each class, keeping it close to the "
            "target ratio. You can still override it per class."
        )
        self._auto_check.toggled.connect(self._on_auto_toggled)
        inner.addWidget(self._auto_check)

        # One row per split — stacked so the labels and values stay readable
        # at any font size instead of being squeezed onto a single line.
        self._ratio_spins: Dict[str, QSpinBox] = {}
        for split, default in (("train", 70), ("test", 20), ("val", 10)):
            ratio_row = QHBoxLayout()
            ratio_row.setContentsMargins(18, 0, 0, 0)
            ratio_row.setSpacing(6)

            lbl = QLabel(SPLIT_LABELS[split])
            lbl.setObjectName("info_label")
            lbl.setMinimumWidth(70)

            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setValue(default)
            spin.setSuffix("%")
            spin.setButtonSymbols(QSpinBox.NoButtons)
            spin.setAlignment(Qt.AlignCenter)
            spin.setToolTip(f"Target share for {SPLIT_LABELS[split]}")
            spin.setEnabled(False)
            spin.valueChanged.connect(lambda _v: self.settings_changed.emit())
            self._ratio_spins[split] = spin

            ratio_row.addWidget(lbl)
            ratio_row.addWidget(spin)
            ratio_row.addStretch()
            inner.addLayout(ratio_row)

        sep = QFrame()
        sep.setObjectName("field_sep")
        sep.setFrameShape(QFrame.HLine)
        inner.addWidget(sep)

        # -- classes ---------------------------------------------------- #
        cls_hdr = QLabel("Classes")
        cls_hdr.setObjectName("field_label")
        inner.addWidget(cls_hdr)

        self._empty_lbl = QLabel("No classes yet — use '+ Add' below.")
        self._empty_lbl.setObjectName("empty_label")
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._empty_lbl.setWordWrap(True)
        inner.addWidget(self._empty_lbl)

        self._rows_holder = QVBoxLayout()
        self._rows_holder.setContentsMargins(0, 0, 0, 0)
        self._rows_holder.setSpacing(0)
        inner.addLayout(self._rows_holder)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_add = QPushButton("+ Add")
        btn_add.setToolTip("Add a new class")
        btn_add.clicked.connect(lambda _: self._add_class_inline())
        btn_manage = QPushButton("Manage")
        btn_manage.setToolTip("Add / rename / reorder classes")
        btn_manage.clicked.connect(lambda _: self.manage_classes_requested.emit())
        for b in (btn_add, btn_manage):
            b.setFixedHeight(26)
            # Ignored width policy: let the buttons shrink with the panel
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_manage)
        inner.addLayout(btn_row)

        sep2 = QFrame()
        sep2.setObjectName("field_sep")
        sep2.setFrameShape(QFrame.HLine)
        inner.addWidget(sep2)

        # -- current image state ---------------------------------------- #
        self._state_lbl = QLabel("No image loaded.")
        self._state_lbl.setObjectName("seg_state_label")
        self._state_lbl.setWordWrap(True)
        inner.addWidget(self._state_lbl)

        self._counts_lbl = QLabel("")
        self._counts_lbl.setObjectName("empty_label")
        self._counts_lbl.setWordWrap(True)
        self._counts_lbl.setAlignment(Qt.AlignLeft)
        inner.addWidget(self._counts_lbl)

        inner.addStretch()
        self._scroll.setWidget(container)
        root.addWidget(self._scroll, 1)

        # -- footer ------------------------------------------------------ #
        footer = QFrame()
        footer.setObjectName("save_frame")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(12, 8, 12, 8)
        fl.setSpacing(6)

        self._sticky_check = QCheckBox("Keep selection")
        self._sticky_check.setToolTip(
            "Keep the same classes and splits selected after assigning — "
            "useful when sorting long runs of the same class."
        )
        self._sticky_check.toggled.connect(lambda _: self.settings_changed.emit())
        fl.addWidget(self._sticky_check)

        self._assign_btn = QPushButton("Assign && Next")
        self._assign_btn.setObjectName("save_label_btn")
        self._assign_btn.setMinimumHeight(40)
        self._assign_btn.setEnabled(False)
        self._assign_btn.setToolTip("Copy this image into the selected class folders  (Enter)")
        self._assign_btn.clicked.connect(lambda _: self.assign_requested.emit())
        fl.addWidget(self._assign_btn)

        self._remove_btn = QPushButton("Remove from dataset")
        self._remove_btn.setObjectName("queue_clear_btn")
        self._remove_btn.setFixedHeight(24)
        self._remove_btn.setEnabled(False)
        self._remove_btn.setToolTip("Delete the copies made for this image")
        self._remove_btn.clicked.connect(lambda _: self.unassign_requested.emit())
        fl.addWidget(self._remove_btn)

        root.addWidget(footer)

    # ------------------------------------------------------------------ #
    #  Classes                                                             #
    # ------------------------------------------------------------------ #

    def load_classes(self, classes: List[str]):
        """Rebuild the class rows."""
        for row in self._rows:
            self._rows_holder.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        self._classes = [c for c in classes if c]
        for name in self._classes:
            row = ClassRow(name)
            row.changed.connect(self._on_row_changed)
            self._rows_holder.addWidget(row)
            self._rows.append(row)

        self._empty_lbl.setVisible(not self._classes)
        self._update_buttons()

    def _add_class_inline(self):
        text, ok = QInputDialog.getText(self, "Add Class", "Class name:")
        if not ok:
            return
        name = (text or "").strip()
        if not name or not sanitize_class_name(name):
            QMessageBox.warning(self, "Invalid Name", "Enter a usable class name.")
            return
        if any(c.lower() == name.lower() for c in self._classes):
            QMessageBox.information(self, "Duplicate", f"'{name}' already exists.")
            return
        self.classes_changed.emit(self._classes + [name])

    # ------------------------------------------------------------------ #
    #  Settings                                                            #
    # ------------------------------------------------------------------ #

    def _on_auto_toggled(self, checked: bool):
        for spin in self._ratio_spins.values():
            spin.setEnabled(checked)
        if checked:
            self._apply_auto_splits()
        self.settings_changed.emit()

    def set_auto_split(self, enabled: bool):
        self._auto_check.blockSignals(True)
        self._auto_check.setChecked(enabled)
        self._auto_check.blockSignals(False)
        for spin in self._ratio_spins.values():
            spin.setEnabled(enabled)

    def auto_split_enabled(self) -> bool:
        return self._auto_check.isChecked()

    def set_ratios(self, ratios: Dict[str, int]):
        for split, spin in self._ratio_spins.items():
            spin.blockSignals(True)
            spin.setValue(int(ratios.get(split, spin.value())))
            spin.blockSignals(False)

    def ratios(self) -> Dict[str, int]:
        return {s: spin.value() for s, spin in self._ratio_spins.items()}

    def set_sticky(self, enabled: bool):
        self._sticky_check.blockSignals(True)
        self._sticky_check.setChecked(enabled)
        self._sticky_check.blockSignals(False)

    def sticky_enabled(self) -> bool:
        return self._sticky_check.isChecked()

    def set_split_suggester(self, fn: Callable[[str], str]):
        """Callable returning the auto-ratio split for a class name."""
        self._suggester = fn

    def set_default_split(self, split: str):
        if split in SPLITS:
            self._default_split = split

    # ------------------------------------------------------------------ #
    #  Per-image state                                                     #
    # ------------------------------------------------------------------ #

    def load_image_state(
        self,
        has_image: bool,
        assignments: List[Dict[str, str]],
        destinations: List[str],
    ):
        """Show the saved assignment for this image, or a clean slate."""
        self._has_image  = has_image
        self._assigned   = bool(assignments)

        if not has_image:
            for row in self._rows:
                row.clear()
            self._state_lbl.setText("No image loaded.")
            self._update_buttons()
            return

        if assignments:
            by_class = {a["class"]: a["split"] for a in assignments}
            for row in self._rows:
                row.clear()
                if row.class_name in by_class:
                    row.set_checked(True)
                    row.set_split(by_class[row.class_name])
            folders = "\n".join(
                f"• {class_folder_name(a['class'])}/{a['split']}/" for a in assignments
            )
            self._state_lbl.setText(f"Already in dataset:\n{folders}")
            self._state_lbl.setToolTip("\n".join(destinations))
        elif not self.sticky_enabled():
            for row in self._rows:
                row.clear()
            self._state_lbl.setText("Not segregated yet.")
            self._state_lbl.setToolTip("")
        else:
            # Sticky: keep the previous selection, just refresh auto splits
            self._state_lbl.setText("Not segregated yet — selection kept.")
            self._state_lbl.setToolTip("")
            if self.auto_split_enabled():
                self._apply_auto_splits()

        self._update_buttons()

    def set_counts(self, counts: Dict[str, Dict[str, int]]):
        """counts: {class: {'train': n, 'test': n, 'val': n}}"""
        if not counts:
            self._counts_lbl.setText("")
            return
        lines = []
        for cls in sorted(counts.keys(), key=str.lower):
            c = counts[cls]
            total = sum(c.values())
            lines.append(
                f"{cls}: {total}  (tr {c.get('train', 0)} / te {c.get('test', 0)} / va {c.get('val', 0)})"
            )
        self._counts_lbl.setText("In dataset —\n" + "\n".join(lines))

    # ------------------------------------------------------------------ #
    #  Selection helpers (used by keyboard shortcuts)                      #
    # ------------------------------------------------------------------ #

    def toggle_class_by_index(self, index: int):
        if 0 <= index < len(self._rows):
            self._rows[index].toggle()

    def set_split_for_checked(self, split: str):
        """Apply one split to every checked class (keyboard 1 / 2 / 3)."""
        targets = [r for r in self._rows if r.is_checked()]
        if not targets:
            return False
        for row in targets:
            row.set_split(split)
        self._on_row_changed()
        return True

    def get_assignments(self) -> List[Dict[str, str]]:
        """[{'class': ..., 'split': ...}] for every checked class."""
        result = []
        for row in self._rows:
            if not row.is_checked():
                continue
            split = row.split() or self._default_split
            result.append({"class": row.class_name, "split": split})
        return result

    def clear_selection(self):
        for row in self._rows:
            row.clear()
        self._update_buttons()

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _on_row_changed(self):
        # Give newly checked rows a split: auto-ratio suggestion or the sticky default
        for row in self._rows:
            if row.is_checked() and row.split() is None:
                if self.auto_split_enabled() and self._suggester:
                    row.set_split(self._suggester(row.class_name))
                else:
                    row.set_split(self._default_split)
        self._update_buttons()

    def _apply_auto_splits(self):
        if not self._suggester:
            return
        for row in self._rows:
            if row.is_checked():
                row.set_split(self._suggester(row.class_name))

    def _update_buttons(self):
        selected = any(r.is_checked() for r in self._rows)
        self._assign_btn.setEnabled(self._has_image and selected)
        self._assign_btn.setText("Update && Next" if self._assigned else "Assign && Next")
        self._remove_btn.setEnabled(self._has_image and self._assigned)
