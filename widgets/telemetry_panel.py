"""
SnP Image Labeler - Telemetry Panel
Dynamically renders configured telemetry fields.
All Qt signal connections are argument-safe.
"""

from typing import Any, Dict, List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QLabel,
    QLineEdit, QPushButton, QRadioButton, QScrollArea,
    QVBoxLayout, QWidget,
)


class TelemetryFieldWidget(QWidget):
    """Renders one telemetry field."""

    value_changed = pyqtSignal()

    def __init__(self, field_def: Dict, parent=None):
        super().__init__(parent)
        self._field = field_def
        self._ftype    = field_def.get("type", "Text Input")
        self._widget   = None   # QWidget | List[QWidget]
        self._grp      = None   # QButtonGroup for radio/boolean
        self._rb_true  = None   # Boolean only
        self._rb_false = None   # Boolean only
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(4)

        # Field label
        lbl = QLabel(f"{self._field.get('name', '')}:")
        lbl.setObjectName("field_label")
        layout.addWidget(lbl)

        opts: List[str] = self._field.get("options", [])

        if self._ftype == "Text Input":
            w = QLineEdit()
            w.setPlaceholderText("Enter value...")
            # textChanged emits (str) — lambda absorbs it
            w.textChanged.connect(lambda _: self.value_changed.emit())
            layout.addWidget(w)
            self._widget = w

        elif self._ftype == "Dropdown":
            w = QComboBox()
            w.addItem("-- Select --")
            w.addItems(opts)
            # currentIndexChanged emits (int) — lambda absorbs it
            w.currentIndexChanged.connect(lambda _: self.value_changed.emit())
            layout.addWidget(w)
            self._widget = w

        elif self._ftype == "Checkboxes":
            boxes = []
            for opt in opts:
                cb = QCheckBox(opt)
                # stateChanged emits (int) — lambda absorbs it
                cb.stateChanged.connect(lambda _: self.value_changed.emit())
                layout.addWidget(cb)
                boxes.append(cb)
            self._widget = boxes

        elif self._ftype == "Radio Buttons":
            buttons = []
            self._grp = QButtonGroup(self)
            self._grp.setExclusive(True)
            for i, opt in enumerate(opts):
                rb = QRadioButton(opt)
                rb.toggled.connect(lambda _: self.value_changed.emit())
                layout.addWidget(rb)
                buttons.append(rb)
                self._grp.addButton(rb, i)
            self._widget = buttons

        elif self._ftype == "Boolean":
            # Two exclusive radio buttons: True / False
            self._grp = QButtonGroup(self)
            self._grp.setExclusive(True)
            self._rb_true  = QRadioButton("True")
            self._rb_false = QRadioButton("False")
            self._rb_true.toggled.connect(lambda _: self.value_changed.emit())
            self._rb_false.toggled.connect(lambda _: self.value_changed.emit())
            layout.addWidget(self._rb_true)
            layout.addWidget(self._rb_false)
            self._grp.addButton(self._rb_true,  1)
            self._grp.addButton(self._rb_false, 0)
            self._widget = [self._rb_true, self._rb_false]

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("field_sep")
        layout.addWidget(sep)

    # ------------------------------------------------------------------ #
    #  Value access                                                        #
    # ------------------------------------------------------------------ #

    def get_value(self) -> Any:
        if self._ftype == "Text Input":
            return self._widget.text()
        if self._ftype == "Dropdown":
            return self._widget.currentText() if self._widget.currentIndex() > 0 else ""
        if self._ftype == "Checkboxes":
            return [cb.text() for cb in self._widget if cb.isChecked()]
        if self._ftype == "Radio Buttons":
            for rb in self._widget:
                if rb.isChecked():
                    return rb.text()
            return ""
        if self._ftype == "Boolean":
            if self._rb_true and self._rb_true.isChecked():
                return True
            if self._rb_false and self._rb_false.isChecked():
                return False
            return None   # nothing selected yet
        return ""

    def set_value(self, value: Any):
        try:
            if self._ftype == "Text Input":
                self._widget.setText(str(value) if value is not None else "")

            elif self._ftype == "Dropdown":
                if value:
                    idx = self._widget.findText(str(value))
                    if idx >= 0:
                        self._widget.setCurrentIndex(idx)

            elif self._ftype == "Checkboxes":
                checked = value if isinstance(value, list) else []
                for cb in self._widget:
                    cb.setChecked(cb.text() in checked)

            elif self._ftype == "Radio Buttons":
                target = str(value) if value is not None else ""
                for rb in self._widget:
                    rb.setChecked(rb.text() == target)

            elif self._ftype == "Boolean":
                if value is True or str(value).lower() == "true":
                    self._rb_true.setChecked(True)
                elif value is False or str(value).lower() == "false":
                    self._rb_false.setChecked(True)

        except Exception:
            pass  # stale/mismatched data — ignore silently

    def clear(self):
        try:
            if self._ftype == "Text Input":
                self._widget.clear()
            elif self._ftype == "Dropdown":
                self._widget.setCurrentIndex(0)
            elif self._ftype == "Checkboxes":
                for cb in self._widget:
                    cb.setChecked(False)
            elif self._ftype == "Radio Buttons" and self._grp:
                self._grp.setExclusive(False)
                for rb in self._widget:
                    rb.setChecked(False)
                self._grp.setExclusive(True)
            elif self._ftype == "Boolean" and self._grp:
                self._grp.setExclusive(False)
                if self._rb_true:  self._rb_true.setChecked(False)
                if self._rb_false: self._rb_false.setChecked(False)
                self._grp.setExclusive(True)
        except Exception:
            pass

    @property
    def field_name(self) -> str:
        return self._field.get("name", "")


# ------------------------------------------------------------------ #
#  Panel                                                               #
# ------------------------------------------------------------------ #

class TelemetryPanel(QWidget):
    """Right-side telemetry panel."""

    save_requested  = pyqtSignal()
    values_changed  = pyqtSignal()   # fired whenever any field value changes

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets: List[TelemetryFieldWidget] = []
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QLabel("Telemetry")
        hdr.setObjectName("panel_header")
        hdr.setAlignment(Qt.AlignCenter)
        root.addWidget(hdr)

        # Scrollable area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)

        self._container = QWidget()
        self._inner = QVBoxLayout(self._container)
        self._inner.setContentsMargins(12, 8, 12, 8)
        self._inner.setSpacing(0)

        self._empty_lbl = QLabel("No fields configured.\nUse 'Manage Fields' to add.")
        self._empty_lbl.setObjectName("empty_label")
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._inner.addWidget(self._empty_lbl)
        self._inner.addStretch()

        self._scroll.setWidget(self._container)
        root.addWidget(self._scroll, 1)

        # Footer with Save button
        footer = QFrame()
        footer.setObjectName("save_frame")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(12, 8, 12, 8)

        self._save_btn = QPushButton("Save Label")
        self._save_btn.setObjectName("save_label_btn")
        self._save_btn.setMinimumHeight(40)
        self._save_btn.setEnabled(False)
        # clicked emits (bool) — lambda absorbs it
        self._save_btn.clicked.connect(lambda _: self.save_requested.emit())
        fl.addWidget(self._save_btn)

        root.addWidget(footer)

    def load_fields(self, field_defs: List[Dict]):
        """Rebuild panel widgets from field definitions."""
        # Remove old widgets
        for w in self._widgets:
            self._inner.removeWidget(w)
            w.deleteLater()
        self._widgets.clear()

        has = bool(field_defs)
        self._empty_lbl.setVisible(not has)
        self._save_btn.setEnabled(has)

        for fd in field_defs:
            w = TelemetryFieldWidget(fd, self._container)
            # Insert before the trailing stretch (always last item)
            self._inner.insertWidget(self._inner.count() - 1, w)
            self._widgets.append(w)
            w.value_changed.connect(self.values_changed)

    def get_values(self) -> Dict[str, Any]:
        return {w.field_name: w.get_value() for w in self._widgets}

    def set_values(self, values: Dict[str, Any]):
        for w in self._widgets:
            if w.field_name in values:
                w.set_value(values[w.field_name])

    def clear_values(self):
        for w in self._widgets:
            w.clear()

    def has_fields(self) -> bool:
        return bool(self._widgets)
