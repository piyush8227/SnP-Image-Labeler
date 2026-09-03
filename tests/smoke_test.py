"""
SnP Image Labeler - Headless smoke test

Runs the real widgets on Qt's "offscreen" platform against generated images.
It exists to prove the things that are easy to break and hard to spot by
clicking around:

  - browsing the queue writes NO label files and marks nothing done
  - a real edit does write, and the box label survives a round trip
  - boxes are clamped to the image
  - undo/redo works on box state
  - caches, index, session store and undo stack behave under load

Run:  python tests/smoke_test.py
"""

import json
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

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap, qRgb
from PyQt5.QtWidgets import QApplication

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  - {detail}" if detail and not condition else ""))
    return bool(condition)


def wait(app, ms):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def make_images(folder: Path, count: int):
    paths = []
    for i in range(count):
        w, h = (800, 600) if i % 3 else (1600, 1200)
        img = QImage(w, h, QImage.Format_RGB32)
        img.fill(qRgb(40 + i * 3 % 200, 90, 140))
        for y in range(0, h, 23):
            for x in range(0, w, 29):
                img.setPixel(x, y, qRgb((x + i) % 255, (y * 3) % 255, 200))
        path = folder / f"img_{i:03d}.png"
        img.save(str(path), "PNG")
        paths.append(str(path))
    return paths


def main():
    tmp = Path(tempfile.mkdtemp(prefix="snp_smoke_"))
    imgs_dir = tmp / "images"
    out_dir  = tmp / "labels"
    imgs_dir.mkdir()
    out_dir.mkdir()

    # ---- Redirect every on-disk side effect into the temp folder --------
    import core.config_manager as cfg_mod
    import core.thumbnail_cache as thumb_mod
    cfg_mod.ConfigManager.APP_DIR       = tmp / "appdir"
    cfg_mod.ConfigManager.CONFIG_FILE   = tmp / "appdir" / "config.json"
    cfg_mod.ConfigManager.CONFIG_BACKUP = tmp / "appdir" / "config.backup.json"
    thumb_mod.CACHE_ROOT                = tmp / "thumbs"

    app = QApplication(sys.argv)

    # Seed the config before the window reads it
    seed = cfg_mod.ConfigManager()
    seed.set("output_directory", str(out_dir))
    seed.set("resume_session", False)
    seed.set("export_format", "json")
    seed.telemetry_fields = [
        {"name": "denomination", "type": "Text Input", "options": []},
        {"name": "is_damaged",   "type": "Boolean",    "options": []},
    ]

    paths = make_images(imgs_dir, 12)
    print(f"\nWorkspace: {tmp}\n")

    # ------------------------------------------------------------------ #
    print("Unit checks")
    # ------------------------------------------------------------------ #
    from core.image_cache import PixmapCache
    from core.label_index import LabelIndex, ReviewFlags
    from core.session_store import SessionStore
    from core.undo_stack import UndoStack

    cache = PixmapCache(budget_mb=64)
    big = QPixmap(2000, 2000)          # ~16 MB each at 32 bpp
    for i in range(8):
        cache.put(f"p{i}", QPixmap(big))
    check("PixmapCache respects its byte budget",
          cache.megabytes <= 64 + 16 and cache.count < 8,
          f"{cache.count} entries / {cache.megabytes:.0f} MB")

    undo = UndoStack()
    undo.reset([])
    undo.push([{"id": "a", "left": 1, "top": 1, "right": 5, "bottom": 5, "label": "x"}])
    undo.push([{"id": "a", "left": 2, "top": 2, "right": 6, "bottom": 6, "label": "x"}])
    step1 = undo.undo()
    step2 = undo.undo()
    check("UndoStack walks back to the baseline",
          step1 and step1[0]["left"] == 1 and step2 == [], str(step2))
    check("UndoStack redo returns the newer state",
          (undo.redo() or [{}])[0].get("left") == 1)

    # A scratch directory: these must not leave anything in the real output
    # folder, or the "browsing writes nothing" check below is meaningless.
    unit_dir = tmp / "unit_labels"
    unit_dir.mkdir()

    idx = LabelIndex(str(unit_dir))
    (unit_dir / "sample_a.json").write_text("{}", encoding="utf-8")
    idx.rebuild()
    check("LabelIndex sees an existing label", idx.has(str(imgs_dir / "sample_a.png")))
    check("LabelIndex ignores an unlabelled image", not idx.has(paths[0]))

    flags = ReviewFlags(str(unit_dir))
    flags.load()
    flags.set_flagged(paths[0], True)
    reloaded = ReviewFlags(str(unit_dir))
    reloaded.load()
    check("ReviewFlags persist to disk", reloaded.is_flagged(paths[0]))
    idx.rebuild()   # the folder now also holds _review_flags.json
    check("LabelIndex skips the flags control file", idx.size == 1,
          f"indexed {idx.size} stems")

    store = SessionStore(tmp / "session.json")
    store.save(paths, 3, "annotation")
    loaded = store.load()
    check("SessionStore round trip",
          loaded.get("index") == 3 and len(loaded.get("queue", [])) == len(paths))
    store.clear()

    # ------------------------------------------------------------------ #
    print("\nApplication checks")
    # ------------------------------------------------------------------ #
    from main_window import MainWindow
    from widgets.image_queue import DONE, FLAGGED

    win = MainWindow()
    win._session = SessionStore(tmp / "session.json")
    win.resize(1280, 800)
    win.show()
    wait(app, 200)

    win._enqueue(paths)
    wait(app, 400)
    check("Queue loaded every image", win._queue.count == len(paths),
          f"{win._queue.count}")

    # ---- The regression that mattered most ------------------------------
    for _ in range(len(paths) + 3):
        win._next()
        wait(app, 90)
    wait(app, 700)                      # outlive the 400 ms autosave debounce

    written = sorted(p.name for p in out_dir.iterdir()
                     if p.suffix in (".json", ".csv"))
    check("Browsing the queue writes no label files", not written,
          f"created {written}")
    check("Browsing marks nothing as done", win._queue.done_count == 0,
          f"done={win._queue.done_count}")

    # ---- Navigation actually moved and loaded ---------------------------
    check("Navigation reached the end of the queue",
          win._current_index == len(paths) - 1, f"index={win._current_index}")
    check("Viewer has an image on screen", win._viewer.has_image)

    # ---- A real edit does save, with its label --------------------------
    win._navigate(0)
    wait(app, 500)
    win._viewer.set_boxes([{
        "id": "b1", "label": "USD", "left": 40, "top": 30,
        "right": 240, "bottom": 190,
    }])
    wait(app, 800)                      # let the debounced autosave run

    label_file = out_dir / (Path(paths[0]).stem + ".json")
    check("An edit writes the label file", label_file.exists(), str(label_file))
    if label_file.exists():
        data = json.loads(label_file.read_text(encoding="utf-8"))
        bbox = data.get("currency_country_bounding_box", {})
        check("Bounding box is stored", bbox.get("left") == 40, str(bbox))
        check("Box label is persisted", bbox.get("label") == "USD", str(bbox))
        check("Telemetry fields are present", "denomination" in data,
              str(sorted(data.keys())))
    check("Edited image is marked done", win._queue.done_count == 1,
          f"done={win._queue.done_count}")

    # ---- Label survives a round trip ------------------------------------
    win._navigate(5)
    wait(app, 400)
    win._navigate(0)
    wait(app, 600)
    restored = win._viewer.get_single_box()
    check("Box reloads with its label", restored.get("label") == "USD", str(restored))
    check("Box reloads with its coordinates", restored.get("left") == 40, str(restored))

    # ---- Undo / redo ----------------------------------------------------
    before = len(win._viewer.get_boxes_data())
    win._viewer.set_boxes([])
    wait(app, 100)
    check("Clearing removes the box", len(win._viewer.get_boxes_data()) == 0)
    win._undo_action()
    wait(app, 100)
    after_undo = win._viewer.get_boxes_data()
    check("Ctrl+Z restores the box", len(after_undo) == before, str(after_undo))
    win._redo_action()
    wait(app, 100)
    check("Ctrl+Y re-applies the clear", len(win._viewer.get_boxes_data()) == 0)
    win._undo_action()
    wait(app, 700)

    # ---- Clamping to the image -----------------------------------------
    win._viewer.set_boxes([{
        "id": "b2", "label": "OUT", "left": -500, "top": -400,
        "right": 999999, "bottom": 999999,
    }])
    wait(app, 300)
    bounds = win._viewer.image_bounds()
    clamped = win._viewer.get_single_box()
    inside = (bounds is not None
              and clamped["left"] >= bounds.left() - 0.5
              and clamped["top"] >= bounds.top() - 0.5
              and clamped["right"] <= bounds.right() + 0.5
              and clamped["bottom"] <= bounds.bottom() + 0.5)
    check("Out-of-bounds box is clamped to the image", inside,
          f"{clamped} vs {bounds}")

    # ---- Flag for review ------------------------------------------------
    win._toggle_flag()
    wait(app, 200)
    check("Flag marks the row for review",
          win._queue.state_of(paths[0]) == FLAGGED,
          f"state={win._queue.state_of(paths[0])}")
    check("Flag is written to the output folder",
          (out_dir / "_review_flags.json").exists())
    win._toggle_flag()
    wait(app, 200)
    check("Unflagging returns the row to done",
          win._queue.state_of(paths[0]) == DONE,
          f"state={win._queue.state_of(paths[0])}")

    # ---- Thumbnails -----------------------------------------------------
    wait(app, 2500)
    icons = sum(1 for p in paths[:8]
                if win._queue._rows[p].icon() is not None
                and not win._queue._rows[p].icon().isNull())
    check("Thumbnails are generated for visible rows", icons >= 3, f"{icons}/8")
    disk_thumbs = list((tmp / "thumbs").rglob("*.png")) if (tmp / "thumbs").exists() else []
    check("Thumbnails are cached on disk", len(disk_thumbs) >= 3,
          f"{len(disk_thumbs)} files")

    # ---- Telemetry edits mark dirty, programmatic loads do not ----------
    win._navigate(2)
    wait(app, 400)
    check("Opening an image leaves it clean", not win._dirty)
    win._telemetry.set_values({"denomination": "20"})
    wait(app, 50)
    check("Programmatic set_values does not dirty the image", not win._dirty)
    win._telemetry.values_changed.emit()
    wait(app, 50)
    check("A user edit does dirty the image", win._dirty)
    win._flush_autosave()
    wait(app, 200)
    check("Flush clears the dirty flag", not win._dirty)

    # ---- Review grid -----------------------------------------------------
    from widgets.review_grid import ReviewGridDialog
    grid = ReviewGridDialog(win._image_paths, win._status_checker(),
                            win._saved_box_for, win)
    wait(app, 1800)
    check("Review grid lists the whole queue",
          grid._list.count() == len(win._image_paths),
          f"{grid._list.count()} of {len(win._image_paths)}")
    grid_icons = sum(1 for i in range(min(6, grid._list.count()))
                     if not grid._list.item(i).icon().isNull())
    check("Review grid renders thumbnails", grid_icons >= 2, f"{grid_icons}")
    grid.close()
    wait(app, 200)

    # ---- Duplicate scanner (shares the thumbnail service) ---------------
    from widgets.duplicate_scan_dialog import DuplicateScanDialog
    twin = imgs_dir / "img_000_copy.png"
    shutil.copyfile(paths[0], twin)
    dlg = DuplicateScanDialog(paths[:4] + [str(twin)], [], win, 1)
    dlg.start_scan()
    wait(app, 4000)
    check("Duplicate scan finds the copied image", len(dlg._groups) >= 1,
          f"{len(dlg._groups)} group(s)")
    dlg.close()
    wait(app, 300)

    # ---- Segregation task still works -----------------------------------
    from main_window import TASK_ANNOTATION, TASK_SEGREGATION
    dataset = tmp / "dataset"
    dataset.mkdir()
    win._config.set("segregation_output_directory", str(dataset))
    win._seg_mgr.set_root(str(dataset))
    win._config.segregation_classes = ["USD"]
    win._seg_panel.load_classes(["USD"])
    win._set_task(TASK_SEGREGATION)
    wait(app, 500)
    check("Switches to the segregation task", win._task == TASK_SEGREGATION)

    win._navigate(3)
    wait(app, 400)
    win._seg_panel._rows[0].set_checked(True)
    win._seg_panel._rows[0].set_split("train")
    wait(app, 150)
    win._seg_assign()
    wait(app, 600)
    train_dir = dataset / "USD_data" / "train"
    copied = list(train_dir.glob("*.png")) if train_dir.exists() else []
    check("Segregation copies the image into the class folder",
          len(copied) == 1, f"{copied}")
    check("Segregation marks the row sorted", win._queue.done_count >= 1,
          f"done={win._queue.done_count}")

    win._set_task(TASK_ANNOTATION)
    wait(app, 400)
    check("Switches back to the annotation task", win._task == TASK_ANNOTATION)
    check("Annotation status is restored after the task round trip",
          win._queue.state_of(paths[0]) == DONE,
          f"state={win._queue.state_of(paths[0])}")

    # ---- Session ---------------------------------------------------------
    win._save_session()
    session_data = win._session.load()
    check("Session records the queue",
          len(session_data.get("queue", [])) == len(paths), str(len(paths)))

    # ---- Shutdown --------------------------------------------------------
    win.close()
    wait(app, 400)
    check("Window closed without error", True)

    # ------------------------------------------------------------------ #
    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 62)
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("  FAILED: " + ", ".join(failed))
    print("=" * 62 + "\n")

    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
