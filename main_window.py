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
import json
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
    QVBoxLayout, QHBoxLayout, QFrame, QTabWidget, QWidget, QSizePolicy,
    QCheckBox, QActionGroup, QMenu,
)

from core import theme
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
from widgets.label_panel     import LabelPanel
from widgets.bbox_panel      import BBoxPanel
from widgets.segregation_panel import SegregationPanel

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
IMG_FILTER     = "Images (*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.tif *.webp)"

TASK_ANNOTATION  = "annotation"
TASK_SEGREGATION = "segregation"

BOXES_FIELD  = "boxes"                       # new: a list of labelled boxes
BBOX_FIELD   = "currency_country_bounding_box"   # legacy single-box key, read only
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
        self._create_actions()
        self._build_menubar()
        self._build_toolbar()
        self._build_statusbar()
        self._bind_shortcuts()
        self._load_labels()
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
        self._queue.setMinimumWidth(theme.px(200))
        self._queue.setMaximumWidth(theme.px(320))
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
        info_bar.setMinimumHeight(theme.px(30))
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
        self._lbl_zoom.setMinimumWidth(theme.px(55))
        self._lbl_zoom.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ib.addWidget(self._lbl_zoom)

        cv.addWidget(info_bar)
        splitter.addWidget(centre)

        # ── Right: task-dependent stack ───────────────────────────
        self._right_stack = QStackedWidget()
        self._right_stack.setObjectName("right_stack")
        self._right_stack.setMinimumWidth(theme.px(300))
        self._right_stack.setMaximumWidth(theme.px(420))

        # Page 0 — Annotation task (Telemetry + Annotations tabs)
        self._right_tabs = QTabWidget()
        self._right_tabs.setObjectName("right_tabs")

        self._labels_panel = LabelPanel()
        self._labels_panel.save_requested.connect(self._manual_save)
        self._labels_panel.manage_requested.connect(self._manage_labels)
        self._labels_panel.label_chosen.connect(self._on_label_chosen)
        self._right_tabs.addTab(self._labels_panel, "Labels")

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

    # ================================================================== #
    #  Header - menu bar + toolbar                                         #
    # ================================================================== #
    #
    # Every command lives on the menu bar. Menus are pop-ups: they never run
    # out of horizontal room, they grow with the font, and Qt scrolls them when
    # they are too tall - so nothing becomes unreachable at 200%. The toolbar
    # is a convenience layer carrying the actions used constantly, and it is
    # free to overflow because the menu bar is the guarantee.

    @staticmethod
    def _with_hint(text, hint):
        """
        "Import Images\tCtrl+O" - Qt right-aligns the part after the tab.

        The hint is displayed, not registered. The real bindings live in
        _bind_shortcuts and the eventFilter; registering them a second time
        here would fire some commands twice per press.
        """
        return (text + "\t" + hint) if hint else text

    def _make_action(self, text, tip, slot, hint="", checkable=False):
        action = QAction(self._with_hint(text, hint), self)
        action.setToolTip(tip)
        if checkable:
            action.setCheckable(True)
        if slot is not None:
            action.triggered.connect(slot)
        return action

    def _create_actions(self):
        """Build every command once; the menus and the toolbar share them."""
        self._annot_only: List[QAction] = []
        self._seg_only:   List[QAction] = []
        mk = self._make_action

        # ---- Task ----------------------------------------------------
        self._act_task_annot = mk("Annotation Task",
                                  "Draw labelled bounding boxes  (Ctrl+1)",
                                  lambda: self._set_task(TASK_ANNOTATION),
                                  "Ctrl+1", checkable=True)
        self._act_task_seg = mk("Segregation Task",
                                "Sort images into class folders  (Ctrl+2)",
                                lambda: self._set_task(TASK_SEGREGATION),
                                "Ctrl+2", checkable=True)

        # ---- File ----------------------------------------------------
        self._act_import  = mk("Import Images or Folder...",
                               "Import images or a whole folder  (Ctrl+O)",
                               self._import, "Ctrl+O")
        self._act_export  = mk("Export Label File...",
                               "Export the label file to a chosen location",
                               self._export)
        self._act_outdir  = mk("Set Output Directory...",
                               "Where label files are written",
                               self._set_output_dir)
        self._act_dsdir   = mk("Set Dataset Directory...",
                               "Dataset root where class folders are created",
                               self._set_dataset_dir)
        self._act_opends  = mk("Open Dataset Folder",
                               "Open the dataset root in the file browser",
                               self._open_dataset_dir)
        self._act_openrec = mk("Open Processed Record",
                               "Open processed_images.csv",
                               self._open_record_file)
        self._act_exit    = mk("Exit", "Close SnP Image Labeler", self.close)
        self._annot_only += [self._act_export, self._act_outdir]
        self._seg_only   += [self._act_dsdir, self._act_opends, self._act_openrec]

        # Export format as a radio group, replacing the toolbar combo.
        self._fmt_actions: Dict[str, QAction] = {}
        self._fmt_group = QActionGroup(self)
        self._fmt_group.setExclusive(True)
        for name in ("CSV", "JSON"):
            action = mk(name, "Write label files as " + name, None, checkable=True)
            action.setChecked(self._config.export_format.upper() == name)
            action.triggered.connect(lambda _c, n=name: self._on_format_changed(n))
            self._fmt_group.addAction(action)
            self._fmt_actions[name] = action

        # ---- Edit ----------------------------------------------------
        self._act_undo    = mk("Undo", "Undo the last box change  (Ctrl+Z)",
                               self._undo_action, "Ctrl+Z")
        self._act_redo    = mk("Redo", "Redo  (Ctrl+Y)", self._redo_action, "Ctrl+Y")
        self._act_delbox  = mk("Delete Selected Box",
                               "Delete the selected box  (Del)",
                               self._delete_selected_box, "Del")
        self._act_clearbx = mk("Clear All Boxes",
                               "Remove every box on this image",
                               self._clear_all_boxes)
        self._act_flag    = mk("Flag for Review",
                               "Flag this image for review  (Ctrl+F)",
                               self._toggle_flag, "Ctrl+F", checkable=True)
        self._act_delete  = mk("Delete Image",
                               "Move this image into _rejected  (Shift+Del)",
                               self._delete_current_image, "Shift+Del")
        self._annot_only += [self._act_undo, self._act_redo,
                             self._act_delbox, self._act_clearbx]

        # ---- View ----------------------------------------------------
        self._act_zoom_in  = mk("Zoom In", "Zoom in  (Ctrl++)",
                                self._zoom_in, "Ctrl++")
        self._act_zoom_out = mk("Zoom Out", "Zoom out  (Ctrl+-)",
                                self._zoom_out, "Ctrl+-")
        self._act_fit      = mk("Fit to Window", "Fit the image to the window  (F)",
                                self._fit, "F")
        self._act_zoom_lock = mk("Lock Zoom",
                                 "Keep this zoom and pan on the next image  (Ctrl+L)",
                                 self._toggle_zoom_lock, "Ctrl+L", checkable=True)
        self._act_refresh  = mk("Reload Image", "Re-read this image from disk  (R)",
                                self._refresh, "R")
        self._act_grid     = mk("Review Grid...",
                                "Contact sheet of the queue  (Ctrl+G)",
                                self._open_review_grid, "Ctrl+G")

        # ---- Tools ---------------------------------------------------
        self._act_dupes   = mk("Find Duplicates...",
                               "Find the same picture stored more than once  (Ctrl+D)",
                               self._find_duplicates, "Ctrl+D")
        self._act_labels  = mk("Manage Labels...",
                               "Add / rename / reorder the box labels",
                               self._manage_labels)
        self._act_classes = mk("Manage Classes...",
                               "Add / rename / reorder segregation classes",
                               self._manage_classes)
        self._act_assign  = mk("Assign to Classes",
                               "Copy this image into the selected class folders  (Enter)",
                               self._seg_assign, "Enter")
        self._annot_only.append(self._act_labels)
        self._seg_only   += [self._act_classes, self._act_assign]

        self._act_nav = mk("Navigate Mode", "Pan and zoom  (N)",
                           self._mode_navigate, "N", checkable=True)
        self._act_nav.setChecked(True)
        self._act_annot = mk("Annotate Mode", "Draw bounding boxes  (B)",
                             self._mode_annotate, "B", checkable=True)
        self._act_sticky = mk("Sticky Label",
                              "Reuse the last label instead of asking every time",
                              self._toggle_sticky_label, checkable=True)
        self._annot_only += [self._act_nav, self._act_annot, self._act_sticky]

        # ---- Appearance ----------------------------------------------
        self._act_dark = mk("Dark Theme",
                            "Switch between the light and dark theme  (Ctrl+Shift+D)",
                            self._on_dark_action, "Ctrl+Shift+D", checkable=True)
        self._act_dark.setChecked(self._config.ui_theme == theme.DARK)

        self._scale_actions: Dict[int, QAction] = {}
        self._scale_group = QActionGroup(self)
        self._scale_group.setExclusive(True)
        for pct in theme.SCALES:
            action = mk(str(pct) + "%", "Scale the interface to " + str(pct) + "%",
                        None, checkable=True)
            action.triggered.connect(lambda _c, value=pct: self._set_scale(value))
            self._scale_group.addAction(action)
            self._scale_actions[pct] = action

        self._act_shortcuts = mk("Keyboard Shortcuts...",
                                 "Every shortcut in one list", self._show_shortcuts)

    def _build_menubar(self):
        bar = self.menuBar()
        bar.setObjectName("main_menubar")
        bar.setNativeMenuBar(False)      # keep it in the window on every OS

        menu = bar.addMenu("&File")
        menu.addAction(self._act_import)
        menu.addSeparator()
        fmt = menu.addMenu("Label Format")
        for action in self._fmt_actions.values():
            fmt.addAction(action)
        menu.addAction(self._act_export)
        menu.addSeparator()
        menu.addAction(self._act_outdir)
        menu.addAction(self._act_dsdir)
        menu.addAction(self._act_opends)
        menu.addAction(self._act_openrec)
        menu.addSeparator()
        menu.addAction(self._act_exit)

        menu = bar.addMenu("&Edit")
        menu.addAction(self._act_undo)
        menu.addAction(self._act_redo)
        menu.addSeparator()
        menu.addAction(self._act_delbox)
        menu.addAction(self._act_clearbx)
        menu.addSeparator()
        menu.addAction(self._act_flag)
        menu.addAction(self._act_delete)

        menu = bar.addMenu("&View")
        menu.addAction(self._act_zoom_in)
        menu.addAction(self._act_zoom_out)
        menu.addAction(self._act_fit)
        menu.addAction(self._act_zoom_lock)
        menu.addSeparator()
        menu.addAction(self._act_refresh)
        menu.addAction(self._act_grid)
        menu.addSeparator()
        menu.addAction(self._act_dark)
        scale = menu.addMenu("Interface Scale")
        for action in self._scale_actions.values():
            scale.addAction(action)

        menu = bar.addMenu("&Task")
        menu.addAction(self._act_task_annot)
        menu.addAction(self._act_task_seg)

        menu = bar.addMenu("T&ools")
        menu.addAction(self._act_dupes)
        menu.addSeparator()
        menu.addAction(self._act_labels)
        menu.addAction(self._act_classes)
        menu.addSeparator()
        menu.addAction(self._act_nav)
        menu.addAction(self._act_annot)
        menu.addAction(self._act_sticky)
        menu.addSeparator()
        menu.addAction(self._act_assign)

        menu = bar.addMenu("&Help")
        menu.addAction(self._act_shortcuts)

    def _build_toolbar(self):
        """
        The frequent actions only.

        This set still fits across the window at 200%; everything else is one
        menu click away, which is why the toolbar no longer has to carry all
        27 commands and silently drop two thirds of them.
        """
        tb = QToolBar("Toolbar")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        self._toolbar = tb
        self._tb_proxies: List[QAction] = []

        def proxy(source, label, danger=False):
            """
            A toolbar entry mirroring a menu action.

            The toolbar wants "Import"; the menu wants "Import Images or
            Folder...  Ctrl+O". A proxy keeps both readable while the menu
            action stays the single source of truth for checked / visible /
            enabled state, so switching task still hides the right buttons.
            """
            mirror = QAction(label, self)
            mirror.setToolTip(source.toolTip())
            mirror.setCheckable(source.isCheckable())
            mirror.setChecked(source.isChecked())
            mirror.setVisible(source.isVisible())
            mirror.setEnabled(source.isEnabled())
            mirror.triggered.connect(source.trigger)
            if source.isCheckable():
                source.toggled.connect(mirror.setChecked)
            source.changed.connect(
                lambda src=source, dst=mirror: (dst.setVisible(src.isVisible()),
                                                dst.setEnabled(src.isEnabled())))
            tb.addAction(mirror)
            if danger:
                button = tb.widgetForAction(mirror)
                if button is not None:
                    button.setObjectName("danger_tool_btn")
            self._tb_proxies.append(mirror)
            return mirror

        tb.addWidget(QLabel(" Task: "))
        proxy(self._act_task_annot, "Annotation")
        proxy(self._act_task_seg,   "Segregation")
        tb.addSeparator()

        proxy(self._act_import, "Import")
        proxy(self._act_dupes,  "Duplicates")
        tb.addSeparator()

        proxy(self._act_nav,    "Navigate")
        proxy(self._act_annot,  "Annotate")
        proxy(self._act_assign, "Assign")
        tb.addSeparator()

        proxy(self._act_zoom_in,  "Zoom+")
        proxy(self._act_zoom_out, "Zoom-")
        proxy(self._act_fit,      "Fit")
        tb.addSeparator()

        proxy(self._act_flag,   "Flag")
        proxy(self._act_delete, "Delete", danger=True)

    # ================================================================== #
    #  Appearance — theme + UI scale                                       #
    # ================================================================== #

    def _apply_appearance(self):
        """
        Re-theme and re-scale the running app.

        The stylesheet and the app font come from core.theme; the widgets that
        paint themselves (bounding boxes, thumbnails, the review grid) cannot be
        reached by a stylesheet, so they are handed the new palette explicitly.
        """
        app = QApplication.instance()
        if app is None:
            return
        mode, scale = self._config.ui_theme, self._config.ui_scale
        theme.apply(app, mode, scale)

        # A widget whose C++ side has already gone raises RuntimeError on any
        # access, including getattr. Re-theming must not be the thing that
        # takes the window down, so each one is attempted independently.
        for widget in (self._viewer, self._queue, self._seg_panel,
                       self._bbox_panel, self._labels_panel,
                       self._grid_dialog, self._dup_dialog):
            if widget is None:
                continue
            try:
                setter = getattr(widget, "apply_theme", None)
                if callable(setter):
                    setter(mode, scale)
            except RuntimeError:
                continue                  # dialog already closed and deleted

        # Panel widths are pixel numbers, so they have to be recomputed or the
        # panels keep their 100% size and clip their own (now larger) contents.
        self._queue.setMinimumWidth(theme.px(200))
        self._queue.setMaximumWidth(theme.px(320))
        self._right_stack.setMinimumWidth(theme.px(300))
        self._right_stack.setMaximumWidth(theme.px(420))
        self._sync_scale_combo()
        self._sync_appearance_menu()

        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _sync_appearance_menu(self):
        """Mirror the live theme and scale onto the View menu."""
        dark = getattr(self, "_act_dark", None)
        if dark is not None:
            dark.blockSignals(True)
            dark.setChecked(self._config.ui_theme == theme.DARK)
            dark.blockSignals(False)
        pct = int(round(self._config.ui_scale * 100))
        for value, action in getattr(self, "_scale_actions", {}).items():
            action.blockSignals(True)
            action.setChecked(value == pct)
            action.blockSignals(False)

    def _sync_scale_combo(self):
        """Keep the status-bar combo showing the scale actually in force."""
        combo = getattr(self, "_scale_combo", None)
        if combo is None:
            return
        pct = int(round(self._config.ui_scale * 100))
        at = combo.findData(pct)
        if at < 0:
            combo.addItem(f"{pct}%", pct)
            at = combo.findData(pct)
        if at != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(at)
            combo.blockSignals(False)

    @_safe_slot
    def _on_dark_action(self, *_args):
        """View > Dark Theme."""
        self._chk_dark.blockSignals(True)
        self._chk_dark.setChecked(self._act_dark.isChecked())
        self._chk_dark.blockSignals(False)
        self._toggle_theme()

    @_safe_slot
    def _set_scale(self, pct):
        """View > Interface Scale > N%."""
        self._config.ui_scale = float(pct) / 100.0
        self._apply_appearance()
        self._set_status("Interface scale: " + str(pct) + "%")

    @_safe_slot
    def _show_shortcuts(self):
        """Help > Keyboard Shortcuts - the menus show the common ones too."""
        rows = [
            ("D / Right", "Next image"),
            ("A / Left", "Previous image"),
            ("Ctrl+O", "Import images or a folder"),
            ("Ctrl+S", "Save label (Assign in Segregation)"),
            ("Ctrl+1 / Ctrl+2", "Annotation / Segregation task"),
            ("N / B", "Navigate / Annotate mode"),
            ("1 - 9", "Quick label (Train/Test/Val in Segregation)"),
            ("Del", "Delete the selected box"),
            ("Alt+Arrows", "Nudge the selected box (Shift for 10 px)"),
            ("Space / middle-drag", "Pan"),
            ("Ctrl+Z / Ctrl+Y", "Undo / Redo"),
            ("Ctrl+F", "Flag for review"),
            ("Ctrl+G", "Review grid"),
            ("Ctrl+D", "Find duplicates"),
            ("Ctrl+L", "Lock zoom"),
            ("F", "Fit to window"),
            ("R", "Reload the image"),
            ("Shift+Del", "Delete image (move to _rejected)"),
            ("Enter", "Assign and next (Segregation)"),
            ("Ctrl+Shift+D", "Toggle the dark theme"),
            ("Ctrl+Shift+ +/-", "Scale the interface up / down"),
        ]
        width = max(len(key) for key, _ in rows)
        body = "\n".join(key.ljust(width) + "   " + what for key, what in rows)
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard Shortcuts")
        box.setText("Keyboard Shortcuts")
        box.setInformativeText("<pre>" + body + "</pre>")
        box.exec_()

    @_safe_slot
    def _toggle_theme(self, *_args):
        mode = theme.DARK if self._chk_dark.isChecked() else theme.LIGHT
        self._config.ui_theme = mode
        self._apply_appearance()
        self._set_status(f"{'Dark' if mode == theme.DARK else 'Light'} theme.")

    @_safe_slot
    def _on_scale_changed(self, _index: int):
        pct = self._scale_combo.currentData()
        if not pct:
            return
        self._config.ui_scale = float(pct) / 100.0
        self._apply_appearance()
        self._set_status(f"Interface scale: {pct}%")

    @_safe_slot
    def _nudge_scale(self, step: int):
        """Ctrl+Shift+Plus / Minus — step through the preset scales."""
        current = int(round(self._config.ui_scale * 100))
        presets = sorted(set(theme.SCALES + [current]))
        index = max(0, min(len(presets) - 1, presets.index(current) + step))
        pct = presets[index]
        if pct == current:
            return
        at = self._scale_combo.findData(pct)
        if at >= 0:
            self._scale_combo.setCurrentIndex(at)      # fires _on_scale_changed
        else:
            self._config.ui_scale = pct / 100.0
            self._apply_appearance()
            self._set_status(f"Interface scale: {pct}%")

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_msg = QLabel("Ready — Import images to get started.")
        # Status text must give way, not shove the controls to its right off
        # the end of the bar when the UI is scaled up.
        self._sb_msg.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._sb_msg.setMinimumWidth(theme.px(60))
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

        # Appearance lives in the status bar rather than the toolbar: the
        # toolbar already overflows at 100%, and these two are exactly the
        # controls you need to reach when the UI is scaled awkwardly.
        self._chk_dark = QCheckBox("Dark")
        self._chk_dark.setObjectName("dark_toggle")
        self._chk_dark.setToolTip("Switch between the light and dark theme  (Ctrl+Shift+D)")
        self._chk_dark.setChecked(self._config.ui_theme == theme.DARK)
        self._chk_dark.toggled.connect(self._toggle_theme)
        self._chk_dark.toggled.connect(
            lambda on: self._act_dark.setChecked(on)
            if getattr(self, "_act_dark", None) is not None else None)
        sb.addPermanentWidget(self._chk_dark)

        self._scale_combo = QComboBox()
        self._scale_combo.setObjectName("scale_combo")
        self._scale_combo.setToolTip(
            "Scale the whole interface - text, buttons and spacing"
            "  (Ctrl+Shift+ +/-)")
        for pct in theme.SCALES:
            self._scale_combo.addItem(f"{pct}%", pct)
        current = int(round(self._config.ui_scale * 100))
        if self._scale_combo.findData(current) < 0:
            self._scale_combo.addItem(f"{current}%", current)
        self._scale_combo.setCurrentIndex(max(0, self._scale_combo.findData(current)))
        self._scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        sb.addPermanentWidget(self._scale_combo)

    def _bind_shortcuts(self):
        # Single letters (A / D / N / B / F / R / digits) are handled in
        # eventFilter so they never steal a keystroke from a text field.
        QShortcut(QKeySequence("Ctrl+Shift+D"), self, self._chk_dark.toggle)
        QShortcut(QKeySequence("Ctrl+Shift++"), self, lambda: self._nudge_scale(1))
        QShortcut(QKeySequence("Ctrl+Shift+="), self, lambda: self._nudge_scale(1))
        QShortcut(QKeySequence("Ctrl+Shift+-"), self, lambda: self._nudge_scale(-1))
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
        if not initial and self._task == TASK_SEGREGATION and task != TASK_SEGREGATION:
            self._seg_mgr.flush()      # leaving segregation: get the record on disk
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
        boxes      = self._boxes_payload()

        values = {
            "image_name": image_name,
            BOXES_FIELD:  boxes,
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
            if boxes:
                self._remember_box_label(boxes[-1].get("label", ""))
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

    def _load_labels(self):
        """Push the configured label list into the panel and the viewer."""
        labels = self._config.annotation_labels
        self._labels_panel.load_labels(labels, self._viewer.label_colors)
        self._viewer.set_label_choices(labels)

    @_safe_slot
    def _manage_labels(self):
        """Add / rename / reorder the labels boxes can be tagged with."""
        dlg = ClassEditorDialog(self._config.annotation_labels, self)
        dlg.setWindowTitle("Manage Labels")
        if dlg.exec_():
            labels = dlg.get_classes()
            self._config.annotation_labels = labels
            self._load_labels()
            self._set_status(f"Labels updated — {len(labels)} label(s).")

    @_safe_slot
    def _on_label_chosen(self, label: str):
        """
        A click in the Labels list: retag the selected box, else arm the label
        for the next box drawn.
        """
        if self._task != TASK_ANNOTATION:
            return
        if self._viewer.apply_label_to_selection(label):
            self._set_status(f"Label applied: {label}")
        else:
            self._viewer.set_sticky_label(True, label)
            self._set_status(f"Next box will be labelled: {label}")
        self._refresh_label_colors()

    def _refresh_label_colors(self):
        self._labels_panel.set_colors(self._viewer.label_colors)

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
        if self._scanner is not None:
            self._set_status("A folder scan is already running.")
            return None

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
        try:
            scanner.start()
            while scanner.isRunning():
                QApplication.processEvents()
                scanner.wait(30)
            QApplication.processEvents()      # let the queued done signal land

            # Read the flag BEFORE closing. QProgressDialog.close() emits
            # canceled() on the way out, which flips wasCanceled() to true for
            # every scan - reading it afterwards throws away a good result and
            # makes folder import look like it silently does nothing.
            cancelled = progress.wasCanceled()
        finally:
            try:
                progress.canceled.disconnect()
            except TypeError:
                pass                          # nothing was connected
            progress.close()
            progress.deleteLater()
            self._scanner = None

        if cancelled:
            self._set_status("Import cancelled.")
            return None
        return result.get("paths", [])

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
            self._viewer.load_image(path, self._boxes_from_label(existing))

        self._lbl_size.setText(self._file_size_text(path))
        self._viewer.prefetch(self._neighbours())

    def _boxes_payload(self) -> List[dict]:
        """The current boxes in the order they appear, ready to serialise."""
        out: List[dict] = []
        for box in self._viewer.get_boxes_data():
            out.append({
                "label":  box.get("label", "") or "",
                "left":   round(float(box.get("left", 0)), 2),
                "top":    round(float(box.get("top", 0)), 2),
                "right":  round(float(box.get("right", 0)), 2),
                "bottom": round(float(box.get("bottom", 0)), 2),
            })
        return out

    def _boxes_from_label(self, existing: Optional[dict]) -> List[dict]:
        """
        Rebuild the box list from a saved label file.

        Reads the current `boxes` list, and falls back to the single
        `currency_country_bounding_box` key written by older builds so images
        labelled before the change still open with their box intact. Nothing on
        disk is rewritten until that image is saved again.
        """
        if not existing:
            return []

        raw = existing.get(BOXES_FIELD)
        if isinstance(raw, str):                 # CSV stores the list as JSON
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None

        boxes: List[dict] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, dict) or item.get("left") is None:
                    continue
                boxes.append({
                    "id":     item.get("id") or f"saved{index}",
                    "label":  item.get("label", "") or "",
                    "left":   float(item["left"]),
                    "top":    float(item["top"]),
                    "right":  float(item["right"]),
                    "bottom": float(item["bottom"]),
                })
            if boxes:
                return boxes

        legacy = existing.get(BBOX_FIELD)
        if isinstance(legacy, str):
            try:
                legacy = json.loads(legacy)
            except (ValueError, TypeError):
                legacy = None
        if isinstance(legacy, dict) and legacy.get("left") is not None:
            return [{
                "id":     "saved",
                "label":  legacy.get("label", "") or "",
                "left":   float(legacy["left"]),
                "top":    float(legacy["top"]),
                "right":  float(legacy["right"]),
                "bottom": float(legacy["bottom"]),
            }]
        return []

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
            BOXES_FIELD: self._boxes_payload(),
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
        self._seg_mgr.flush()          # compact any appended record rows
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
