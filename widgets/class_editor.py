"""
SnP Image Labeler - Class Editor
Dialog for managing the segregation class list (add / rename / delete / reorder).
Mirrors the interaction pattern of FieldEditorDialog.
"""

from typing import List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QMessageBox, QPushButton, QVBoxLayout,
)

from core.segregation_manager import class_folder_name, sanitize_class_name


class ClassEditorDialog(QDialog):
    """Edit the list of segregation classes."""

    def __init__(self, classes: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Classes")
        self.setMinimumSize(380, 420)
        self.setModal(True)
        self._classes: List[str] = [c for c in classes if c]
        self._build()
        self._refresh_list()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        hint = QLabel("Each class becomes a folder: e.g. currency → Currency_data/")
        hint.setObjectName("empty_label")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_select)
        layout.addWidget(self._list, 1)

        self._preview = QLabel("")
        self._preview.setObjectName("info_label")
        layout.addWidget(self._preview)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._btn_add    = QPushButton("Add")
        self._btn_edit   = QPushButton("Rename")
        self._btn_del    = QPushButton("Delete")
        self._btn_up     = QPushButton("↑")
        self._btn_down   = QPushButton("↓")
        for b in (self._btn_add, self._btn_edit, self._btn_del):
            b.setMinimumHeight(28)
        for b in (self._btn_up, self._btn_down):
            b.setFixedWidth(32)
            b.setMinimumHeight(28)
        self._btn_add.clicked.connect(lambda _: self._add())
        self._btn_edit.clicked.connect(lambda _: self._rename())
        self._btn_del.clicked.connect(lambda _: self._delete())
        self._btn_up.clicked.connect(lambda _: self._move(-1))
        self._btn_down.clicked.connect(lambda _: self._move(1))
        row.addWidget(self._btn_add)
        row.addWidget(self._btn_edit)
        row.addWidget(self._btn_del)
        row.addStretch()
        row.addWidget(self._btn_up)
        row.addWidget(self._btn_down)
        layout.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ------------------------------------------------------------------ #
    #  List handling                                                       #
    # ------------------------------------------------------------------ #

    def _refresh_list(self, select: int = -1):
        self._list.clear()
        for name in self._classes:
            self._list.addItem(name)
        if self._classes:
            self._list.setCurrentRow(
                min(max(select, 0), len(self._classes) - 1)
            )
        self._on_select(self._list.currentRow())

    def _on_select(self, row: int):
        valid = 0 <= row < len(self._classes)
        self._btn_edit.setEnabled(valid)
        self._btn_del.setEnabled(valid)
        self._btn_up.setEnabled(valid and row > 0)
        self._btn_down.setEnabled(valid and row < len(self._classes) - 1)
        self._preview.setText(
            f"Folder: {class_folder_name(self._classes[row])}/train | test | val"
            if valid else ""
        )

    def _ask_name(self, title: str, current: str = "") -> Optional[str]:
        text, ok = QInputDialog.getText(self, title, "Class name:", text=current)
        if not ok:
            return None
        name = (text or "").strip()
        if not name:
            QMessageBox.warning(self, "Invalid Name", "Class name cannot be empty.")
            return None
        if not sanitize_class_name(name):
            QMessageBox.warning(
                self, "Invalid Name",
                "That name contains no characters usable in a folder name.",
            )
            return None
        clash = [c for c in self._classes if c.lower() == name.lower()
                 and c.lower() != current.lower()]
        if clash:
            QMessageBox.warning(self, "Duplicate", f"'{name}' already exists.")
            return None
        return name

    def _add(self):
        name = self._ask_name("Add Class")
        if name:
            self._classes.append(name)
            self._refresh_list(len(self._classes) - 1)

    def _rename(self):
        row = self._list.currentRow()
        if not (0 <= row < len(self._classes)):
            return
        current = self._classes[row]
        name = self._ask_name("Rename Class", current)
        if not name:
            return
        QMessageBox.information(
            self, "Renamed",
            f"'{current}' → '{name}'.\n\n"
            f"Images already segregated stay in {class_folder_name(current)}/. "
            f"New assignments go to {class_folder_name(name)}/.",
        )
        self._classes[row] = name
        self._refresh_list(row)

    def _delete(self):
        row = self._list.currentRow()
        if not (0 <= row < len(self._classes)):
            return
        name = self._classes[row]
        if QMessageBox.question(
            self, "Delete Class",
            f"Remove '{name}' from the class list?\n\n"
            f"Already-segregated files in {class_folder_name(name)}/ are not touched.",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        del self._classes[row]
        self._refresh_list(row - 1)

    def _move(self, delta: int):
        row = self._list.currentRow()
        new = row + delta
        if 0 <= row < len(self._classes) and 0 <= new < len(self._classes):
            self._classes[row], self._classes[new] = self._classes[new], self._classes[row]
            self._refresh_list(new)

    # ------------------------------------------------------------------ #
    #  Result                                                              #
    # ------------------------------------------------------------------ #

    def get_classes(self) -> List[str]:
        return list(self._classes)
