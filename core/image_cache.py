"""
SnP Image Labeler - Image Cache & Async Loader

Two pieces, both used by the viewer:

  PixmapCache      - byte-budgeted LRU of full-resolution QPixmaps. The old
                     cache held 50 entries regardless of size, which is
                     2.4 GB for 12 MP photos. This one counts bytes.

  AsyncImageLoader - decodes QImage on worker threads so a 12 MP JPEG
                     (~60-75 ms) never blocks the UI, and prefetches the
                     neighbours of the current image so Next/Prev feel
                     instant.

QPixmap may only be touched on the GUI thread, so workers produce QImage and
the main thread converts.
"""

from collections import OrderedDict
from typing import Dict, List, Optional

from PyQt5.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt5.QtGui import QImage, QImageReader, QPixmap

DEFAULT_BUDGET_MB = 384


# ------------------------------------------------------------------ #
#  Byte-budgeted LRU pixmap cache                                      #
# ------------------------------------------------------------------ #

class PixmapCache:
    """LRU cache bounded by total pixel bytes rather than entry count."""

    def __init__(self, budget_mb: int = DEFAULT_BUDGET_MB):
        self._budget = max(64, int(budget_mb)) * 1024 * 1024
        self._items: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._bytes = 0

    @staticmethod
    def _cost(pixmap: Optional[QPixmap]) -> int:
        if pixmap is None or pixmap.isNull():
            return 0
        return max(1, pixmap.width() * pixmap.height() * (pixmap.depth() or 32) // 8)

    def get(self, path: str) -> Optional[QPixmap]:
        pixmap = self._items.get(path)
        if pixmap is not None:
            self._items.move_to_end(path)       # most-recently used
        return pixmap

    def put(self, path: str, pixmap: QPixmap) -> None:
        if pixmap is None or pixmap.isNull():
            return
        if path in self._items:
            self._bytes -= self._cost(self._items[path])
            del self._items[path]
        cost = self._cost(pixmap)
        # A single image bigger than the whole budget is kept alone, not dropped
        while self._items and self._bytes + cost > self._budget:
            _, evicted = self._items.popitem(last=False)
            self._bytes -= self._cost(evicted)
        self._items[path] = pixmap
        self._bytes += cost

    def invalidate(self, path: str) -> None:
        pixmap = self._items.pop(path, None)
        if pixmap is not None:
            self._bytes -= self._cost(pixmap)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def contains(self, path: str) -> bool:
        return path in self._items

    @property
    def megabytes(self) -> float:
        return self._bytes / (1024 * 1024)

    @property
    def count(self) -> int:
        return len(self._items)


# ------------------------------------------------------------------ #
#  Worker                                                              #
# ------------------------------------------------------------------ #

class _DecodeSignals(QObject):
    done   = pyqtSignal(str, int, QImage, QSize)   # path, token, image, original size
    failed = pyqtSignal(str, int)


class _DecodeTask(QRunnable):
    """Decode one image file to a QImage on a pool thread."""

    def __init__(self, path: str, token: int, signals: _DecodeSignals,
                 max_side: int = 0):
        super().__init__()
        self.setAutoDelete(True)
        self._path     = path
        self._token    = token
        self._signals  = signals
        self._max_side = max_side

    def run(self):
        try:
            reader = QImageReader(self._path)
            reader.setAutoTransform(True)          # honour EXIF orientation
            full = reader.size()
            if self._max_side > 0 and full.isValid():
                if max(full.width(), full.height()) > self._max_side:
                    scaled = QSize(full)
                    scaled.scale(self._max_side, self._max_side, Qt.KeepAspectRatio)
                    reader.setScaledSize(scaled)
            image = reader.read()
            if image.isNull():
                self._signals.failed.emit(self._path, self._token)
                return
            self._signals.done.emit(
                self._path, self._token, image,
                full if full.isValid() else image.size(),
            )
        except Exception:
            self._signals.failed.emit(self._path, self._token)


# ------------------------------------------------------------------ #
#  Async loader with prefetch                                          #
# ------------------------------------------------------------------ #

class AsyncImageLoader(QObject):
    """
    Decodes images off the GUI thread and keeps them in a PixmapCache.

    image_ready fires only for the request that is still current (token
    match), so holding Next never paints a stale image. Prefetch results
    land in the cache silently.
    """

    image_ready  = pyqtSignal(str, QPixmap, QSize)   # path, pixmap, original size
    image_failed = pyqtSignal(str)

    def __init__(self, cache: PixmapCache, parent=None, max_side: int = 0):
        super().__init__(parent)
        self._cache    = cache
        self._max_side = max_side
        self._token    = 0
        self._wanted: Optional[str] = None
        self._inflight: Dict[str, int] = {}
        self._sizes: Dict[str, QSize]  = {}

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(
            max(2, min(4, QThreadPool.globalInstance().maxThreadCount()))
        )

        self._signals = _DecodeSignals()
        self._signals.done.connect(self._on_decoded)
        self._signals.failed.connect(self._on_failed)

    # ---- Public ---------------------------------------------------------

    def original_size(self, path: str) -> Optional[QSize]:
        return self._sizes.get(path)

    def request(self, path: str) -> Optional[QPixmap]:
        """
        Ask for `path` as the visible image. Returns the pixmap immediately
        when it is already cached (the common case once prefetch is warm),
        otherwise None and image_ready follows.
        """
        self._token += 1
        self._wanted = path
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        self._enqueue(path, self._token, priority=10)
        return None

    def prefetch(self, paths: List[str]) -> None:
        """Warm the cache for likely-next images. Never emits image_ready."""
        for path in paths:
            if path and not self._cache.contains(path) and path not in self._inflight:
                self._enqueue(path, 0, priority=0)

    def invalidate(self, path: str) -> None:
        self._cache.invalidate(path)
        self._sizes.pop(path, None)

    def clear(self) -> None:
        self._wanted = None
        self._token += 1
        self._inflight.clear()
        self._sizes.clear()
        self._cache.clear()

    def shutdown(self) -> None:
        self._wanted = None
        self._token += 1
        try:
            self._pool.clear()                 # drop queued, un-started tasks
            self._pool.waitForDone(2000)
        except Exception:
            pass

    # ---- Internal -------------------------------------------------------

    def _enqueue(self, path: str, token: int, priority: int) -> None:
        if path in self._inflight:
            self._inflight[path] = max(self._inflight[path], token)
            return
        self._inflight[path] = token
        self._pool.start(_DecodeTask(path, token, self._signals, self._max_side),
                         priority)

    def _on_decoded(self, path: str, token: int, image: QImage, size: QSize):
        self._inflight.pop(path, None)
        self._sizes[path] = size
        pixmap = QPixmap.fromImage(image)        # main thread — safe
        if pixmap.isNull():
            if path == self._wanted:
                self.image_failed.emit(path)
            return
        self._cache.put(path, pixmap)
        if path == self._wanted:
            self.image_ready.emit(path, pixmap, size)

    def _on_failed(self, path: str, _token: int):
        self._inflight.pop(path, None)
        if path == self._wanted:
            self.image_failed.emit(path)
