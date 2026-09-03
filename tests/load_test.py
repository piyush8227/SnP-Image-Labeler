"""
SnP Image Labeler - Heavy-load benchmark

Measures the things a labeller feels on a big batch:

  1. how long the GUI thread is blocked by one Next/Prev
  2. how long a queue of 20,000 images takes to appear
  3. how many thumbnails get requested (should be the visible rows, not all)
  4. that the pixmap cache stays inside its byte budget on 12 MP images

Run:  python tests/load_test.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import sys
from pathlib import Path

# Run from anywhere: put the project root on the import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Console output: Windows consoles and pipes default to cp1252, which raises
# on any non-ASCII character and can kill a run mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PyQt5.QtGui import QImage, qRgb
from PyQt5.QtWidgets import QApplication

BIG_W, BIG_H = 4000, 3000
N_BIG        = 40
N_PATHS      = 20_000


def wait(app, ms):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.003)


def build_big_image(path: Path):
    img = QImage(BIG_W, BIG_H, QImage.Format_RGB32)
    img.fill(qRgb(120, 90, 60))
    for y in range(0, BIG_H, 7):
        for x in range(0, BIG_W, 11):
            img.setPixel(x, y, qRgb((x * 7) % 255, (y * 3) % 255, (x + y) % 255))
    img.save(str(path), "JPG", 85)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="snp_load_"))
    imgs = tmp / "images"
    out  = tmp / "labels"
    imgs.mkdir()
    out.mkdir()

    import core.config_manager as cfg_mod
    import core.thumbnail_cache as thumb_mod
    cfg_mod.ConfigManager.APP_DIR       = tmp / "appdir"
    cfg_mod.ConfigManager.CONFIG_FILE   = tmp / "appdir" / "config.json"
    cfg_mod.ConfigManager.CONFIG_BACKUP = tmp / "appdir" / "config.backup.json"
    thumb_mod.CACHE_ROOT                = tmp / "thumbs"

    app = QApplication(sys.argv)

    seed = cfg_mod.ConfigManager()
    seed.set("output_directory", str(out))
    seed.set("resume_session", False)
    seed.telemetry_fields = [{"name": "denomination", "type": "Text Input",
                              "options": []}]

    print(f"\nGenerating {N_BIG} images at {BIG_W}x{BIG_H} ({BIG_W*BIG_H/1e6:.0f} MP)…")
    master = tmp / "master.jpg"
    build_big_image(master)
    big_paths = []
    for i in range(N_BIG):
        dest = imgs / f"big_{i:03d}.jpg"
        shutil.copyfile(master, dest)
        big_paths.append(str(dest))
    print(f"  {os.path.getsize(master)/1e6:.1f} MB each\n")

    from main_window import MainWindow
    from core.session_store import SessionStore

    win = MainWindow()
    win._session = SessionStore(tmp / "session.json")
    win.resize(1280, 800)
    win.show()
    wait(app, 200)

    # ------------------------------------------------------------------ #
    # 1. GUI-thread cost of one navigation step
    # ------------------------------------------------------------------ #
    win._enqueue(big_paths)
    wait(app, 600)

    blocking = []
    for _ in range(N_BIG - 1):
        t0 = time.perf_counter()
        win._next()                       # what the keypress actually runs
        blocking.append((time.perf_counter() - t0) * 1000)
        wait(app, 60)                     # let the decode land, as a user would

    blocking.sort()
    print("Navigation - GUI thread blocked per Next:")
    print(f"  median {blocking[len(blocking)//2]:6.2f} ms")
    print(f"  p95    {blocking[int(len(blocking)*0.95)]:6.2f} ms")
    print(f"  max    {blocking[-1]:6.2f} ms")
    print(f"  (old code decoded inline: ~74 ms per image, every image)\n")

    # Second pass - everything the prefetcher touched is cached
    t0 = time.perf_counter()
    for _ in range(10):
        win._prev()
        wait(app, 40)
    print(f"Backwards pass over 10 cached images: "
          f"{(time.perf_counter()-t0)*1000:.0f} ms total\n")

    cached = win._viewer.cache_stats()
    print(f"Pixmap cache after {N_BIG} images of {BIG_W*BIG_H/1e6:.0f} MP: {cached}")
    budget = win._config.image_cache_mb
    mb = float(cached.split("/")[1].strip().split(" ")[0])
    print(f"  budget {budget} MB - {'OK' if mb <= budget + 50 else 'OVER BUDGET'}"
          f"   (old cache: 50 x 48 MB = 2,400 MB)\n")

    # ------------------------------------------------------------------ #
    # 2. Thumbnails: only what is visible should be requested
    # ------------------------------------------------------------------ #
    requested = []
    service = win._queue.thumbnails
    original_request = service.request
    service.request = lambda p, _o=original_request, _r=requested: (
        _r.append(p) or _o(p)
    )

    # ------------------------------------------------------------------ #
    # 3. A 20,000 image queue
    # ------------------------------------------------------------------ #
    win._clear_queue_confirmed = True
    win._image_paths.clear()
    win._queue.clear()
    requested.clear()

    synthetic = [str(imgs / f"ghost_{i:06d}.jpg") for i in range(N_PATHS)]
    t0 = time.perf_counter()
    win._queue.load_images(synthetic, lambda p: 0)
    build_ms = (time.perf_counter() - t0) * 1000
    wait(app, 400)

    print(f"Queue of {N_PATHS:,} rows:")
    print(f"  built in {build_ms:.0f} ms")
    print(f"  thumbnails requested: {len(requested)} "
          f"({len(requested)/N_PATHS*100:.1f}% of the queue)")
    print(f"  (old code decoded all {N_PATHS:,} at full resolution up front)\n")

    t0 = time.perf_counter()
    win._queue.refresh_status(lambda p: 0)
    refresh_ms = (time.perf_counter() - t0) * 1000
    print(f"Full status refresh over {N_PATHS:,} rows: {refresh_ms:.0f} ms")

    t0 = time.perf_counter()
    for i in range(0, 2000, 7):
        win._queue.mark_labeled(synthetic[i], True)
    mark_ms = (time.perf_counter() - t0) * 1000
    print(f"286 mark-as-done updates: {mark_ms:.1f} ms "
          f"({mark_ms/286:.3f} ms each - was ~5.6 ms at 10k rows)\n")

    service.request = original_request
    win.close()
    wait(app, 300)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
