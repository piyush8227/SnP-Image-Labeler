"""
SnP Image Labeler - Image Queue Panel
Left panel with:
  - Background thumbnail loading (QImage in thread → QPixmap in main thread)
  - Green/grey label-status indicators
  - Right-click context menu: Remove from Queue / Delete Image
  - Clear All button in header
"""

from pathlib import Path
from typing import Callable, List, Optional

from PyQt5.QtCore import Qt, QSize, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QIcon, QPixmap, QColor, QImage
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QVBoxLayout, QWidget,
)


# ------------------------------------------------------------------ #
#  Background thumbnail loader — uses QImage (thread-safe)            #
# ------------------------------------------------------------------ #

class ThumbnailLoader(QThread):
    thumbnail_ready = pyqtSignal(str, QImage)
    SIZE = 56

    def __init__(self, paths: List[str], parent=None, size: Optional[int] = None):
        super().__init__(parent)
        self._paths     = list(paths)
        self._size      = size or self.SIZE
        self._cancelled = False

    def run(self):
        for path in self._paths:
            if self._cancelled:
                break
            try:
                img = QImage(path)
                if not img.isNull():
                    thumb = img.scaled(
                        self._size, self._size,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                    self.thumbnail_ready.emit(path, thumb)
            except Exception:
                pass

    def cancel(self):
        self._cancelled = True


# ------------------------------------------------------------------ #
#  Single queue item                                                   #
# ------------------------------------------------------------------ #

class ImageQueueItem(QListWidgetItem):
    def __init__(
        self,
        image_path: str,
        labeled: bool = False,
        done_word: str = "Labeled",
        pending_word: str = "Pending",
    ):
        super().__init__()
        self.image_path    = image_path
        self._labeled      = labeled
        self._done_word    = done_word
        self._pending_word = pending_word
        self.setSizeHint(QSize(190, 68))
        self._refresh_text()

    def set_status_words(self, done_word: str, pending_word: str):
        self._done_word    = done_word
        self._pending_word = pending_word
        self._refresh_text()

    def _refresh_text(self):
        name = Path(self.image_path).name
        if len(name) > 22:
            name = name[:19] + "..."
        status = (f"✓ {self._done_word}" if self._labeled
                  else f"○ {self._pending_word}")
        self.setText(f"{name}\n{status}")
        self.setForeground(
            QColor("#a6e3a1") if self._labeled else QColor("#a6adc8")
        )

    def set_thumbnail(self, image: QImage):
        self.setIcon(QIcon(QPixmap.fromImage(image)))

    def set_labeled(self, labeled: bool):
        if self._labeled != labeled:
            self._labeled = labeled
            self._refresh_text()

    @property
    def is_labeled(self) -> bool:
        return self._labeled


# ------------------------------------------------------------------ #
#  Panel widget                                                        #
# ------------------------------------------------------------------ #

class ImageQueuePanel(QWidget):
    image_selected      = pyqtSignal(str, int)   # (path, index)
    remove_requested    = pyqtSignal(str)         # path to remove
    delete_requested    = pyqtSignal(str)         # path to move to _rejected
    clear_all_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_paths: List[str]           = []
        self._loaders:     List[ThumbnailLoader] = []
        self._done_word    = "Labeled"
        self._pending_word = "Pending"
        self._pending_only = False
        self._setup_ui()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header row with title + Clear button
        hdr_frame = QFrame()
        hdr_frame.setObjectName("queue_header_frame")
        hdr_layout = QHBoxLayout(hdr_frame)
        hdr_layout.setContentsMargins(8, 6, 4, 6)
        hdr_layout.setSpacing(4)

        hdr_lbl = QLabel("Image Queue")
        hdr_lbl.setObjectName("panel_header_inline")
        hdr_layout.addWidget(hdr_lbl, 1)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setObjectName("queue_clear_btn")
        self._btn_clear.setFixedHeight(22)
        self._btn_clear.setToolTip("Clear entire image queue")
        self._btn_clear.setEnabled(False)
        self._btn_clear.clicked.connect(lambda _: self.clear_all_requested.emit())
        hdr_layout.addWidget(self._btn_clear)

        layout.addWidget(hdr_frame)

        # Count bar with "pending only" filter
        count_frame = QFrame()
        count_frame.setObjectName("count_frame")
        cf = QHBoxLayout(count_frame)
        cf.setContentsMargins(6, 2, 4, 2)
        cf.setSpacing(4)

        self._count_label = QLabel("0 images")
        self._count_label.setObjectName("count_label")
        self._count_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        cf.addWidget(self._count_label, 1)

        self._btn_filter = QPushButton("Pending")
        self._btn_filter.setObjectName("queue_filter_btn")
        self._btn_filter.setCheckable(True)
        self._btn_filter.setFixedHeight(22)
        self._btn_filter.setToolTip("Show only images that still need work")
        self._btn_filter.toggled.connect(self._on_filter_toggled)
        cf.addWidget(self._btn_filter)

        layout.addWidget(count_frame)

        # Image list with context menu
        self._list = QListWidget()
        self._list.setObjectName("image_list")
        self._list.setIconSize(QSize(56, 56))
        self._list.setSpacing(1)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._list)

    # ------------------------------------------------------------------ #
    #  Context menu                                                        #
    # ------------------------------------------------------------------ #

    def _show_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if not isinstance(item, ImageQueueItem):
            return
        menu = QMenu(self)
        act_remove = menu.addAction("Remove from Queue")
        act_remove.setToolTip("Take it out of this session. The file is not touched.")
        act_delete = menu.addAction("Delete Image…")
        act_delete.setToolTip(
            "Move the file into the _rejected folder and drop it from the dataset."
        )
        chosen = menu.exec_(self._list.mapToGlobal(pos))
        if chosen == act_remove:
            self.remove_requested.emit(item.image_path)
        elif chosen == act_delete:
            self.delete_requested.emit(item.image_path)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def load_images(
        self,
        paths: List[str],
        label_checker: Optional[Callable[[str], bool]] = None,
    ):
        self._stop_all_loaders()
        self._list.clear()
        self._image_paths = list(paths)

        for path in paths:
            labeled = label_checker(path) if label_checker else False
            self._list.addItem(
                ImageQueueItem(path, labeled, self._done_word, self._pending_word)
            )

        self._apply_filter()
        self._update_count()
        self._start_thumbnail_loader(paths)
        self._btn_clear.setEnabled(bool(paths))

    def add_images(
        self,
        paths: List[str],
        label_checker: Optional[Callable[[str], bool]] = None,
    ):
        self._image_paths.extend(paths)
        for path in paths:
            labeled = label_checker(path) if label_checker else False
            self._list.addItem(
                ImageQueueItem(path, labeled, self._done_word, self._pending_word)
            )
        self._apply_filter()
        self._update_count()
        self._start_thumbnail_loader(paths)
        self._btn_clear.setEnabled(True)

    def remove_image(self, path: str) -> int:
        """
        Remove one image from the queue.
        Returns the row index that was removed (-1 if not found).
        """
        for i in range(self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem) and item.image_path == path:
                self._list.takeItem(i)
                if path in self._image_paths:
                    self._image_paths.remove(path)
                self._update_count()
                self._btn_clear.setEnabled(bool(self._image_paths))
                return i
        return -1

    def mark_labeled(self, path: str, labeled: bool = True):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem) and item.image_path == path:
                item.set_labeled(labeled)
                # Keep the row visible while it is the current one, even when
                # the "Pending" filter is on — hiding it under the cursor is jarring.
                if self._pending_only and labeled and i != self._list.currentRow():
                    item.setHidden(True)
                break
        self._update_count()

    # ------------------------------------------------------------------ #
    #  Task-aware status wording / re-evaluation                           #
    # ------------------------------------------------------------------ #

    def set_status_labels(self, done_word: str, pending_word: str = "Pending"):
        """Change the per-item wording (e.g. 'Labeled' vs 'Sorted')."""
        self._done_word    = done_word
        self._pending_word = pending_word
        for i in range(self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem):
                item.set_status_words(done_word, pending_word)
        self._update_count()

    def refresh_status(self, checker: Optional[Callable[[str], bool]] = None):
        """Re-evaluate every item's done/pending state with a new checker."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem):
                item.set_labeled(checker(item.image_path) if checker else False)
        self._apply_filter()
        self._update_count()

    # ------------------------------------------------------------------ #
    #  Pending filter                                                      #
    # ------------------------------------------------------------------ #

    def _on_filter_toggled(self, checked: bool):
        self._pending_only = checked
        self._apply_filter()

    def _apply_filter(self):
        current = self._list.currentRow()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if not isinstance(item, ImageQueueItem):
                continue
            hide = self._pending_only and item.is_labeled and i != current
            item.setHidden(hide)

    def is_visible(self, index: int) -> bool:
        item = self._list.item(index)
        return item is not None and not item.isHidden()

    def is_done(self, index: int) -> bool:
        item = self._list.item(index)
        return isinstance(item, ImageQueueItem) and item.is_labeled

    def first_pending_index(self, start: int = 0) -> int:
        """First not-done row at or after `start`, else -1."""
        for i in range(max(start, 0), self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem) and not item.is_labeled:
                return i
        return -1

    def set_current_index(self, index: int):
        if 0 <= index < self._list.count():
            self._list.blockSignals(True)
            self._list.setCurrentRow(index)
            self._list.blockSignals(False)

    def current_index(self) -> int:
        return self._list.currentRow()

    def clear(self):
        self._stop_all_loaders()
        self._list.clear()
        self._image_paths.clear()
        self._update_count()
        self._btn_clear.setEnabled(False)

    @property
    def image_paths(self) -> List[str]:
        return list(self._image_paths)

    @property
    def count(self) -> int:
        return len(self._image_paths)

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _on_row_changed(self, row: int):
        if 0 <= row < len(self._image_paths):
            self.image_selected.emit(self._image_paths[row], row)

    @pyqtSlot(str, QImage)
    def _on_thumbnail_ready(self, path: str, image: QImage):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem) and item.image_path == path:
                item.set_thumbnail(image)
                break

    def _update_count(self):
        total   = self._list.count()
        labeled = sum(
            1 for i in range(total)
            if isinstance(self._list.item(i), ImageQueueItem)
            and self._list.item(i).is_labeled
        )
        pending = total - labeled
        self._count_label.setText(
            f"{total} images  ·  {labeled} {self._done_word.lower()}  ·  {pending} pending"
        )

    def _start_thumbnail_loader(self, paths: List[str]):
        loader = ThumbnailLoader(paths, self)
        loader.thumbnail_ready.connect(self._on_thumbnail_ready)
        loader.finished.connect(
            lambda l=loader: self._loaders.remove(l) if l in self._loaders else None
        )
        self._loaders.append(loader)
        loader.start()

    def _stop_all_loaders(self):
        for loader in self._loaders:
            loader.cancel()
            loader.wait(500)
        self._loaders.clear()

    def closeEvent(self, event):
        self._stop_all_loaders()
        super().closeEvent(event)
