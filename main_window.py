"""
SnP Image Labeler - Main Window

Two tasks share one shell (queue + viewer + navigation); the toolbar and the
right-hand panel swap with the active task.

  Annotation task (default)
    - Single output file per image: {image_name}.json or {image_name}.csv
    - Contains: image_name + telemetry fields + currency_country_bounding_box
      (left/top/right/bottom + the label typed for the box)
    - Autosave is dirty-driven: only a real edit writes a file, so browsing
      the queue never creates empty labels or marks images done
    - Undo/redo (Ctrl+Z / Ctrl+Y) over box add / move / resize / delete

  Segregation task
    - Copies each image into {dataset_root}/{Class}_data/{split}/
    - Multi-class with an independent train/test/val split per class
    - Progress tracked in the dataset root so a part-done folder resumes cleanly

Keyboard
    D / Right      next image           A / Left      previous image
    Ctrl+S         save                 Ctrl+Z / Y    undo / redo
    N              navigate mode        B             annotate (box) mode
    F              fit to window        R             reload image
    Ctrl+F         flag for review      Ctrl+G        review grid
    1-9            quick label (annotation) / split (segregation)
    Space / MMB    pan          Alt+Arrows  nudge box      Del  delete box
"""

import inspect
import os
import sys
import time
import traceback
from collections import deque
from pathlib import Path
from typing import List, Optional

from PyQt5.QtCore import (
    QByteArray, QEvent, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal,
)
from PyQt5.QtGui import QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractSpinBox, QAction, QApplication, QComboBox, QFileDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressDialog,
    QShortcut, QSplitter, QStackedWidget, QStatusBar, QTextEdit, QToolBar,
    QVBoxLayout, QHBoxLayout, QFrame, QTabWidget, QWidget,
)

from core.config_manager     import ConfigManager
from core.duplicate_finder   import DEFAULT_SENSITIVITY
from core.label_index        import LabelIndex, ReviewFlags
from core.label_manager      import LabelManager
from core.rejection_manager  import REJECTED_DIRNAME, move_to_rejected, rejected_dir
from core.segregation_manager import SPLITS, SPLIT_LABELS, SegregationManager
from core.session_store      import SessionStore
from core.undo_stack         import UndoStack
from widgets.annotation_viewer import AnnotationViewer, ViewerMode
from widgets.class_editor    import ClassEditorDialog
from widgets.duplicate_dialog import (
    CANCEL as DUP_CANCEL, SKIP as DUP_SKIP, UPDATE as DUP_UPDATE,
    ask_duplicate_action,
)
from widgets.duplicate_scan_dialog import DuplicateScanDialog
from widgets.image_queue     import DONE, FLAGGED, PENDING, ImageQueuePanel
from widgets.review_grid     import ReviewGridDialog
from widgets.telemetry_panel  import TelemetryPanel
from widgets.bbox_panel      import BBoxPanel
from widgets.field_editor    import FieldEditorDialog
from widgets.segregation_panel import SegregationPanel

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
IMG_FILTER     = "Images (*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.tif *.webp)"

TASK_ANNOTATION  = "annotation"
TASK_SEGREGATION = "segregation"

BBOX_FIELD   = "currency_country_bounding_box"
NAV_SETTLE_MS = 70          # coalesce held-down Next/Prev before decoding
PREFETCH_AHEAD = 2
PREFETCH_BEHIND = 1


# ------------------------------------------------------------------ #
#  Safe slot decorator — trims extra Qt signal args, catches errors   #
# ------------------------------------------------------------------ #

def _safe_slot(fn):
    n_params = len(inspect.signature(fn).parameters)

    def wrapper(*args, **kwargs):
        try:
            return fn(*args[:n_params], **kwargs)
        except Exception:
            msg = traceback.format_exc()
            try:
                log = (
                    Path(sys.executable).parent / "snp_crash.log"
                    if getattr(sys, "frozen", False)
                    else Path(__file__).parent / "snp_crash.log"
                )
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"\n[{fn.__name__}]\n{msg}\n")
            except Exception:
                pass
            QMessageBox.critical(None, "Error", f"Error in {fn.__name__}:\n\n{msg}")

    wrapper.__name__ = fn.__name__
    return wrapper


# ------------------------------------------------------------------ #
#  Folder scanner — walks a tree without freezing the UI              #
# ------------------------------------------------------------------ #

class FolderScanner(QThread):
    progress = pyqtSignal(int)          # files seen so far
    done     = pyqtSignal(list)         # image paths, sorted

    def __init__(self, folder: str, recursive: bool, parent=None):
        super().__init__(parent)
        self._folder    = folder
        self._recursive = recursive
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        found: List[str] = []
        seen = 0
        try:
            if self._recursive:
                for root, dirs, files in os.walk(self._folder):
                    if self._cancelled:
                        break
                    dirs[:] = [d for d in dirs if d != REJECTED_DIRNAME]
                    for name in files:
                        seen += 1
                        if os.path.splitext(name)[1].lower() in SUPPORTED_EXTS:
                            found.append(os.path.join(root, name))
                        if seen % 500 == 0:
                            self.progress.emit(seen)
            else:
                with os.scandir(self._folder) as entries:
                    for entry in entries:
                        if self._cancelled:
                            break
                        seen += 1
                        if entry.is_file() and os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTS:
                            found.append(entry.path)
                        if seen % 500 == 0:
                            self.progress.emit(seen)
        except OSError:
            pass
        self.done.emit([] if self._cancelled else sorted(found))


# ------------------------------------------------------------------ #
#  Main Window                                                         #
# ------------------------------------------------------------------ #

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self._config    = ConfigManager()
        self._label_mgr = LabelManager(self._config.output_directory)
        self._seg_mgr   = SegregationManager(
            self._config.get("segregation_output_directory", "")
            or self._config.segregation_output_directory
        )

        # Fast "is this image done?" — one directory scan, not 2 stats/image
        out_dir = self._config.get("output_directory", "")
        self._label_index  = LabelIndex(out_dir)
        self._review_flags = ReviewFlags(out_dir)

        self._task = self._config.active_task
        self._image_paths:   List[str] = []
        self._current_index: int       = -1

        # Edit tracking — nothing is written unless the user actually changed
        # something on this image.
        self._dirty         = False
        self._applying_undo = False
        self._undo          = UndoStack()

        # "Apply to all remaining" answer from the already-processed dialog.
        self._dup_action: Optional[str] = None
        self._dup_dialog: Optional[DuplicateScanDialog] = None
        self._grid_dialog: Optional[ReviewGridDialog] = None
        self._scanner: Optional[FolderScanner] = None

        # Debounced autosave
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._do_autosave)

        # Navigation coalescing — holding Next must not decode every image
        self._nav_timer = QTimer(self)
        self._nav_timer.setSingleShot(True)
        self._nav_timer.timeout.connect(self._settle_navigation)

        # Session persistence (debounced — the queue can be very large)
        self._session = SessionStore()
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.timeout.connect(self._save_session)

        # Throughput samples for the ETA readout
        self._completions: deque = deque(maxlen=40)

        self.setWindowTitle("SnP Image Labeler")
        self.setMinimumSize(1150, 700)

        self._build_ui()
        self._build_toolbar()
        self._build_statusbar()
        self._bind_shortcuts()
        self._load_fields()
        self._load_segregation_settings()
        self._restore_geometry()
        self._apply_task(self._task, initial=True)
        self._apply_viewer_preferences()

        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self._offer_resume)

    # ================================================================== #
    #  UI Construction                                                     #
    # ================================================================== #

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setChildrenCollapsible(False)

        # ── Left: Image Queue ─────────────────────────────────────
        self._queue = ImageQueuePanel()
        self._queue.setMinimumWidth(200)
        self._queue.setMaximumWidth(320)
        self._queue.image_selected.connect(self._on_queue_selection)
        self._queue.remove_requested.connect(self._remove_from_queue)
        self._queue.delete_requested.connect(self._delete_image)
        self._queue.flag_requested.connect(self._toggle_flag_for)
        self._queue.clear_all_requested.connect(self._clear_queue)
        splitter.addWidget(self._queue)

        # ── Centre: Viewer + info bar ─────────────────────────────
        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        self._viewer = AnnotationViewer(cache_budget_mb=self._config.image_cache_mb)
        self._viewer.zoom_changed.connect(self._on_zoom)
        self._viewer.boxes_changed.connect(self._on_boxes_changed)
        self._viewer.selection_changed.connect(self._on_selection_changed)
        self._viewer.image_loaded.connect(self._on_image_loaded)
        self._viewer.image_failed.connect(self._on_image_failed)
        self._viewer.status_message.connect(self._set_status)
        cv.addWidget(self._viewer, 1)

        # Info bar
        info_bar = QFrame()
        info_bar.setObjectName("info_bar")
        info_bar.setFixedHeight(30)
        ib = QHBoxLayout(info_bar)
        ib.setContentsMargins(10, 0, 10, 0)
        ib.setSpacing(12)

        self._lbl_name = QLabel("No image loaded")
        self._lbl_name.setObjectName("info_label")
        ib.addWidget(self._lbl_name)
        ib.addStretch()

        self._lbl_dims = QLabel("")
        self._lbl_dims.setObjectName("info_label")
        ib.addWidget(self._lbl_dims)

        self._lbl_size = QLabel("")
        self._lbl_size.setObjectName("info_label")
        ib.addWidget(self._lbl_size)

        self._lbl_zoom = QLabel("100%")
        self._lbl_zoom.setObjectName("info_label")
        self._lbl_zoom.setFixedWidth(55)
        self._lbl_zoom.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ib.addWidget(self._lbl_zoom)

        cv.addWidget(info_bar)
        splitter.addWidget(centre)

        # ── Right: task-dependent stack ───────────────────────────
        self._right_stack = QStackedWidget()
        self._right_stack.setObjectName("right_stack")
        self._right_stack.setMinimumWidth(300)
        self._right_stack.setMaximumWidth(420)

        # Page 0 — Annotation task (Telemetry + Annotations tabs)
        self._right_tabs = QTabWidget()
        self._right_tabs.setObjectName("right_tabs")

        self._telemetry = TelemetryPanel()
        self._telemetry.save_requested.connect(self._manual_save)
        self._telemetry.values_changed.connect(self._on_telemetry_edited)
        self._right_tabs.addTab(self._telemetry, "Telemetry")

        self._bbox_panel = BBoxPanel()
        self._bbox_panel.box_select_requested.connect(self._on_panel_box_select)
        self._bbox_panel.box_delete_requested.connect(self._on_panel_box_delete)
        self._bbox_panel.clear_all_requested.connect(self._clear_all_boxes)
        self._right_tabs.addTab(self._bbox_panel, "Annotations")
        self._right_stack.addWidget(self._right_tabs)

        # Page 1 — Segregation task
        self._seg_panel = SegregationPanel()
        self._seg_panel.assign_requested.connect(self._seg_assign)
        self._seg_panel.unassign_requested.connect(self._seg_unassign)
        self._seg_panel.manage_classes_requested.connect(self._manage_classes)
        self._seg_panel.classes_changed.connect(self._on_classes_changed)
        self._seg_panel.settings_changed.connect(self._save_segregation_settings)
        self._seg_panel.set_split_suggester(self._suggest_split)
        self._right_stack.addWidget(self._seg_panel)

        splitter.addWidget(self._right_stack)
        splitter.setSizes([250, 660, 330])
        splitter.setStretchFactor(1, 1)
        rl.addWidget(splitter)

    def _build_toolbar(self):
        tb = QToolBar("Toolbar")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        def act(label, tip, slot):
            a = QAction(label, self)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        self._annot_only: List[QAction] = []
        self._seg_only:   List[QAction] = []

        # ── Task switch ───────────────────────────────────────────
        tb.addWidget(QLabel("  Task: "))
        self._act_task_annot = QAction("Annotation", self)
        self._act_task_annot.setToolTip("Annotation task — telemetry + bounding box  (Ctrl+1)")
        self._act_task_annot.setCheckable(True)
        self._act_task_annot.triggered.connect(lambda: self._set_task(TASK_ANNOTATION))
        tb.addAction(self._act_task_annot)

        self._act_task_seg = QAction("Segregation", self)
        self._act_task_seg.setToolTip("Segregation task — sort images into class folders  (Ctrl+2)")
        self._act_task_seg.setCheckable(True)
        self._act_task_seg.triggered.connect(lambda: self._set_task(TASK_SEGREGATION))
        tb.addAction(self._act_task_seg)

        tb.addSeparator()
        act("Import",  "Import images or folder  (Ctrl+O)", self._import)
        act("Find Duplicates",
            "Scan for the same picture stored more than once  (Ctrl+D)",
            self._find_duplicates)
        act("Review Grid",
            "Contact sheet of the queue with saved boxes drawn on  (Ctrl+G)",
            self._open_review_grid)
        self._act_delete = act(
            "Delete Image",
            "Move this image out of the working set into _rejected  (Shift+Del)",
            self._delete_current_image)
        delete_button = tb.widgetForAction(self._act_delete)
        if delete_button is not None:
            delete_button.setObjectName("danger_tool_btn")
        tb.addSeparator()
        act("Prev",    "Previous image  (A / Left)",         self._prev)
        act("Next",    "Next image  (D / Right)",            self._next)
        self._act_flag = QAction("Flag", self)
        self._act_flag.setToolTip("Flag this image for review  (Ctrl+F)")
        self._act_flag.setCheckable(True)
        self._act_flag.triggered.connect(self._toggle_flag)
        tb.addAction(self._act_flag)
        tb.addSeparator()
        act("Zoom+",   "Zoom In  (Ctrl++)",                  self._zoom_in)
        act("Zoom-",   "Zoom Out  (Ctrl+-)",                 self._zoom_out)
        act("Fit",     "Fit to window  (F)",                 self._fit)

        self._act_zoom_lock = QAction("Lock Zoom", self)
        self._act_zoom_lock.setToolTip(
            "Keep the current zoom and pan when moving to the next image  (Ctrl+L)")
        self._act_zoom_lock.setCheckable(True)
        self._act_zoom_lock.triggered.connect(self._toggle_zoom_lock)
        tb.addAction(self._act_zoom_lock)

        act("Refresh", "Reload this image from disk  (R)",   self._refresh)
        tb.addSeparator()

        # ── Annotation-only controls ──────────────────────────────
        self._annot_only.append(
            act("Undo", "Undo the last box change  (Ctrl+Z)", self._undo_action))
        self._annot_only.append(
            act("Redo", "Redo  (Ctrl+Y)", self._redo_action))
        self._annot_only.append(tb.addSeparator())

        self._annot_only.append(tb.addWidget(QLabel("  Format: ")))
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(["CSV", "JSON"])
        self._fmt_combo.setCurrentText(self._config.export_format.upper())
        self._fmt_combo.setFixedWidth(78)
        self._fmt_combo.currentTextChanged.connect(self._on_format_changed)
        self._annot_only.append(tb.addWidget(self._fmt_combo))

        self._annot_only.append(
            act("Export", "Export label file to custom location", self._export))
        self._annot_only.append(tb.addSeparator())
        self._annot_only.append(
            act("Output Dir", "Set label output directory", self._set_output_dir))
        self._annot_only.append(
            act("Manage Fields", "Add / edit telemetry fields", self._manage_fields))
        self._annot_only.append(tb.addSeparator())

        self._annot_only.append(tb.addWidget(QLabel("  Mode: ")))
        self._act_nav = QAction("Navigate", self)
        self._act_nav.setToolTip("Navigate mode — pan & zoom  (N)")
        self._act_nav.setCheckable(True)
        self._act_nav.setChecked(True)
        self._act_nav.triggered.connect(self._mode_navigate)
        tb.addAction(self._act_nav)
        self._annot_only.append(self._act_nav)

        self._act_annot = QAction("Annotate", self)
        self._act_annot.setToolTip("Annotate mode — draw bounding box  (B)")
        self._act_annot.setCheckable(True)
        self._act_annot.setChecked(False)
        self._act_annot.triggered.connect(self._mode_annotate)
        tb.addAction(self._act_annot)
        self._annot_only.append(self._act_annot)

        self._act_sticky = QAction("Sticky Label", self)
        self._act_sticky.setToolTip(
            "Reuse the last label for new boxes instead of asking every time")
        self._act_sticky.setCheckable(True)
        self._act_sticky.triggered.connect(self._toggle_sticky_label)
        tb.addAction(self._act_sticky)
        self._annot_only.append(self._act_sticky)

        self._annot_only.append(tb.addSeparator())
        self._annot_only.append(
            act("Del Box", "Delete selected box  (Del)", self._delete_selected_box))

        # ── Segregation-only controls ─────────────────────────────
        self._seg_only.append(
            act("Dataset Dir", "Set the dataset root where class folders are created",
                self._set_dataset_dir))
        self._seg_only.append(
            act("Manage Classes", "Add / rename / reorder segregation classes",
                self._manage_classes))
        self._seg_only.append(tb.addSeparator())
        self._seg_only.append(
            act("Assign", "Copy this image into the selected class folders  (Enter)",
                self._seg_assign))
        self._seg_only.append(
            act("Open Dataset", "Open the dataset folder in the file browser",
                self._open_dataset_dir))
        self._seg_only.append(
            act("Open Record", "Open processed_images.csv — the log of every processed image",
                self._open_record_file))

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_msg = QLabel("Ready — Import images to get started.")
        sb.addWidget(self._sb_msg, 1)

        self._sb_progress = QLabel("")
        self._sb_progress.setObjectName("outdir_label")
        sb.addPermanentWidget(self._sb_progress)

        self._sb_stats = QLabel("")
        self._sb_stats.setObjectName("outdir_label")
        self._sb_stats.setVisible(False)
        sb.addPermanentWidget(self._sb_stats)

        self._sb_outdir = QLabel(f"Output: {self._config.output_directory}")
        self._sb_outdir.setObjectName("outdir_label")
        sb.addPermanentWidget(self._sb_outdir)

    def _bind_shortcuts(self):
        # Single letters (A / D / N / B / F / R / digits) are handled in
        # eventFilter so they never steal a keystroke from a text field.
        QShortcut(QKeySequence("Left"),      self, self._prev)
        QShortcut(QKeySequence("Right"),     self, self._next)
        QShortcut(QKeySequence("Ctrl+S"),    self, self._manual_save)
        QShortcut(QKeySequence("Ctrl+Z"),    self, self._undo_action)
        QShortcut(QKeySequence("Ctrl+Y"),    self, self._redo_action)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, self._redo_action)
        QShortcut(QKeySequence("Ctrl+O"),    self, self._import)
        QShortcut(QKeySequence("Ctrl+D"),    self, self._find_duplicates)
        QShortcut(QKeySequence("Ctrl+F"),    self, self._toggle_flag)
        QShortcut(QKeySequence("Ctrl+G"),    self, self._open_review_grid)
        QShortcut(QKeySequence("Ctrl+L"),    self, self._toggle_zoom_lock)
        QShortcut(QKeySequence("Shift+Del"), self, self._delete_current_image)
        QShortcut(QKeySequence("Ctrl++"),    self, self._zoom_in)
        QShortcut(QKeySequence("Ctrl+="),    self, self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"),    self, self._zoom_out)
        QShortcut(QKeySequence("Ctrl+0"),    self, self._zoom_reset)

        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._set_task(TASK_ANNOTATION))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._set_task(TASK_SEGREGATION))

    def _restore_geometry(self):
        geom = self._config.get("window_geometry")
        if geom:
            try:
                self.restoreGeometry(QByteArray.fromHex(geom.encode()))
                return
            except Exception:
                pass
        self.resize(1300, 820)

    def _apply_viewer_preferences(self):
        locked = self._config.zoom_locked
        self._viewer.set_zoom_locked(locked)
        self._act_zoom_lock.setChecked(locked)

        sticky = self._config.annotation_sticky_label
        recent = self._config.annotation_recent_labels
        self._viewer.set_recent_labels(recent)
        self._viewer.set_sticky_label(sticky, recent[0] if recent else "")
        self._viewer.set_max_boxes(self._config.annotation_max_boxes)
        self._act_sticky.setChecked(sticky)

    # ================================================================== #
    #  Keyboard — application-wide filter                                 #
    # ================================================================== #

    @staticmethod
    def _is_text_entry(widget) -> bool:
        """True when a keystroke belongs to the widget, not to a shortcut."""
        return isinstance(widget, (QLineEdit, QAbstractSpinBox, QTextEdit,
                                   QPlainTextEdit, QComboBox))

    def eventFilter(self, obj, event):
        if event.type() != QEvent.KeyPress:
            return False
        if not self.isActiveWindow():
            return False                       # a dialog owns the keyboard
        if self._is_text_entry(QApplication.focusWidget()):
            return False
        mods = event.modifiers()
        if mods & (Qt.ControlModifier | Qt.MetaModifier):
            return False                       # Ctrl+… handled by QShortcut
        key = event.key()

        if key == Qt.Key_D and not (mods & Qt.AltModifier):
            self._next()
            return True
        if key == Qt.Key_A and not (mods & Qt.AltModifier):
            self._prev()
            return True
        if key == Qt.Key_N:
            self._mode_navigate()
            return True
        if key == Qt.Key_B:
            self._mode_annotate()
            return True
        if key == Qt.Key_F:
            self._fit()
            return True
        if key == Qt.Key_R:
            self._refresh()
            return True
        if (key == Qt.Key_Delete and self._task == TASK_ANNOTATION
                and not (mods & Qt.ShiftModifier)):   # Shift+Del = reject image
            # Works wherever the focus is, not only inside the viewer
            if self._viewer.delete_selected_boxes():
                self._set_status("Box deleted  (Ctrl+Z to undo)")
                return True
            return False
        if Qt.Key_1 <= key <= Qt.Key_9:
            return self._handle_digit(key - Qt.Key_0)
        return False

    def _handle_digit(self, digit: int) -> bool:
        if self._task == TASK_SEGREGATION:
            if 1 <= digit <= len(SPLITS):
                self._seg_set_split(SPLITS[digit - 1])
                return True
            return False
        labels = self._viewer.existing_labels()
        if digit <= len(labels):
            label = labels[digit - 1]
            if self._viewer.apply_label_to_selection(label):
                self._set_status(f"Label: {label}")
            else:
                self._viewer.set_sticky_label(self._act_sticky.isChecked(), label)
                self._set_status(f"Next box will be labelled: {label}")
            return True
        return False

    # ================================================================== #
    #  Output directory — ensure set before saving                        #
    # ================================================================== #

    def _ensure_output_dir(self) -> bool:
        raw = self._config.get("output_directory", "")
        if raw:
            return True

        QMessageBox.information(
            self,
            "Output Directory Required",
            "Please select an output directory where label files will be saved.",
        )
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", str(Path.home()),
        )
        if not folder:
            self._set_status("Output directory not set — label file not saved.")
            return False

        self._set_output_directory(folder)
        self._set_status(f"Output directory set: {folder}")
        return True

    def _set_output_directory(self, folder: str):
        """Point every label-aware component at a new output directory."""
        self._config.output_directory = folder
        self._label_mgr.set_output_directory(folder)
        self._label_index.set_directory(folder)
        self._label_index.rebuild()
        self._review_flags.set_directory(folder)
        self._review_flags.load()
        self._sb_outdir.setText(f"Output: {folder}")
        if self._task == TASK_ANNOTATION:
            self._queue.refresh_status(self._status_checker())
            self._update_progress_readout()

    # ================================================================== #
    #  Task switching                                                      #
    # ================================================================== #

    @_safe_slot
    def _set_task(self, task: str):
        if task == self._task:
            self._sync_task_actions()
            return
        if self._task == TASK_ANNOTATION:
            self._flush_autosave()
        self._apply_task(task)

    def _apply_task(self, task: str, initial: bool = False):
        self._task = task if task in (TASK_ANNOTATION, TASK_SEGREGATION) else TASK_ANNOTATION
        self._config.active_task = self._task
        seg = self._task == TASK_SEGREGATION

        self._sync_task_actions()
        for action in self._annot_only:
            action.setVisible(not seg)
        for action in self._seg_only:
            action.setVisible(seg)
        self._act_flag.setVisible(not seg)
        self._right_stack.setCurrentIndex(1 if seg else 0)

        if seg:
            self._viewer.set_mode(ViewerMode.NAVIGATE)
            self._act_nav.setChecked(True)
            self._act_annot.setChecked(False)
            self._queue.set_status_labels("Sorted")
            self._sb_outdir.setText(
                f"Dataset: {self._config.segregation_output_directory}")
        else:
            self._queue.set_status_labels("Labeled")
            self._sb_outdir.setText(f"Output: {self._config.output_directory}")
            self._label_index.set_directory(self._config.get("output_directory", ""))
            self._review_flags.set_directory(self._config.get("output_directory", ""))
            self._label_index.rebuild()
            self._review_flags.load()

        self._sb_stats.setVisible(seg)
        self._queue.refresh_status(self._status_checker())
        self._update_progress_readout()

        # Reload the current image under the new task. Blanking the index
        # first skips the autosave flush — switching tasks never writes.
        if 0 <= self._current_index < len(self._image_paths):
            index, self._current_index = self._current_index, -1
            self._dirty = False
            self._navigate(index)
        else:
            self._refresh_seg_view()

        if not initial:
            self._set_status(
                "Task: Segregation — sort images into class folders."
                if seg else "Task: Annotation — telemetry + bounding box."
            )

    def _sync_task_actions(self):
        self._act_task_annot.setChecked(self._task == TASK_ANNOTATION)
        self._act_task_seg.setChecked(self._task == TASK_SEGREGATION)

    def _status_checker(self):
        """
        The queue's per-row state test for the active task.

        Flagged wins over done on purpose: an image someone marked for review
        is not finished work, and it must stay findable.
        """
        if self._task == TASK_SEGREGATION:
            return lambda p: DONE if self._seg_mgr.is_done(p) else PENDING

        def state(path: str) -> int:
            if self._review_flags.is_flagged(path):
                return FLAGGED
            return DONE if self._label_index.has(path) else PENDING
        return state

    # ================================================================== #
    #  Segregation — settings & classes                                    #
    # ================================================================== #

    def _load_segregation_settings(self):
        self._seg_panel.set_auto_split(self._config.segregation_auto_split)
        self._seg_panel.set_ratios(self._config.segregation_ratios)
        self._seg_panel.set_sticky(self._config.segregation_sticky_selection)
        self._seg_panel.set_default_split(self._config.segregation_last_split)
        self._seg_panel.load_classes(self._config.segregation_classes)

    @_safe_slot
    def _save_segregation_settings(self):
        self._config.segregation_auto_split       = self._seg_panel.auto_split_enabled()
        self._config.segregation_ratios           = self._seg_panel.ratios()
        self._config.segregation_sticky_selection = self._seg_panel.sticky_enabled()

    def _suggest_split(self, class_name: str) -> str:
        return self._seg_mgr.suggest_split(class_name, self._seg_panel.ratios())

    @_safe_slot
    def _manage_classes(self):
        dlg = ClassEditorDialog(self._config.segregation_classes, self)
        if dlg.exec_():
            self._on_classes_changed(dlg.get_classes())

    @_safe_slot
    def _on_classes_changed(self, classes: List[str]):
        self._config.segregation_classes = list(classes)
        self._seg_panel.load_classes(self._config.segregation_classes)
        self._refresh_seg_view()
        self._set_status(f"Classes updated — {len(classes)} class(es).")

    # ================================================================== #
    #  Segregation — dataset directory                                     #
    # ================================================================== #

    def _ensure_dataset_dir(self) -> bool:
        if self._config.get("segregation_output_directory", ""):
            return True
        QMessageBox.information(
            self, "Dataset Directory Required",
            "Choose the dataset root.\n\n"
            "Class folders ({Class}_data/train|test|val) are created inside it.",
        )
        return self._choose_dataset_dir()

    @_safe_slot
    def _set_dataset_dir(self):
        self._choose_dataset_dir()

    def _choose_dataset_dir(self) -> bool:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Dataset Root", self._config.segregation_output_directory,
        )
        if not folder:
            self._set_status("Dataset directory not set — nothing was copied.")
            return False

        self._config.segregation_output_directory = folder
        self._dup_action = None
        self._seg_mgr.set_root(folder)
        healed = self._seg_mgr.verify()
        self._sb_outdir.setText(f"Dataset: {folder}")
        self._queue.refresh_status(self._status_checker())
        self._refresh_seg_view()

        msg = f"Dataset root: {folder}"
        if self._seg_mgr.done_count():
            msg += f"  —  resuming, {self._seg_mgr.done_count()} image(s) already sorted"
        if healed:
            msg += f"  ({healed} entry(ies) reset to pending)"
        self._set_status(msg)
        return True

    @_safe_slot
    def _open_record_file(self):
        record = Path(self._seg_mgr.record_path)
        if record.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record)))
        else:
            self._set_status(
                "No record yet — it is created when the first image is assigned.")

    @_safe_slot
    def _open_dataset_dir(self):
        root = self._config.segregation_output_directory
        if root and Path(root).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(root))
        else:
            self._set_status("Dataset directory does not exist yet.")

    # ================================================================== #
    #  Segregation — assign / remove                                       #
    # ================================================================== #

    @_safe_slot
    def _seg_assign(self):
        if self._task != TASK_SEGREGATION:
            return
        path = self._current_image_path()
        if not path:
            self._set_status("No image selected.")
            return

        assignments = self._seg_panel.get_assignments()
        if not assignments:
            self._set_status("Select at least one class before assigning.")
            return
        if not self._ensure_dataset_dir():
            return

        record = self._seg_mgr.find_record(path)
        if record:
            action = self._dup_action
            if action is None:
                action, remember = ask_duplicate_action(self, path, record)
                if remember and action in (DUP_SKIP, DUP_UPDATE):
                    self._dup_action = action
            if action == DUP_CANCEL:
                self._set_status("Cancelled — the image was left as it is.")
                return
            if action == DUP_SKIP:
                name = Path(path).name
                self._queue.mark_labeled(path, True)
                self._advance_after_assign()
                self._set_status(
                    f"Skipped {name} — already processed."
                    + (f"  Skipping all further duplicates."
                       if self._dup_action == DUP_SKIP else "")
                )
                return

        ok, msg = self._seg_mgr.assign(path, assignments)
        if not ok:
            QMessageBox.critical(self, "Segregation Error", msg)
            self._set_status(msg)
            return

        last_split = assignments[-1]["split"]
        if self._config.segregation_last_split != last_split:
            self._config.segregation_last_split = last_split
        self._seg_panel.set_default_split(last_split)
        self._queue.mark_labeled(path, True)
        self._record_completion()

        summary = f"{Path(path).name} → {msg}"
        if record and self._dup_action == DUP_UPDATE:
            summary += "  (updating all further duplicates)"
        self._advance_after_assign()
        self._set_status(summary)

    def _advance_after_assign(self):
        """Jump to the next image that still needs sorting (wraps once)."""
        nxt = self._queue.first_pending_index(self._current_index + 1)
        if nxt < 0:
            nxt = self._queue.first_pending_index(0)
        if nxt < 0:
            self._refresh_seg_view()
            self._set_status("All images in the queue are segregated.")
            return
        self._navigate(nxt)

    @_safe_slot
    def _seg_unassign(self):
        path = self._current_image_path()
        if not path:
            return
        if QMessageBox.question(
            self, "Remove from Dataset",
            f"Delete the copies made for {Path(path).name}?\n\n"
            "The source image is not touched.",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        ok, msg = self._seg_mgr.unassign(path)
        if ok:
            self._queue.mark_labeled(path, False)
            self._refresh_seg_view()
        self._set_status(msg)

    @_safe_slot
    def _seg_set_split(self, split: str):
        """Keyboard 1 / 2 / 3 — set the split for the checked classes."""
        if self._task != TASK_SEGREGATION:
            return
        if self._config.segregation_last_split != split:
            self._config.segregation_last_split = split
        self._seg_panel.set_default_split(split)
        if self._seg_panel.set_split_for_checked(split):
            self._set_status(f"Split: {SPLIT_LABELS[split]}")
        else:
            self._set_status(f"Default split: {SPLIT_LABELS[split]} — now pick a class.")

    def _refresh_seg_view(self):
        """Sync the segregation panel + counters with the current image."""
        path = self._current_image_path()
        self._seg_panel.load_image_state(
            bool(path),
            self._seg_mgr.assignments(path) if path else [],
            self._seg_mgr.destinations(path) if path else [],
        )
        self._seg_panel.set_counts(self._seg_mgr.all_counts())
        total = len(self._image_paths)
        self._sb_stats.setText(f"Segregated: {self._queue.done_count} / {total}")

    # ================================================================== #
    #  Autosave — dirty-driven                                             #
    # ================================================================== #

    def _mark_dirty(self):
        self._dirty = True
        self._schedule_autosave()

    def _schedule_autosave(self, *args):
        """Debounced: coalesces rapid changes into a single save after 400 ms."""
        if self._task != TASK_ANNOTATION or not self._dirty:
            return
        if self._current_index >= 0 and self._image_paths:
            self._autosave_timer.start(400)

    def _do_autosave(self, prompt: bool = True):
        """
        Write the label file — but only when the user actually changed
        something. Merely opening an image must never create a file.
        """
        if self._task != TASK_ANNOTATION or not self._dirty:
            return
        path = self._current_image_path()
        if not path:
            return
        if prompt:
            if not self._ensure_output_dir():
                return
        elif not self._config.get("output_directory", ""):
            self._set_status("Output directory not set — label not saved.")
            return
        self._write_label_file(path, silent=True)

    def _flush_autosave(self):
        """Commit pending changes silently (no output-directory prompt)."""
        self._autosave_timer.stop()
        if self._dirty:
            self._do_autosave(prompt=False)

    def _write_label_file(self, path: str, silent: bool = False) -> bool:
        """Build and save the unified label file for the given image."""
        fmt        = self._config.export_format
        image_name = Path(path).name
        telemetry  = self._telemetry.get_values()
        bbox       = self._viewer.get_single_box()

        values = {
            "image_name": image_name,
            **telemetry,
            BBOX_FIELD: bbox,
        }

        was_done = self._label_index.has(path)
        ok = self._label_mgr.save_label(path, values, fmt)
        if ok:
            self._dirty = False
            self._label_index.mark(path)
            if not self._review_flags.is_flagged(path):
                self._queue.mark_labeled(path, True)
            if not was_done:
                self._record_completion()
            self._remember_box_label(bbox.get("label", ""))
            self._update_progress_readout()
            if not silent:
                stem = Path(path).stem
                self._set_status(
                    f"Saved: {stem}.{fmt}  →  {self._config.output_directory}"
                )
        else:
            if not silent:
                QMessageBox.critical(self, "Save Error", "Failed to write label file.")
        return ok

    def _remember_box_label(self, label: str):
        if not label:
            return
        recent = self._config.annotation_recent_labels
        if recent and recent[0] == label:
            return
        recent = [label] + [l for l in recent if l != label]
        self._config.annotation_recent_labels = recent
        self._viewer.set_recent_labels(recent)

    # ================================================================== #
    #  Progress / ETA                                                      #
    # ================================================================== #

    def _record_completion(self):
        self._completions.append(time.monotonic())

    def _update_progress_readout(self):
        total = len(self._image_paths)
        if not total:
            self._sb_progress.setText("")
            return
        done    = self._queue.done_count
        flagged = self._queue.flagged_count
        pending = max(0, total - done - flagged)
        text = f"{done}/{total} done"
        if flagged:
            text += f"  ·  {flagged} flagged"

        if len(self._completions) >= 3:
            span = self._completions[-1] - self._completions[0]
            if span > 0:
                rate = (len(self._completions) - 1) / span      # images/sec
                if rate > 0:
                    text += f"  ·  {rate * 60:.0f}/min"
                    if pending:
                        eta = pending / rate
                        text += f"  ·  ETA {self._format_eta(eta)}"
        self._sb_progress.setText(text)

    @staticmethod
    def _format_eta(seconds: float) -> str:
        seconds = int(max(0, seconds))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"

    # ================================================================== #
    #  Fields                                                              #
    # ================================================================== #

    def _load_fields(self):
        self._telemetry.load_fields(self._config.telemetry_fields)

    @_safe_slot
    def _manage_fields(self):
        dlg = FieldEditorDialog(self._config.telemetry_fields, self)
        if dlg.exec_():
            fields = dlg.get_fields()
            self._config.telemetry_fields = fields
            self._telemetry.load_fields(fields)
            self._reload_current_annotation()
            self._set_status(f"Fields updated — {len(fields)} field(s).")

    # ================================================================== #
    #  Mode / viewer toggles                                               #
    # ================================================================== #

    @_safe_slot
    def _mode_navigate(self):
        self._viewer.set_mode(ViewerMode.NAVIGATE)
        self._act_nav.setChecked(True)
        self._act_annot.setChecked(False)
        self._set_status("Mode: Navigate — pan and zoom. Press B to draw a box.")

    @_safe_slot
    def _mode_annotate(self):
        if self._task != TASK_ANNOTATION:
            self._set_status("Switch to the Annotation task to draw boxes  (Ctrl+1).")
            return
        self._viewer.set_mode(ViewerMode.ANNOTATE)
        self._act_nav.setChecked(False)
        self._act_annot.setChecked(True)
        self._set_status(
            "Mode: Annotate — drag to draw. Space or middle-mouse pans, "
            "Alt+Arrows nudge, Del deletes, Ctrl+Z undoes."
        )

    @_safe_slot
    def _toggle_zoom_lock(self):
        locked = not self._viewer.zoom_locked
        self._viewer.set_zoom_locked(locked)
        self._config.zoom_locked = locked
        self._act_zoom_lock.setChecked(locked)
        self._set_status(
            "Zoom locked — the next image keeps this zoom and pan."
            if locked else "Zoom unlocked — each image is fitted to the window."
        )

    @_safe_slot
    def _toggle_sticky_label(self):
        enabled = self._act_sticky.isChecked()
        self._config.annotation_sticky_label = enabled
        recent = self._config.annotation_recent_labels
        self._viewer.set_sticky_label(enabled, recent[0] if recent else "")
        if enabled:
            label = self._viewer.sticky_label or "(none yet)"
            self._set_status(f"Sticky label on — new boxes get: {label}")
        else:
            self._set_status("Sticky label off — you will be asked for each box.")

    @_safe_slot
    def _on_format_changed(self, text: str):
        self._config.export_format = text.lower()
        self._label_index.rebuild()
        if self._task == TASK_ANNOTATION:
            self._queue.refresh_status(self._status_checker())

    # ================================================================== #
    #  Import                                                              #
    # ================================================================== #

    @_safe_slot
    def _import(self):
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Import Images")
        dlg.setText("What would you like to import?")
        btn_files  = dlg.addButton("Select Images",  QMessageBox.ActionRole)
        btn_folder = dlg.addButton("Select Folder",  QMessageBox.ActionRole)
        dlg.addButton(QMessageBox.Cancel)
        dlg.exec_()
        clicked = dlg.clickedButton()
        if clicked == btn_files:
            self._import_files()
        elif clicked == btn_folder:
            self._import_folder()

    def _import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Images",
            self._config.last_import_directory, IMG_FILTER,
        )
        if paths:
            self._config.last_import_directory = str(Path(paths[0]).parent)
            self._enqueue(paths)

    def _import_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder", self._config.last_import_directory,
        )
        if not folder:
            return
        self._config.last_import_directory = folder

        top_level = self._scan_folder(folder, recursive=False)
        if top_level is None:
            return                                   # cancelled
        if top_level:
            self._enqueue(top_level)
            return

        nested = self._scan_folder(folder, recursive=True)
        if not nested:
            QMessageBox.information(self, "No Images",
                                    "No supported images found in that folder.")
            return
        if QMessageBox.question(
            self, "Include Sub-folders?",
            f"No images directly in that folder, but {len(nested)} found in "
            f"sub-folders. Import them?",
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes:
            self._enqueue(nested)

    def _scan_folder(self, folder: str, recursive: bool) -> Optional[List[str]]:
        """
        Walk a folder on a worker thread behind a cancellable progress dialog.
        Returns the image paths, or None when the user cancelled.
        """
        progress = QProgressDialog(
            f"Scanning {folder}…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Importing")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(250)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        result: dict = {}
        scanner = FolderScanner(folder, recursive, self)
        scanner.progress.connect(
            lambda n: progress.setLabelText(f"Scanning {folder}…\n{n:,} files seen")
        )
        scanner.done.connect(lambda paths: result.setdefault("paths", paths))
        progress.canceled.connect(scanner.cancel)

        self._scanner = scanner
        scanner.start()
        while scanner.isRunning():
            QApplication.processEvents()
            scanner.wait(30)
        QApplication.processEvents()          # let the queued done signal land
        progress.close()
        self._scanner = None

        paths = result.get("paths", [])
        if progress.wasCanceled():
            self._set_status("Import cancelled.")
            return None
        return paths

    def _enqueue(self, paths: List[str]):
        self._dup_action = None
        existing = set(self._image_paths)
        new = [p for p in paths if p not in existing]
        if not new:
            self._set_status("All selected images already in queue.")
            return

        if self._task == TASK_ANNOTATION:
            self._label_index.rebuild()
            self._review_flags.load()
        checker = self._status_checker()

        first_import = not self._image_paths
        if first_import:
            self._image_paths = list(new)
            self._queue.load_images(new, checker)
        else:
            self._image_paths.extend(new)
            self._queue.add_images(new, checker)

        if first_import:
            self._current_index = -1
            self._navigate(0)
        if self._task == TASK_SEGREGATION:
            self._refresh_seg_view()
        self._update_progress_readout()
        self._touch_session()
        self._set_status(f"Added {len(new)} image(s). Total: {len(self._image_paths)}")

    # ================================================================== #
    #  Session resume                                                      #
    # ================================================================== #

    def _touch_session(self):
        self._session_timer.start(2500)

    def _save_session(self):
        if self._image_paths:
            self._session.save(self._image_paths, self._current_index, self._task)
        else:
            self._session.clear()

    @_safe_slot
    def _offer_resume(self):
        if not self._config.resume_session or self._image_paths:
            return
        data = self._session.load()
        if not data:
            return
        paths = self._session.existing(data.get("queue", []))
        if not paths:
            self._session.clear()
            return

        missing = len(data.get("queue", [])) - len(paths)
        when    = data.get("saved_at", "")
        answer = QMessageBox.question(
            self, "Resume Last Session",
            f"Restore the queue from your last session?\n\n"
            f"    {len(paths):,} image(s)"
            + (f"  ({missing} no longer on disk)" if missing else "")
            + (f"\n    Saved: {when}" if when else "")
            + "\n\nChoosing No starts empty; the session is kept either way.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        task = data.get("task", self._task)
        if task in (TASK_ANNOTATION, TASK_SEGREGATION) and task != self._task:
            self._apply_task(task)

        self._enqueue(paths)
        index = max(0, min(int(data.get("index", 0)), len(self._image_paths) - 1))
        if index:
            self._navigate(index)
        self._set_status(f"Resumed — {len(paths):,} image(s), back at #{index + 1}.")

    # ================================================================== #
    #  Queue management                                                    #
    # ================================================================== #

    @_safe_slot
    def _remove_from_queue(self, path: str):
        self._drop_from_queue([path])

    def _drop_from_queue(self, paths: List[str]):
        """Take images out of the queue in one pass, then land on a sane row."""
        wanted = dict.fromkeys(paths)
        # One set, not a fresh list copy per membership test — the duplicate
        # scanner can drop thousands of rows in one go.
        in_queue = set(self._image_paths)
        present = [p for p in wanted if p in in_queue]
        if not present:
            return
        if self._current_image_path() in wanted:
            self._flush_autosave()

        drop = set(present)
        first_idx = min((i for i, p in enumerate(self._image_paths) if p in drop),
                        default=0)
        for path in present:
            self._queue.remove_image(path)
        # One rebuild instead of a linear remove per path
        self._image_paths = [p for p in self._image_paths if p not in drop]

        if not self._image_paths:
            self._reset_empty_view()
            self._update_progress_readout()
            self._touch_session()
            self._set_status("Queue empty.")
            return

        new_idx = min(first_idx, len(self._image_paths) - 1)
        self._current_index = -1
        self._dirty = False
        self._navigate(new_idx)
        self._update_progress_readout()
        self._touch_session()

    def _reset_empty_view(self):
        """Blank every panel — the queue has nothing left to show."""
        self._current_index = -1
        self._dirty = False
        self._nav_timer.stop()
        self._viewer.clear()
        self._bbox_panel.clear()
        self._telemetry.load_values(None)
        self._undo.reset([])
        self._lbl_name.setText("No image loaded")
        self._lbl_dims.setText("")
        self._lbl_size.setText("")
        self._refresh_seg_view()

    @_safe_slot
    def _clear_queue(self):
        if not self._image_paths:
            return
        if QMessageBox.question(
            self, "Clear Queue", "Remove all images from the queue?",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._flush_autosave()
        self._dup_action = None
        self._image_paths.clear()
        self._queue.clear()
        self._reset_empty_view()
        self._viewer.release_memory()
        self._completions.clear()
        self._update_progress_readout()
        self._session.clear()
        self._set_status("Queue cleared.")

    # ================================================================== #
    #  Duplicate scan                                                      #
    # ================================================================== #

    @_safe_slot
    def _find_duplicates(self):
        """Open the (non-modal) duplicate scanner on the queue + the record."""
        if not self._image_paths and not self._seg_mgr.done_count():
            self._set_status("Nothing to scan — import some images first.")
            return

        if self._dup_dialog is None:
            self._dup_dialog = DuplicateScanDialog(
                self._image_paths,
                self._seg_mgr.signatures(),
                self,
                self._config.get("duplicate_sensitivity", DEFAULT_SENSITIVITY),
            )
            self._dup_dialog.remove_from_queue_requested.connect(self._dup_remove)
            self._dup_dialog.reject_requested.connect(self._dup_reject)
            self._dup_dialog.phashes_learned.connect(self._seg_mgr.remember_phashes)
            self._dup_dialog.open_requested.connect(self._show_image)
            self._dup_dialog.finished.connect(self._on_dup_dialog_closed)
        else:
            self._dup_dialog.update_queue_paths(self._image_paths)

        self._dup_dialog.show()
        self._dup_dialog.raise_()
        self._dup_dialog.activateWindow()
        self._dup_dialog.start_scan()
        self._set_status("Scanning for duplicate images…")

    @_safe_slot
    def _dup_remove(self, paths: List[str]):
        """Ticked copies leave the queue; not one file is touched."""
        wanted = [str(p) for p in paths]
        self._drop_from_queue(wanted)
        if self._dup_dialog:
            self._dup_dialog.mark_handled(wanted)
            self._dup_dialog.update_queue_paths(self._image_paths)
        self._set_status(f"Removed {len(wanted)} duplicate(s) from the queue.")

    @_safe_slot
    def _dup_reject(self, paths: List[str]):
        handled = self._reject_paths([str(p) for p in paths],
                                     reason="duplicate scan")
        if handled and self._dup_dialog:
            self._dup_dialog.mark_handled(handled)
            self._dup_dialog.update_queue_paths(self._image_paths)

    @_safe_slot
    def _on_dup_dialog_closed(self):
        if self._dup_dialog:
            self._config.set("duplicate_sensitivity",
                             self._dup_dialog.sensitivity_index())
            self._dup_dialog.deleteLater()
            self._dup_dialog = None

    @_safe_slot
    def _show_image(self, path: str):
        """Jump the queue to this image (double-click in the scan results)."""
        index = self._queue.index_of(path)
        if index < 0 and path in self._image_paths:
            index = self._image_paths.index(path)
        if index >= 0:
            self._navigate(index)
            self.raise_()
            self.activateWindow()

    # ================================================================== #
    #  Review grid                                                         #
    # ================================================================== #

    @_safe_slot
    def _open_review_grid(self):
        if not self._image_paths:
            self._set_status("Nothing to review — import some images first.")
            return
        self._flush_autosave()
        if self._grid_dialog is None:
            self._grid_dialog = ReviewGridDialog(
                self._image_paths, self._status_checker(),
                self._saved_box_for, self,
            )
            self._grid_dialog.open_requested.connect(self._show_image)
            self._grid_dialog.finished.connect(self._on_grid_closed)
        else:
            self._grid_dialog.reload(self._image_paths)
        self._grid_dialog.show()
        self._grid_dialog.raise_()
        self._grid_dialog.activateWindow()

    @_safe_slot
    def _on_grid_closed(self):
        if self._grid_dialog:
            self._grid_dialog.deleteLater()
            self._grid_dialog = None

    def _saved_box_for(self, path: str) -> Optional[dict]:
        """The stored bounding box for an image, or None."""
        if not self._label_index.has(path):
            return None
        data = self._label_mgr.load_label(path, self._config.export_format)
        if not data:
            return None
        box = data.get(BBOX_FIELD)
        if isinstance(box, dict) and box.get("left") is not None:
            return box
        return None

    # ================================================================== #
    #  Delete (move to _rejected)                                          #
    # ================================================================== #

    @_safe_slot
    def _delete_current_image(self):
        path = self._current_image_path()
        if not path:
            self._set_status("No image selected.")
            return
        self._reject_paths([path], reason="not useful")

    @_safe_slot
    def _delete_image(self, path: str):
        """Right-click → Delete Image on a queue row."""
        if path:
            self._reject_paths([path], reason="not useful")

    def _rejected_root(self, sample_path: str) -> Path:
        return rejected_dir(
            sample_path,
            self._config.get("rejected_directory", ""),
            self._config.get("segregation_output_directory", ""),
        )

    def _reject_paths(self, paths: List[str], reason: str = "") -> List[str]:
        """
        Move images out of the working set: file to _rejected, dataset copies
        deleted, record entry cleared, row taken out of the queue.
        """
        wanted = [p for p in dict.fromkeys(paths) if p]
        if not wanted:
            return []

        target = self._rejected_root(wanted[0])
        listing = "\n".join(f"    • {Path(p).name}" for p in wanted[:8])
        if len(wanted) > 8:
            listing += f"\n    … and {len(wanted) - 8} more"
        in_dataset = [p for p in wanted if self._seg_mgr.find_record(p)]

        message = (
            f"Move {len(wanted)} image(s) out of the working set?\n\n"
            f"{listing}\n\n"
            f"Moved to:\n    {target}\n\n"
        )
        if in_dataset:
            message += (f"{len(in_dataset)} of them are already sorted — their "
                        f"copies in the dataset are deleted and their rows in "
                        f"processed_images.csv are cleared.\n\n")
        message += ("Nothing is destroyed: to undo, move the file back from the "
                    "_rejected folder (rejected_log.csv records where each one "
                    "came from).")

        if QMessageBox.question(
            self, "Delete Image" if len(wanted) == 1 else "Delete Images",
            message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            self._set_status("Nothing was moved.")
            return []

        # Flush first: an in-flight autosave must not re-create a label file
        # for an image that is on its way out.
        self._flush_autosave()

        labelled = [p for p in wanted if self._label_index.has(p)]
        drop_labels = bool(labelled) and QMessageBox.question(
            self, "Label Files",
            f"{len(labelled)} of these image(s) have a label file in\n"
            f"    {self._label_mgr.output_directory}\n\n"
            f"Delete those label files too?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.Yes

        override = self._config.get("rejected_directory", "")
        dataset  = self._config.get("segregation_output_directory", "")
        handled: List[str] = []
        failures: List[str] = []

        for path in wanted:
            if self._seg_mgr.find_record(path):
                ok, msg = self._seg_mgr.unassign(path)
                if not ok:
                    failures.append(msg)
                    continue                    # leave the image alone entirely
            if drop_labels:
                self._label_mgr.delete_label(path)
                self._label_index.unmark(path)
            self._review_flags.set_flagged(path, False)
            self._viewer.invalidate_cache(path)

            if Path(path).is_file():
                ok, msg = move_to_rejected(path, override, dataset, reason)
                if not ok:
                    failures.append(msg)
                    continue
            handled.append(path)

        self._drop_from_queue(handled)
        self._refresh_seg_view()

        if failures:
            QMessageBox.warning(
                self, "Some Images Were Not Moved",
                "\n".join(failures[:10])
                + ("" if len(failures) <= 10 else f"\n… and {len(failures) - 10} more"),
            )
        self._set_status(
            f"Moved {len(handled)} image(s) to {target}"
            + (f" — {len(failures)} failed." if failures else ".")
        )
        return handled

    # ================================================================== #
    #  Navigation                                                          #
    # ================================================================== #

    @_safe_slot
    def _on_queue_selection(self, path: str, index: int):
        if index != self._current_index:
            self._navigate(index)

    @_safe_slot
    def _prev(self):
        self._step(-1)

    @_safe_slot
    def _next(self):
        self._step(1)

    def _step(self, delta: int):
        """Move one image, skipping rows hidden by the queue filter."""
        index = self._current_index + delta
        while 0 <= index < len(self._image_paths):
            if self._queue.is_visible(index):
                self._navigate(index)
                return
            index += delta
        if self._image_paths:
            self._set_status(
                "Start of queue." if delta < 0 else
                "End of queue — nothing further matches the queue filter."
            )

    def _navigate(self, index: int):
        """
        Switch to an image. The panels update immediately; the decode is
        coalesced so holding Next does not queue one decode per image.
        """
        if not self._image_paths or not (0 <= index < len(self._image_paths)):
            return

        if self._current_index >= 0 and self._current_index != index:
            self._flush_autosave()

        self._current_index = index
        path = self._image_paths[index]

        self._lbl_name.setText(Path(path).name)
        self._queue.set_current_index(index)
        self._act_flag.setChecked(self._review_flags.is_flagged(path))
        self._set_status(
            f"Image {index + 1} / {len(self._image_paths)}  —  {Path(path).name}"
            + (" — already sorted" if self._task == TASK_SEGREGATION
               and self._seg_mgr.is_done(path) else "")
        )
        self._touch_session()

        # A cached image can be shown at once; anything else waits for the
        # navigation to settle so a held-down key decodes once, not 40 times.
        if self._viewer.is_cached(path):
            self._nav_timer.stop()
            self._settle_navigation()
        else:
            self._nav_timer.start(NAV_SETTLE_MS)

    def _settle_navigation(self):
        path = self._current_image_path()
        if not path:
            return

        self._dirty = False
        if self._task == TASK_SEGREGATION:
            self._viewer.load_image(path, [])
            self._refresh_seg_view()
        else:
            existing = self._label_mgr.load_label(path, self._config.export_format)
            self._telemetry.load_values(existing)
            self._viewer.load_image(path, self._boxes_from_label(existing))

        self._lbl_size.setText(self._file_size_text(path))
        self._viewer.prefetch(self._neighbours())

    def _boxes_from_label(self, existing: Optional[dict]) -> List[dict]:
        """Rebuild the box list from a saved label file."""
        if not existing:
            return []
        bbox = existing.get(BBOX_FIELD) or {}
        if not isinstance(bbox, dict) or bbox.get("left") is None:
            return []
        return [{
            "id":     "saved",
            "label":  bbox.get("label", "") or "",
            "left":   bbox["left"],
            "top":    bbox["top"],
            "right":  bbox["right"],
            "bottom": bbox["bottom"],
        }]

    def _neighbours(self) -> List[str]:
        lo = max(0, self._current_index - PREFETCH_BEHIND)
        hi = min(len(self._image_paths), self._current_index + PREFETCH_AHEAD + 1)
        return [self._image_paths[i] for i in range(lo, hi)
                if i != self._current_index]

    @staticmethod
    def _file_size_text(path: str) -> str:
        try:
            kb = os.path.getsize(path) / 1024
        except OSError:
            return ""
        return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"

    def _reload_current_annotation(self):
        """Re-read the label file for the current image (fields changed)."""
        path = self._current_image_path()
        if not path or self._task != TASK_ANNOTATION:
            return
        existing = self._label_mgr.load_label(path, self._config.export_format)
        self._telemetry.load_values(existing)

    # ================================================================== #
    #  Viewer events                                                       #
    # ================================================================== #

    @_safe_slot
    def _on_image_loaded(self, path: str, size: QSize):
        if size and size.isValid():
            self._lbl_dims.setText(f"{size.width()} x {size.height()} px")
        else:
            self._lbl_dims.setText("")
        if self._task == TASK_ANNOTATION:
            self._refresh_bbox_panel()
            self._undo.reset(self._viewer.get_boxes_data())

    @_safe_slot
    def _on_image_failed(self, path: str):
        self._lbl_dims.setText("")
        self._bbox_panel.clear()
        self._set_status(f"Failed to load: {Path(path).name}")

    def _on_boxes_changed(self):
        """A committed box change — panel, undo, dirty flag, autosave."""
        self._refresh_bbox_panel()
        if self._applying_undo:
            return
        self._undo.push(self._viewer.get_boxes_data())
        self._mark_dirty()

    def _on_telemetry_edited(self):
        self._mark_dirty()

    def _on_selection_changed(self):
        box_id = self._viewer.get_selected_box_id()
        if box_id:
            self._bbox_panel.select_by_id(box_id)

    @_safe_slot
    def _on_panel_box_select(self, box_id: str):
        self._viewer.select_box_by_id(box_id)

    @_safe_slot
    def _on_panel_box_delete(self, box_id: str):
        self._viewer.delete_box_by_id(box_id)
        self._set_status("Box deleted  (Ctrl+Z to undo)")

    @_safe_slot
    def _clear_all_boxes(self):
        if not self._current_image_path() or self._viewer.box_count == 0:
            return
        self._viewer.clear_boxes()
        self._set_status("Box cleared  (Ctrl+Z to undo)")

    @_safe_slot
    def _delete_selected_box(self):
        if self._viewer.delete_selected_boxes():
            self._set_status("Box deleted  (Ctrl+Z to undo)")

    # ================================================================== #
    #  Undo / redo                                                         #
    # ================================================================== #

    @_safe_slot
    def _undo_action(self):
        if self._task != TASK_ANNOTATION:
            return
        state = self._undo.undo()
        if state is None:
            self._set_status("Nothing to undo.")
            return
        self._apply_undo_state(state)
        self._set_status("Undone.  (Ctrl+Y redoes)")

    @_safe_slot
    def _redo_action(self):
        if self._task != TASK_ANNOTATION:
            return
        state = self._undo.redo()
        if state is None:
            self._set_status("Nothing to redo.")
            return
        self._apply_undo_state(state)
        self._set_status("Redone.")

    def _apply_undo_state(self, state: List[dict]):
        self._applying_undo = True
        try:
            self._viewer.set_boxes(state)
        finally:
            self._applying_undo = False
        self._refresh_bbox_panel()
        self._mark_dirty()

    # ================================================================== #
    #  Flag for review                                                     #
    # ================================================================== #

    @_safe_slot
    def _toggle_flag(self):
        path = self._current_image_path()
        if not path:
            self._set_status("No image selected.")
            return
        self._toggle_flag_for(path)

    @_safe_slot
    def _toggle_flag_for(self, path: str):
        if self._task != TASK_ANNOTATION:
            self._set_status("Flagging is part of the Annotation task  (Ctrl+1).")
            return
        if not self._config.get("output_directory", ""):
            if not self._ensure_output_dir():
                return
        flagged = self._review_flags.toggle(path)
        if flagged:
            self._queue.set_flagged(path, True)
        else:
            self._queue.set_state(
                path, DONE if self._label_index.has(path) else PENDING)
        if path == self._current_image_path():
            self._act_flag.setChecked(flagged)
        self._update_progress_readout()
        self._set_status(
            f"Flagged for review: {Path(path).name}" if flagged
            else f"Flag cleared: {Path(path).name}"
        )

    # ================================================================== #
    #  Manual save (Ctrl+S / Save button)                                  #
    # ================================================================== #

    @_safe_slot
    def _manual_save(self):
        if self._task == TASK_SEGREGATION:
            self._seg_assign()
            return
        if self._current_index < 0 or not self._image_paths:
            self._set_status("No image selected.")
            return
        if not self._ensure_output_dir():
            return
        self._autosave_timer.stop()
        path = self._image_paths[self._current_index]
        self._write_label_file(path, silent=False)

    # ================================================================== #
    #  Export                                                              #
    # ================================================================== #

    @_safe_slot
    def _export(self):
        if self._current_index < 0 or not self._image_paths:
            return
        path    = self._image_paths[self._current_index]
        fmt     = self._config.export_format
        default = f"{Path(path).stem}.{fmt}"
        ffilter = "CSV Files (*.csv)" if fmt == "csv" else "JSON Files (*.json)"
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export Label File",
            str(Path(self._config.output_directory) / default), ffilter,
        )
        if not dest:
            return
        values = {
            "image_name": Path(path).name,
            **self._telemetry.get_values(),
            BBOX_FIELD: self._viewer.get_single_box(),
        }
        if self._label_mgr.save_label_to_path(dest, values, fmt):
            self._set_status(f"Exported: {dest}")
        else:
            QMessageBox.critical(self, "Export Error", "Failed to export.")

    # ================================================================== #
    #  Misc actions                                                        #
    # ================================================================== #

    @_safe_slot
    def _zoom_in(self):  self._viewer.zoom_in()

    @_safe_slot
    def _zoom_out(self): self._viewer.zoom_out()

    @_safe_slot
    def _zoom_reset(self): self._viewer.zoom_reset()

    @_safe_slot
    def _fit(self):      self._viewer.fit_in_view()

    @_safe_slot
    def _refresh(self):
        path = self._current_image_path()
        if not path:
            return
        self._viewer.invalidate_cache(path)
        self._queue.thumbnails.forget(path)
        self._settle_navigation()
        self._set_status("Image reloaded from disk.")

    @_safe_slot
    def _set_output_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self._config.output_directory,
        )
        if folder:
            self._flush_autosave()
            self._set_output_directory(folder)
            self._set_status(f"Output directory: {folder}")

    def _on_zoom(self, zoom: float):
        self._lbl_zoom.setText(f"{int(zoom * 100)}%")

    def _set_status(self, msg: str):
        self._sb_msg.setText(msg)

    def _current_image_path(self) -> str:
        if 0 <= self._current_index < len(self._image_paths):
            return self._image_paths[self._current_index]
        return ""

    def _refresh_bbox_panel(self):
        self._bbox_panel.refresh(
            self._viewer.get_boxes_data(),
            self._viewer.label_colors,
        )

    # ================================================================== #
    #  Keyboard                                                            #
    # ================================================================== #

    def keyPressEvent(self, event):
        """Enter assigns in the segregation task (splits are in eventFilter)."""
        if self._task == TASK_SEGREGATION and event.key() in (
            Qt.Key_Return, Qt.Key_Enter
        ):
            self._seg_assign()
            event.accept()
            return
        super().keyPressEvent(event)

    # ================================================================== #
    #  Close                                                               #
    # ================================================================== #

    def closeEvent(self, event):
        if self._dup_dialog:
            self._dup_dialog.close()
        if self._grid_dialog:
            self._grid_dialog.close()
        if self._scanner is not None:
            self._scanner.cancel()
            self._scanner.wait(1000)

        self._nav_timer.stop()
        self._flush_autosave()
        self._session_timer.stop()
        self._save_session()

        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass

        # Stop worker threads before the C++ widgets go away
        self._queue.shutdown()
        self._viewer.shutdown()

        try:
            self._config.set("window_geometry", bytes(self.saveGeometry()).hex())
        except Exception:
            pass
        super().closeEvent(event)
