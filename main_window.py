"""
SnP Image Labeler - Main Window

Two tasks share one shell (queue + viewer + navigation); the toolbar and the
right-hand panel swap with the active task.

  Annotation task (default)
    - Single output file per image: {image_name}.json or {image_name}.csv
    - Contains: image_name + telemetry fields + currency_country_bounding_box
    - Autosave triggers: box drawn/modified/deleted, telemetry changed, navigate away
    - Output directory prompt if not yet set

  Segregation task
    - Copies each image into {dataset_root}/{Class}_data/{split}/
    - Multi-class with an independent train/test/val split per class
    - Progress tracked in the dataset root so a part-done folder resumes cleanly
"""

import inspect
import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from PyQt5.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (
    QAction, QComboBox, QFileDialog, QLabel, QMainWindow,
    QMessageBox, QShortcut, QSplitter, QStackedWidget, QStatusBar, QToolBar,
    QVBoxLayout, QHBoxLayout, QFrame, QTabWidget, QWidget,
)

from core.config_manager     import ConfigManager
from core.duplicate_finder   import DEFAULT_SENSITIVITY
from core.label_manager      import LabelManager
from core.rejection_manager  import REJECTED_DIRNAME, move_to_rejected, rejected_dir
from core.segregation_manager import SPLITS, SPLIT_LABELS, SegregationManager
from widgets.annotation_viewer import AnnotationViewer, ViewerMode
from widgets.class_editor    import ClassEditorDialog
from widgets.duplicate_dialog import (
    CANCEL as DUP_CANCEL, SKIP as DUP_SKIP, UPDATE as DUP_UPDATE,
    ask_duplicate_action,
)
from widgets.duplicate_scan_dialog import DuplicateScanDialog
from widgets.image_queue     import ImageQueuePanel
from widgets.telemetry_panel  import TelemetryPanel
from widgets.bbox_panel      import BBoxPanel
from widgets.field_editor    import FieldEditorDialog
from widgets.segregation_panel import SegregationPanel

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
IMG_FILTER     = "Images (*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.tif *.webp)"

TASK_ANNOTATION  = "annotation"
TASK_SEGREGATION = "segregation"


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

        self._task = self._config.active_task
        self._image_paths:   List[str] = []
        self._current_index: int       = -1

        # "Apply to all remaining" answer from the already-processed dialog.
        # Session-scoped: cleared on import, queue clear and dataset change.
        self._dup_action: Optional[str] = None

        # Non-modal duplicate scanner, kept alive while the user works the queue
        self._dup_dialog: Optional[DuplicateScanDialog] = None

        # Debounce autosave — wait 400 ms after last change before writing
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._do_autosave)

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
        self._queue.clear_all_requested.connect(self._clear_queue)
        splitter.addWidget(self._queue)

        # ── Centre: Viewer + info bar ─────────────────────────────
        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        self._viewer = AnnotationViewer()
        self._viewer.zoom_changed.connect(self._on_zoom)
        self._viewer.box_added.connect(self._on_box_changed)
        self._viewer.box_modified.connect(self._on_box_changed)
        self._viewer.box_deleted.connect(self._on_box_changed)
        self._viewer.selection_changed.connect(self._on_selection_changed)
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
        # Autosave trigger: any telemetry field change
        self._telemetry.values_changed.connect(self._schedule_autosave)
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

        # Actions that belong to only one task (shown/hidden on task switch)
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
        self._act_delete = act(
            "Delete Image",
            "Move this image out of the working set into _rejected  (Shift+Del)",
            self._delete_current_image)
        delete_button = tb.widgetForAction(self._act_delete)
        if delete_button is not None:
            delete_button.setObjectName("danger_tool_btn")
        tb.addSeparator()
        act("Prev",    "Previous image  (Left)",             self._prev)
        act("Next",    "Next image  (Right)",                self._next)
        tb.addSeparator()
        act("Zoom+",   "Zoom In  (Ctrl++)",                  self._zoom_in)
        act("Zoom-",   "Zoom Out  (Ctrl+-)",                 self._zoom_out)
        act("Fit",     "Fit to window  (F)",                 self._fit)
        act("Refresh", "Refresh  (R)",                       self._refresh)
        tb.addSeparator()

        # ── Annotation-only controls ──────────────────────────────
        self._annot_only.append(tb.addWidget(QLabel("  Format: ")))
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(["CSV", "JSON"])
        self._fmt_combo.setCurrentText(self._config.export_format.upper())
        self._fmt_combo.setFixedWidth(78)
        self._fmt_combo.currentTextChanged.connect(
            lambda t: setattr(self._config, "export_format", t.lower())
        )
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
        self._act_annot.setToolTip("Annotate mode — draw bounding box  (A)")
        self._act_annot.setCheckable(True)
        self._act_annot.setChecked(False)
        self._act_annot.triggered.connect(self._mode_annotate)
        tb.addAction(self._act_annot)
        self._annot_only.append(self._act_annot)

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
        self._sb_stats = QLabel("")
        self._sb_stats.setObjectName("outdir_label")
        self._sb_stats.setVisible(False)
        sb.addPermanentWidget(self._sb_stats)

        self._sb_outdir = QLabel(f"Output: {self._config.output_directory}")
        self._sb_outdir.setObjectName("outdir_label")
        sb.addPermanentWidget(self._sb_outdir)

    def _bind_shortcuts(self):
        QShortcut(QKeySequence("Left"),   self, self._prev)
        QShortcut(QKeySequence("Right"),  self, self._next)
        QShortcut(QKeySequence("Ctrl+S"), self, self._manual_save)
        QShortcut(QKeySequence("Ctrl+O"), self, self._import)
        QShortcut(QKeySequence("Ctrl+D"), self, self._find_duplicates)
        QShortcut(QKeySequence("Shift+Del"), self, self._delete_current_image)
        QShortcut(QKeySequence("Ctrl++"), self, self._zoom_in)
        QShortcut(QKeySequence("Ctrl+="), self, self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, self._zoom_out)
        QShortcut(QKeySequence("F"),      self, self._fit)
        QShortcut(QKeySequence("R"),      self, self._refresh)
        QShortcut(QKeySequence("N"),      self, self._mode_navigate)
        QShortcut(QKeySequence("A"),      self, self._mode_annotate)

        # Task switch
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._set_task(TASK_ANNOTATION))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._set_task(TASK_SEGREGATION))

        # Segregation keys (1/2/3 = split, Enter = assign) are handled in
        # keyPressEvent, not as QShortcuts, so they never steal keystrokes
        # from a focused spin box or text field.

    def _restore_geometry(self):
        geom = self._config.get("window_geometry")
        if geom:
            try:
                self.restoreGeometry(QByteArray.fromHex(geom.encode()))
                return
            except Exception:
                pass
        self.resize(1300, 820)

    # ================================================================== #
    #  Output directory — ensure set before saving                        #
    # ================================================================== #

    def _ensure_output_dir(self) -> bool:
        """
        Returns True if output directory is confirmed set.
        If not explicitly set, prompts user to choose one.
        """
        # Config stores "" when not set; output_directory property falls back to home
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

        self._config.output_directory = folder
        self._label_mgr.set_output_directory(folder)
        self._sb_outdir.setText(f"Output: {folder}")
        self._set_status(f"Output directory set: {folder}")
        return True

    # ================================================================== #
    #  Task switching                                                      #
    # ================================================================== #

    @_safe_slot
    def _set_task(self, task: str):
        if task == self._task:
            self._sync_task_actions()      # keep toggles consistent on re-click
            return
        if self._task == TASK_ANNOTATION:  # flush pending label write first
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
        self._right_stack.setCurrentIndex(1 if seg else 0)

        if seg:
            # Segregation never draws boxes — lock the viewer to pan/zoom
            self._viewer.set_mode(ViewerMode.NAVIGATE)
            self._act_nav.setChecked(True)
            self._act_annot.setChecked(False)
            self._queue.set_status_labels("Sorted")
            self._sb_outdir.setText(
                f"Dataset: {self._config.segregation_output_directory}")
        else:
            self._queue.set_status_labels("Labeled")
            self._sb_outdir.setText(f"Output: {self._config.output_directory}")

        self._sb_stats.setVisible(seg)
        self._queue.refresh_status(self._status_checker())

        # Reload the current image under the new task. Blanking the index first
        # skips _navigate's autosave flush, so merely switching tasks never
        # writes a label file.
        if 0 <= self._current_index < len(self._image_paths):
            index, self._current_index = self._current_index, -1
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
        """The 'is this image done?' test for the active task."""
        return (self._seg_mgr.is_done if self._task == TASK_SEGREGATION
                else self._label_mgr.has_label)

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
        self._seg_mgr.set_root(folder)                 # loads that root's record
        healed = self._seg_mgr.verify()                # forget copies deleted by hand
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

        # Already in processed_images.csv? Ask before touching it again.
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
        if self._config.segregation_last_split != last_split:   # avoid a disk write per image
            self._config.segregation_last_split = last_split
        self._seg_panel.set_default_split(last_split)
        self._queue.mark_labeled(path, True)

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
        done  = sum(1 for p in self._image_paths if self._seg_mgr.is_done(p))
        self._sb_stats.setText(f"Segregated: {done} / {total}")

    # ================================================================== #
    #  Autosave                                                            #
    # ================================================================== #

    def _schedule_autosave(self, *args):
        """Debounced: coalesces rapid changes into a single save after 400 ms."""
        if self._task != TASK_ANNOTATION:
            return
        if self._current_index >= 0 and self._image_paths:
            self._autosave_timer.start(400)

    def _do_autosave(self, prompt: bool = True):
        """
        Write the label file. Called by the debounce timer (prompt=True) and by
        _flush_autosave (prompt=False) when leaving an image, switching task or
        closing — a flush must never pop a dialog.
        """
        if self._task != TASK_ANNOTATION:
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
            "currency_country_bounding_box": bbox,
        }

        ok = self._label_mgr.save_label(path, values, fmt)
        if ok:
            self._queue.mark_labeled(path, True)
            if not silent:
                stem = Path(path).stem
                self._set_status(
                    f"Saved: {stem}.{fmt}  →  {self._config.output_directory}"
                )
        else:
            if not silent:
                QMessageBox.critical(self, "Save Error", "Failed to write label file.")
        return ok

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
            self._set_status(f"Fields updated — {len(fields)} field(s).")

    # ================================================================== #
    #  Mode                                                                #
    # ================================================================== #

    @_safe_slot
    def _mode_navigate(self):
        self._viewer.set_mode(ViewerMode.NAVIGATE)
        self._act_nav.setChecked(True)
        self._act_annot.setChecked(False)
        self._set_status("Mode: Navigate — Pan and zoom. Press A to annotate.")

    @_safe_slot
    def _mode_annotate(self):
        if self._task != TASK_ANNOTATION:
            self._set_status("Switch to the Annotation task to draw boxes  (Ctrl+1).")
            return
        self._viewer.set_mode(ViewerMode.ANNOTATE)
        self._act_nav.setChecked(False)
        self._act_annot.setChecked(True)
        self._set_status("Mode: Annotate — Drag to draw box. Double-click to relabel. Del to delete.")

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
        if folder:
            self._config.last_import_directory = folder
            paths = sorted(
                str(p) for p in Path(folder).iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
            )
            if not paths:
                nested = sorted(
                    str(p) for p in Path(folder).rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
                    and REJECTED_DIRNAME not in p.parts
                )
                if nested and QMessageBox.question(
                    self, "Include Sub-folders?",
                    f"No images directly in that folder, but {len(nested)} found in "
                    f"sub-folders. Import them?",
                    QMessageBox.Yes | QMessageBox.No,
                ) == QMessageBox.Yes:
                    paths = nested
            if paths:
                self._enqueue(paths)
            else:
                QMessageBox.information(self, "No Images",
                    "No supported images found in that folder.")

    def _enqueue(self, paths: List[str]):
        self._dup_action = None          # a new import always asks again
        existing = set(self._image_paths)
        new = [p for p in paths if p not in existing]
        if not new:
            self._set_status("All selected images already in queue.")
            return
        checker = self._status_checker()
        if not self._image_paths:
            self._image_paths = list(new)
            self._queue.load_images(new, checker)
            self._navigate(0)
        else:
            self._image_paths.extend(new)
            self._queue.add_images(new, checker)
        if self._task == TASK_SEGREGATION:
            self._refresh_seg_view()
        self._set_status(f"Added {len(new)} image(s). Total: {len(self._image_paths)}")

    # ================================================================== #
    #  Queue management                                                    #
    # ================================================================== #

    @_safe_slot
    def _remove_from_queue(self, path: str):
        self._drop_from_queue([path])

    def _drop_from_queue(self, paths: List[str]):
        """Take images out of the queue in one pass, then land on a sane row."""
        present = [p for p in dict.fromkeys(paths) if p in self._image_paths]
        if not present:
            return
        # Autosave the current image before it can be pulled out from under us
        if self._current_image_path() in present:
            self._flush_autosave()

        first_idx = min(self._image_paths.index(p) for p in present)
        for path in present:
            self._image_paths.remove(path)
            self._queue.remove_image(path)

        if not self._image_paths:
            self._reset_empty_view()
            self._set_status("Queue empty.")
            return

        new_idx = min(first_idx, len(self._image_paths) - 1)
        self._current_index = -1
        self._navigate(new_idx)

    def _reset_empty_view(self):
        """Blank every panel — the queue has nothing left to show."""
        self._current_index = -1
        self._viewer.clear_boxes()
        self._viewer.clear()
        self._bbox_panel.clear()
        self._telemetry.clear_values()
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
        if path in self._image_paths:
            self._navigate(self._image_paths.index(path))
            self.raise_()
            self.activateWindow()

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

        Returns the paths that were dealt with, so a caller (the duplicate
        scanner) can drop them from its own view.
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

        # Label files are the user's work — never bin them without asking
        labelled = [p for p in wanted if self._label_mgr.has_label(p)]
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

            if Path(path).is_file():
                ok, msg = move_to_rejected(path, override, dataset, reason)
                if not ok:
                    failures.append(msg)
                    continue
            handled.append(path)

        self._drop_from_queue(handled)
        self._queue.refresh_status(self._status_checker())
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
        """Move one image, skipping rows hidden by the queue's Pending filter."""
        index = self._current_index + delta
        while 0 <= index < len(self._image_paths):
            if self._queue.is_visible(index):
                self._navigate(index)
                return
            index += delta

    def _navigate(self, index: int):
        if not self._image_paths or not (0 <= index < len(self._image_paths)):
            return

        # Flush any pending autosave for current image before leaving
        if self._current_index >= 0:
            self._flush_autosave()

        self._current_index = index
        path = self._image_paths[index]

        if not self._viewer.load_image(path):
            self._set_status(f"Failed to load: {Path(path).name}")
            return

        # Image metadata
        try:
            from PyQt5.QtGui import QImageReader
            reader = QImageReader(path)
            sz = reader.size()
            self._lbl_name.setText(Path(path).name)
            self._lbl_dims.setText(f"{sz.width()} x {sz.height()} px" if sz.isValid() else "")
            kb = os.path.getsize(path) / 1024
            self._lbl_size.setText(f"{kb:.0f} KB" if kb < 1024 else f"{kb/1024:.1f} MB")
        except Exception:
            self._lbl_name.setText(Path(path).name)
            self._lbl_dims.setText("")
            self._lbl_size.setText("")

        if self._task == TASK_SEGREGATION:
            # Segregation shows the saved class/split for this image instead
            self._viewer.clear_boxes()
            self._refresh_seg_view()
            self._queue.set_current_index(index)
            done = " — already sorted" if self._seg_mgr.is_done(path) else ""
            self._set_status(
                f"Image {index + 1} / {len(self._image_paths)}  —  "
                f"{Path(path).name}{done}"
            )
            return

        # Load existing label file — restore telemetry AND bounding box
        existing = self._label_mgr.load_label(path, self._config.export_format)
        if existing:
            self._telemetry.set_values(existing)
            # Restore bounding box from label file
            bbox = existing.get("currency_country_bounding_box", {})
            if bbox and bbox.get("left") is not None:
                box_list = [{
                    "id":     "restored",
                    "label":  "",
                    "left":   bbox["left"],
                    "top":    bbox["top"],
                    "right":  bbox["right"],
                    "bottom": bbox["bottom"],
                }]
                self._viewer.load_boxes(box_list)
            else:
                self._viewer.clear_boxes()
        else:
            self._telemetry.clear_values()
            self._viewer.clear_boxes()

        self._refresh_bbox_panel()
        self._queue.set_current_index(index)
        self._set_status(
            f"Image {index + 1} / {len(self._image_paths)}  —  {Path(path).name}"
        )

    # ================================================================== #
    #  Box events                                                          #
    # ================================================================== #

    def _on_box_changed(self, *args):
        """Called on box added / modified / deleted — schedule autosave."""
        self._refresh_bbox_panel()
        self._right_tabs.setCurrentIndex(1)
        self._schedule_autosave()

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
        self._refresh_bbox_panel()
        self._schedule_autosave()
        self._set_status("Box deleted.")

    @_safe_slot
    def _clear_all_boxes(self):
        path = self._current_image_path()
        if not path:
            return
        if QMessageBox.question(
            self, "Clear Boxes", "Delete the bounding box for this image?",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._viewer.clear_boxes()
        self._refresh_bbox_panel()
        self._schedule_autosave()
        self._set_status("Box cleared.")

    @_safe_slot
    def _delete_selected_box(self):
        self._viewer.delete_selected_boxes()

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
        image_name = Path(path).name
        telemetry  = self._telemetry.get_values()
        bbox       = self._viewer.get_single_box()
        values     = {
            "image_name": image_name,
            **telemetry,
            "currency_country_bounding_box": bbox,
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
    def _fit(self):      self._viewer.fit_in_view()

    @_safe_slot
    def _refresh(self):
        if self._current_index >= 0 and self._image_paths:
            path = self._image_paths[self._current_index]
            self._viewer.invalidate_cache(path)
            self._viewer.load_image(path)
            self._set_status("Image refreshed.")

    @_safe_slot
    def _set_output_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self._config.output_directory,
        )
        if folder:
            self._config.output_directory = folder
            self._label_mgr.set_output_directory(folder)
            self._sb_outdir.setText(f"Output: {folder}")
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
            self._viewer._label_colors,
        )

    # ================================================================== #
    #  Keyboard                                                            #
    # ================================================================== #

    def keyPressEvent(self, event):
        """Segregation shortcuts: 1/2/3 pick the split, Enter assigns."""
        if self._task == TASK_SEGREGATION:
            split_keys = {
                Qt.Key_1: SPLITS[0], Qt.Key_2: SPLITS[1], Qt.Key_3: SPLITS[2],
            }
            key = event.key()
            if key in split_keys:
                self._seg_set_split(split_keys[key])
                event.accept()
                return
            if key in (Qt.Key_Return, Qt.Key_Enter):
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
        self._flush_autosave()
        try:
            self._config.set("window_geometry", bytes(self.saveGeometry()).hex())
        except Exception:
            pass
        super().closeEvent(event)
