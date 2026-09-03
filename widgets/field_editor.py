"""
SnP Image Labeler - Field Editor
Runtime telemetry-field management.
All signal connections are argument-safe.
"""

import uuid
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

FIELD_TYPES = ["Text Input", "Dropdown", "Checkboxes", "Radio Buttons", "Boolean"]

TYPE_ICON = {
    "Text Input":   "Aa",
    "Dropdown":     "vv",
    "Checkboxes":   "[]",
    "Radio Buttons":"()",
    "Boolean":      "T/F",
}


# ------------------------------------------------------------------ #
#  Data class                                                          #
# ------------------------------------------------------------------ #

class FieldDefinition:
    def __init__(
        self,
        name: str,
        field_type: str,
        options: Optional[List[str]] = None,
        field_id: Optional[str] = None,
    ):
        self.id         = field_id or str(uuid.uuid4())[:8]
        self.name       = name
        self.field_type = field_type
        self.options    = list(options) if options else []

    def to_dict(self) -> Dict:
        return {
            "id":      self.id,
            "name":    self.name,
            "type":    self.field_type,
            "options": self.options,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "FieldDefinition":
        return cls(
            name      = d.get("name", ""),
            field_type= d.get("type", "Text Input"),
            options   = list(d.get("options", [])),
            field_id  = d.get("id"),
        )


# ------------------------------------------------------------------ #
#  Add / Edit single field dialog                                      #
# ------------------------------------------------------------------ #

class SingleFieldDialog(QDialog):
    def __init__(self, field: Optional[FieldDefinition] = None, parent=None):
        super().__init__(parent)
        self._existing = field
        self.setWindowTitle("Edit Field" if field else "Add Field")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._build()
        if field:
            self._populate(field)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Field Name:"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Category, Severity, Notes...")
        layout.addWidget(self._name)

        layout.addWidget(QLabel("Field Type:"))
        self._type = QComboBox()
        self._type.addItems(FIELD_TYPES)
        # currentTextChanged emits (str) — _toggle_options accepts it
        self._type.currentTextChanged.connect(self._toggle_options)
        layout.addWidget(self._type)

        # Options block — visible only for choice-type fields
        self._opt_widget = QWidget()
        ol = QVBoxLayout(self._opt_widget)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(QLabel("Options (one per line):"))
        self._options = QTextEdit()
        self._options.setPlaceholderText("Option A\nOption B\nOption C")
        self._options.setFixedHeight(120)
        ol.addWidget(self._options)
        layout.addWidget(self._opt_widget)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._validate)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._toggle_options(self._type.currentText())

    def _toggle_options(self, ftype: str):
        # Text Input and Boolean have no options to configure
        self._opt_widget.setVisible(ftype not in ("Text Input", "Boolean"))
        self.adjustSize()

    def _populate(self, f: FieldDefinition):
        self._name.setText(f.name)
        idx = self._type.findText(f.field_type)
        if idx >= 0:
            self._type.setCurrentIndex(idx)
        if f.options:
            self._options.setPlainText("\n".join(f.options))

    def _validate(self):
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation", "Field name cannot be empty.")
            return
        ftype = self._type.currentText()
        if ftype not in ("Text Input", "Boolean"):
            opts = [o.strip() for o in self._options.toPlainText().split("\n") if o.strip()]
            if not opts:
                QMessageBox.warning(self, "Validation",
                    f"'{ftype}' requires at least one option.")
                return
        self.accept()

    def result_field(self) -> FieldDefinition:
        name  = self._name.text().strip()
        ftype = self._type.currentText()
        opts  = []
        if ftype not in ("Text Input", "Boolean"):
            opts = [o.strip() for o in self._options.toPlainText().split("\n") if o.strip()]
        fid = self._existing.id if self._existing else None
        return FieldDefinition(name, ftype, opts, fid)


# ------------------------------------------------------------------ #
#  Field manager dialog                                                #
# ------------------------------------------------------------------ #

class FieldEditorDialog(QDialog):
    def __init__(self, fields: List[Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Telemetry Fields")
        self.setMinimumSize(520, 420)
        self.setModal(True)
        self._fields: List[FieldDefinition] = [FieldDefinition.from_dict(f) for f in fields]
        self._build()
        self._refresh_list()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Define the fields shown when labeling each image:"))

        self._lst = QListWidget()
        self._lst.setMinimumHeight(220)
        # currentRowChanged emits (int) — _on_select accepts it
        self._lst.currentRowChanged.connect(self._on_select)
        layout.addWidget(self._lst)

        # Buttons row
        row = QHBoxLayout()
        self._btn_add  = QPushButton("+ Add Field")
        self._btn_edit = QPushButton("Edit")
        self._btn_del  = QPushButton("Delete")
        self._btn_up   = QPushButton("Up")
        self._btn_dn   = QPushButton("Down")

        self._btn_edit.setEnabled(False)
        self._btn_del.setEnabled(False)
        self._btn_up.setEnabled(False)
        self._btn_dn.setEnabled(False)
        self._btn_up.setFixedWidth(52)
        self._btn_dn.setFixedWidth(52)

        # clicked emits (bool) — lambda absorbs it
        self._btn_add.clicked.connect(lambda _: self._add())
        self._btn_edit.clicked.connect(lambda _: self._edit())
        self._btn_del.clicked.connect(lambda _: self._delete())
        self._btn_up.clicked.connect(lambda _: self._move_up())
        self._btn_dn.clicked.connect(lambda _: self._move_down())

        for btn in (self._btn_add, self._btn_edit, self._btn_del):
            row.addWidget(btn)
        row.addStretch()
        row.addWidget(self._btn_up)
        row.addWidget(self._btn_dn)
        layout.addLayout(row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _refresh_list(self):
        self._lst.clear()
        for f in self._fields:
            icon = TYPE_ICON.get(f.field_type, "?")
            opts_preview = (
                f"  [{', '.join(f.options[:3])}{'...' if len(f.options) > 3 else ''}]"
                if f.options else ""
            )
            item = QListWidgetItem(f"[{icon}]  {f.name}{opts_preview}")
            item.setData(Qt.UserRole, f.id)
            self._lst.addItem(item)

    def _on_select(self, row: int):
        valid = row >= 0
        n     = len(self._fields)
        self._btn_edit.setEnabled(valid)
        self._btn_del.setEnabled(valid)
        self._btn_up.setEnabled(valid and row > 0)
        self._btn_dn.setEnabled(valid and row < n - 1)

    def _add(self):
        dlg = SingleFieldDialog(parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self._fields.append(dlg.result_field())
            self._refresh_list()
            self._lst.setCurrentRow(len(self._fields) - 1)

    def _edit(self):
        row = self._lst.currentRow()
        if 0 <= row < len(self._fields):
            dlg = SingleFieldDialog(self._fields[row], parent=self)
            if dlg.exec_() == QDialog.Accepted:
                self._fields[row] = dlg.result_field()
                self._refresh_list()
                self._lst.setCurrentRow(row)

    def _delete(self):
        row = self._lst.currentRow()
        if 0 <= row < len(self._fields):
            name = self._fields[row].name
            if QMessageBox.question(
                self, "Confirm Delete",
                f"Delete field '{name}'?",
                QMessageBox.Yes | QMessageBox.No,
            ) == QMessageBox.Yes:
                del self._fields[row]
                self._refresh_list()

    def _move_up(self):
        r = self._lst.currentRow()
        if r > 0:
            self._fields[r - 1], self._fields[r] = self._fields[r], self._fields[r - 1]
            self._refresh_list()
            self._lst.setCurrentRow(r - 1)

    def _move_down(self):
        r = self._lst.currentRow()
        if r < len(self._fields) - 1:
            self._fields[r], self._fields[r + 1] = self._fields[r + 1], self._fields[r]
            self._refresh_list()
            self._lst.setCurrentRow(r + 1)

    def get_fields(self) -> List[Dict]:
        return [f.to_dict() for f in self._fields]
