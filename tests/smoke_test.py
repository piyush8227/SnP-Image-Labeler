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
    seed.annotation_labels = ["USD", "GBP", "EUR"]

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
        data  = json.loads(label_file.read_text(encoding="utf-8"))
        boxes = data.get("boxes", [])
        check("Bounding box is stored",
              bool(boxes) and boxes[0].get("left") == 40, str(boxes))
        check("Box label is persisted",
              bool(boxes) and boxes[0].get("label") == "USD", str(boxes))
        check("Typed telemetry fields are gone from the schema",
              sorted(data.keys()) == ["boxes", "image_name"],
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

    # ---- Box edits mark dirty, programmatic loads do not ----------------
    win._navigate(2)
    wait(app, 400)
    check("Opening an image leaves it clean", not win._dirty)

    # Image 0 already has a saved box; re-opening it must load that box without
    # marking the image edited, or merely browsing would rewrite label files.
    win._navigate(0)
    wait(app, 500)
    check("Re-opening a labelled image loads its box",
          win._viewer.box_count >= 1, f"{win._viewer.box_count} boxes")
    check("Loading saved boxes does not dirty the image", not win._dirty)

    win._navigate(2)
    wait(app, 400)
    win._viewer.set_boxes([{"id": "p1", "label": "GBP", "left": 10, "top": 10,
                            "right": 90, "bottom": 70}])
    win._on_boxes_changed()
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

    # ---- Folder import: the whole folder must land in the queue ---------
    # Regression: _scan_folder used to read wasCanceled() AFTER closing the
    # progress dialog, and close() sets that flag itself - so every folder
    # import reported "cancelled" and imported nothing.
    nested = tmp / "nested"
    (nested / "sub").mkdir(parents=True)
    flat = tmp / "flat"
    flat.mkdir()
    flat_paths   = make_images(flat, 7)
    nested_paths = make_images(nested / "sub", 4)

    found = win._scan_folder(str(flat), recursive=False)
    check("Folder scan returns every image in the folder",
          found is not None and len(found) == len(flat_paths),
          f"{None if found is None else len(found)} of {len(flat_paths)}")

    before = len(win._image_paths)
    win._enqueue(found or [])
    wait(app, 400)
    check("Folder import adds the whole folder to the queue",
          len(win._image_paths) == before + len(flat_paths),
          f"{len(win._image_paths)} (was {before})")

    top = win._scan_folder(str(nested), recursive=False)
    deep = win._scan_folder(str(nested), recursive=True)
    check("Sub-folder images need the recursive walk",
          top == [] and deep is not None and len(deep) == len(nested_paths),
          f"top={top}, deep={None if deep is None else len(deep)}")
    check("Folder scan releases its worker slot", win._scanner is None)


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

    # ---- Real mouse gestures on the canvas ------------------------------
    # Everything above drives the viewer through set_boxes(). That never
    # touches mousePressEvent, which is how a crash in the drag-preview setup
    # (setBrush given a Qt.NoBrush enum instead of a QBrush) shipped unseen.
    # These checks use the actual press / move / release path.
    from PyQt5.QtCore import QPoint, QPointF, QEvent
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtTest import QTest
    from core import theme as theme_mod

    def _move_to(view, point):
        """
        A move with the button still held.

        QTest.mouseMove sends no button state, and both QGraphicsView and
        QGraphicsItem decide whether a drag is in progress from
        event.buttons() - so a QTest-only drag silently does nothing.
        """
        event = QMouseEvent(QEvent.MouseMove, QPointF(point),
                            Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        app.sendEvent(view.viewport(), event)
        app.processEvents()

    def _drag(view, start, end, steps=5):
        QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        app.processEvents()
        for i in range(1, steps + 1):
            _move_to(view, QPoint(start.x() + (end.x() - start.x()) * i // steps,
                                  start.y() + (end.y() - start.y()) * i // steps))
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, end)
        app.processEvents()

    from widgets.annotation_viewer import ViewerMode
    win._set_task(TASK_ANNOTATION)
    win._navigate(4)
    wait(app, 600)
    viewer = win._viewer
    viewer.set_max_boxes(0)
    viewer.set_sticky_label(True, "USD")     # else the drag ends in a dialog
    viewer.set_mode(ViewerMode.ANNOTATE)
    viewer.set_boxes([])
    wait(app, 200)

    drawn = viewer.box_count
    _drag(viewer, QPoint(120, 110), QPoint(300, 250))
    check("Dragging on the canvas draws a box",
          viewer.box_count == drawn + 1, f"count={viewer.box_count}")
    check("Drag preview items are cleaned up afterwards",
          viewer._preview_item is None and viewer._preview_halo is None,
          f"item={viewer._preview_item} halo={viewer._preview_halo}")

    if viewer.box_count:
        drawn_box = list(viewer._boxes.values())[-1]
        check("The drawn box takes the sticky label",
              drawn_box.label == "USD", drawn_box.label)

        # Every corner must answer a click. The label chip overhangs the
        # top-left one, and QPainterPath's odd-even default punched a hole
        # through the inner half of each handle - both made corners dead.
        from widgets.annotation_viewer import BoxItem
        corners = {}
        rect = drawn_box.scene_rect()
        for name, pt in (("top-left", rect.topLeft()),
                         ("top-right", rect.topRight()),
                         ("bottom-left", rect.bottomLeft()),
                         ("bottom-right", rect.bottomRight())):
            hit = viewer._scene.itemAt(pt, viewer.transform())
            local = drawn_box.mapFromScene(pt)
            corners[name] = (isinstance(hit, BoxItem),
                             drawn_box._handle_at(local) is not None)
        check("All four corners hit the box, not the label chip",
              all(is_box for is_box, _ in corners.values()), str(corners))
        check("All four corners resolve to a resize handle",
              all(has_handle for _, has_handle in corners.values()), str(corners))

        # ---- resize by a corner, with no prior selection -----------------
        before_box = drawn_box.scene_rect()
        corner = viewer.mapFromScene(before_box.bottomRight())
        _drag(viewer, corner, QPoint(corner.x() + 60, corner.y() + 45))
        after_box = drawn_box.scene_rect()
        check("Dragging a corner resizes the box",
              abs(after_box.width() - before_box.width()) > 2,
              f"{before_box.width():.0f} -> {after_box.width():.0f}")

        # ---- move by the body --------------------------------------------
        before_box = drawn_box.scene_rect()
        centre = viewer.mapFromScene(before_box.center())
        _drag(viewer, centre, QPoint(centre.x() - 50, centre.y() - 35))
        after_box = drawn_box.scene_rect()
        check("Dragging the body moves the box",
              abs(after_box.x() - before_box.x()) > 2
              or abs(after_box.y() - before_box.y()) > 2,
              f"({before_box.x():.0f},{before_box.y():.0f}) -> "
              f"({after_box.x():.0f},{after_box.y():.0f})")

        check("A resized box is still inside the image",
              viewer.sceneRect().contains(drawn_box.scene_rect())
              or viewer.sceneRect().intersects(drawn_box.scene_rect()))

    # The same gestures must survive a theme and scale change, since the
    # preview chrome is rebuilt from the theme on every press.
    for mode, pct in ((theme_mod.DARK, 200), (theme_mod.LIGHT, 100)):
        win._config.ui_theme = mode
        win._config.ui_scale = pct / 100.0
        win._apply_appearance()
        wait(app, 300)
        viewer.set_mode(ViewerMode.ANNOTATE)
        n_before = viewer.box_count
        _drag(viewer, QPoint(150, 300), QPoint(270, 400))
        check(f"Drawing still works in {mode} at {pct}%",
              viewer.box_count == n_before + 1, f"count={viewer.box_count}")
    win._config.ui_theme = theme_mod.LIGHT
    win._config.ui_scale = 1.0
    win._apply_appearance()
    viewer.set_mode(ViewerMode.NAVIGATE)

    # Clearing boxes that were drawn at a different UI scale used to abort the
    # process outright. Box chrome is sized in screen pixels, so changing the
    # scale (or the zoom) changes every boundingRect(); QGraphicsScene indexes
    # items by those rectangles and must be told BEFORE they move. set_chrome()
    # ran first, leaving the index pointing at rectangles that no longer
    # matched - and the stale entries took the process down when the items were
    # removed. Reaching the check below at all is the regression test.
    viewer.set_boxes([])
    win._dirty = False
    wait(app, 300)
    check("Clearing boxes drawn at another UI scale does not crash",
          viewer.box_count == 0, f"count={viewer.box_count}")

    # The same hazard on the zoom path: zoom, then clear.
    viewer.set_mode(ViewerMode.ANNOTATE)
    _drag(viewer, QPoint(140, 120), QPoint(300, 240))
    wait(app, 200)
    viewer.zoom_in()
    wait(app, 150)
    viewer.zoom_out()
    wait(app, 150)
    viewer.set_boxes([])
    wait(app, 300)
    check("Clearing boxes after a zoom does not crash",
          viewer.box_count == 0, f"count={viewer.box_count}")
    viewer.set_mode(ViewerMode.NAVIGATE)
    win._dirty = False
    wait(app, 150)

    # ---- Segregation record: O(1) writes must stay correct --------------
    # Regression: assign() used to rewrite the whole CSV (and copy it to the
    # backup) on every image. At 6,000 rows that was ~34 ms of a 42 ms assign
    # while the image copy itself took 0.5 ms, so sorting 10,000 images crawled.
    # Rows are now appended and the file compacted periodically - these checks
    # pin down that the cheaper write path still round-trips exactly.
    from core.segregation_manager import SegregationManager

    seg_dir = str(dataset)
    batch_dir = tmp / "seg_batch"
    batch_dir.mkdir()
    many = make_images(batch_dir, 12)
    for p in many:
        assigned, why = win._seg_mgr.assign(p, [{"class": "USD", "split": "train"}])
        if not assigned:
            break
    check("Batch assign records every image",
          all(win._seg_mgr.lookup(p) for p in many), why if not assigned else "")
    check("Assign appends rather than rewriting the whole record",
          win._seg_mgr._appended > 0, f"appended={win._seg_mgr._appended}")

    win._seg_mgr.flush()
    reloaded = SegregationManager(seg_dir)
    check("Appended rows survive a reload",
          all(reloaded.lookup(p) for p in many),
          f"{len(reloaded._records)} rows on disk")
    check("Reload matches memory exactly",
          set(reloaded._records) == set(win._seg_mgr._records),
          f"{len(reloaded._records)} vs {len(win._seg_mgr._records)}")

    # Re-assigning must supersede the old row, not add a second one
    rows_before = len(win._seg_mgr._records)
    win._seg_mgr.assign(many[0], [{"class": "USD", "split": "val"}])
    win._seg_mgr.flush()
    reloaded = SegregationManager(seg_dir)
    check("Re-assign supersedes the old row",
          len(reloaded._records) == rows_before
          and reloaded.lookup(many[0])["splits"] == ["val"],
          f"{len(reloaded._records)} vs {rows_before}")

    # The size index is what makes content-duplicate lookup O(1); if it drifts
    # out of step with the records, renamed duplicates stop being detected.
    rebuilt = {}
    for k, rec in win._seg_mgr._records.items():
        rebuilt.setdefault(int(rec.get("size_bytes") or 0), []).append(k)
    check("Size index stays in step with the records",
          {k: sorted(v) for k, v in win._seg_mgr._by_size.items()}
          == {k: sorted(v) for k, v in rebuilt.items()})

    # Counts are cached per navigation; the cache must die on every change
    counts_before = dict(win._seg_mgr.all_counts().get("USD", {}))
    win._seg_mgr.assign(many[1], [{"class": "GBP", "split": "test"}])
    check("Counts cache is invalidated by a change",
          win._seg_mgr.all_counts().get("GBP", {}).get("test") == 1
          and win._seg_mgr.all_counts()["USD"] != counts_before,
          f"{counts_before} -> {win._seg_mgr.all_counts()}")

    # A renamed copy is still caught at assign time, but status queries must
    # never read the disk - that is what made a 10,000 row queue slow.
    renamed = batch_dir / "renamed_copy.png"
    shutil.copy2(many[2], renamed)
    check("Renamed duplicate still found by content at assign time",
          win._seg_mgr.find_record(str(renamed)) is not None)
    check("Status lookup is path-only (no disk read)",
          win._seg_mgr.lookup(str(renamed)) is None
          and win._seg_mgr.is_done(str(renamed)) is False)

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
          len(session_data.get("queue", [])) == len(win._image_paths),
          f'{len(session_data.get("queue", []))} of {len(win._image_paths)}')

    # ------------------------------------------------------------------ #
    print("")
    print("Image queue - scrolling")
    # ------------------------------------------------------------------ #
    # The queue used QListView.Batched layout, which lays the view out a batch
    # at a time on a timer and restarts from item 0 whenever anything
    # invalidates it - marking a row done, a status refresh. While it caught
    # up, the scrollbar range collapsed to a single batch: the view jumped back
    # to the top mid-session and the lower rows could not be reached, because
    # dragging past the first batch snapped straight back.
    from PyQt5.QtWidgets import QListView
    from widgets.image_queue import ImageQueuePanel, PENDING

    scroll_dir = tmp / "scroll_imgs"
    scroll_dir.mkdir()
    ROWS = 400                      # comfortably more than one 120-item batch
    scroll_paths = make_images(scroll_dir, ROWS)

    sq = ImageQueuePanel()
    sq.resize(320, 600)
    sq.show()
    wait(app, 200)
    sq.load_images(scroll_paths, lambda path: PENDING)
    wait(app, 900)

    bar = sq._list.verticalScrollBar()
    check("Queue lays out in a single pass, not batches",
          sq._list.layoutMode() == QListView.SinglePass,
          f"layoutMode={sq._list.layoutMode()}")
    check("Queue keeps uniform row sizes (what makes that cheap)",
          sq._list.uniformItemSizes())

    row_h = sq._list.sizeHintForRow(0) + 2 * sq._list.spacing()
    full_range = ROWS * row_h - sq._list.viewport().height()
    check("Scroll range covers the whole queue, not one batch",
          bar.maximum() > full_range * 0.8,
          f"max={bar.maximum()} expected around {full_range}")

    bar.setValue(bar.maximum())
    wait(app, 300)
    at_bottom = bar.value()
    check("The bottom of the queue can be reached",
          at_bottom >= bar.maximum() - 2, f"{at_bottom} of {bar.maximum()}")
    wait(app, 500)
    check("And it stays there",
          bar.value() >= bar.maximum() - 2, f"{bar.value()} of {bar.maximum()}")

    # Park half way, then do the things that used to throw it to the top.
    bar.setValue(bar.maximum() // 2)
    wait(app, 250)
    parked = bar.value()
    check("A mid-queue position can be held", parked > 0, str(parked))

    for i in range(60):
        sq.mark_labeled(scroll_paths[i], True)
    wait(app, 400)
    check("Marking rows done does not move the scroll",
          abs(bar.value() - parked) <= 4,
          f"{parked} -> {bar.value()}")

    sq.refresh_status(lambda path: PENDING)
    wait(app, 400)
    check("A status refresh does not move the scroll",
          abs(bar.value() - parked) <= 4,
          f"{parked} -> {bar.value()}")

    wait(app, 600)                  # let thumbnails stream in
    check("Streaming thumbnails do not move the scroll",
          abs(bar.value() - parked) <= 4,
          f"{parked} -> {bar.value()}")

    # Navigating to a far row must actually bring that row into view.
    far = ROWS - 10
    sq.set_current_index(far)
    wait(app, 350)
    check("Navigating to a far row scrolls to it",
          sq._list.currentRow() == far and bar.value() > parked,
          f"row={sq._list.currentRow()} scroll={bar.value()} parked={parked}")
    first, last = sq._visible_row_range()
    check("The far row is inside the visible range",
          first <= far <= last, f"visible {first}-{last}, wanted {far}")

    sq.shutdown()
    sq.close()
    wait(app, 200)

    # ---- Shutdown --------------------------------------------------------
    win.close()
    wait(app, 400)
    check("Window closed without error", True)

    # ------------------------------------------------------------------ #
    print("")
    print("Appearance - theme + UI scale")
    # ------------------------------------------------------------------ #
    from core import theme as theme_mod

    tpl = theme_mod.load_template()
    check("Stylesheet template carries no raw hex colours",
          "#" not in tpl.split("*/", 1)[-1].replace("#", "", 0) or
          not __import__("re").search(r"#[0-9a-fA-F]{6}", tpl),
          "raw colours must live in core/theme.py")

    for mode in (theme_mod.LIGHT, theme_mod.DARK):
        for factor in (1.0, 1.25, 2.0):
            sheet = theme_mod.build_stylesheet(tpl, mode, factor)
            check(f"Stylesheet resolves for {mode} at {int(factor * 100)}%",
                  "{{" not in sheet and "}}" not in sheet,
                  "unresolved token left in the stylesheet")

    base = theme_mod.build_stylesheet(tpl, theme_mod.LIGHT, 1.0)
    twice = theme_mod.build_stylesheet(tpl, theme_mod.LIGHT, 2.0)
    check("UI scale doubles the pixel sizes",
          "font-size: 16px" in base and "font-size: 32px" in twice)
    # A hairline must never round down to nothing, at any scale.
    import re as _re
    zeroed = []
    for factor in (0.5, 0.75, 1.25, 2.0):
        sized = theme_mod.build_stylesheet(tpl, theme_mod.LIGHT, factor)
        if any(int(n) == 0 for n in _re.findall(r"(?<![\w.])(\d+)px", sized)):
            zeroed.append(factor)
    template_zeros = [n for n in _re.findall(r"(?<![\w.])(\d+)px", tpl) if int(n) == 0]
    check("A 1px border never scales away",
          not zeroed or bool(template_zeros),
          f"scales producing 0px: {zeroed}")
    check("Light and dark resolve to different colours",
          theme_mod.color(theme_mod.LIGHT, "bg") != theme_mod.color(theme_mod.DARK, "bg"))
    check("Box palette differs per theme",
          theme_mod.box_colors(theme_mod.LIGHT) != theme_mod.box_colors(theme_mod.DARK))

    scaled_win = MainWindow()
    scaled_win.resize(1280, 800)
    scaled_win.show()
    wait(app, 300)
    for pct, mode in ((100, "light"), (200, "dark"), (125, "light")):
        scaled_win._config.ui_scale = pct / 100.0
        scaled_win._config.ui_theme = mode
        scaled_win._chk_dark.setChecked(mode == "dark")
        scaled_win._apply_appearance()
        wait(app, 250)
        check(f"Panels keep scaling at {pct}% ({mode})",
              scaled_win._queue.minimumWidth() == theme_mod.px(200)
              and scaled_win._right_stack.minimumWidth() == theme_mod.px(300),
              f"queue={scaled_win._queue.minimumWidth()} "
              f"right={scaled_win._right_stack.minimumWidth()}")
        check(f"Scale control shows {pct}%",
              scaled_win._scale_combo.currentText() == f"{pct}%",
              scaled_win._scale_combo.currentText())

    # Every command must be reachable from the menu bar at ANY scale. The
    # toolbar used to carry all 27 commands and silently dropped two thirds of
    # them above 100% - the overflow chevron was a fixed 15px sliver nobody
    # could find. Menus are pop-ups, so they never run out of room.
    from PyQt5.QtWidgets import QToolBar, QToolButton

    menu_bar = scaled_win.menuBar()
    menus = [a.menu() for a in menu_bar.actions() if a.menu()]
    check("Menu bar groups commands into menus",
          [m.title().replace("&", "") for m in menus]
          == ["File", "Edit", "View", "Task", "Tools", "Help"],
          str([m.title() for m in menus]))

    def _commands(menu):
        found = []
        for item in menu.actions():
            if item.isSeparator():
                continue
            if item.menu():
                found += _commands(item.menu())
            else:
                found.append(item)
        return found

    all_commands = []
    for menu in menus:
        all_commands += _commands(menu)
    check("Menu bar carries the whole command set",
          len(all_commands) >= 35, str(len(all_commands)))

    # The commands that used to fall off the toolbar must all be in a menu.
    must_reach = [
        scaled_win._act_undo, scaled_win._act_redo, scaled_win._act_export,
        scaled_win._act_outdir, scaled_win._act_dsdir, scaled_win._act_labels,
        scaled_win._act_classes, scaled_win._act_sticky, scaled_win._act_grid,
        scaled_win._act_zoom_lock, scaled_win._act_refresh, scaled_win._act_delbox,
        scaled_win._act_openrec, scaled_win._act_opends,
    ]
    missing = [a.text().split("\t")[0] for a in must_reach
               if a not in all_commands]
    check("Commands dropped by the toolbar all live in a menu",
          not missing, "missing: " + str(missing))

    for pct in (100, 150, 200, 300):
        scaled_win._config.ui_scale = pct / 100.0
        scaled_win._apply_appearance()
        wait(app, 200)
        check(f"Menu bar stays usable at {pct}%",
              menu_bar.isVisible() and menu_bar.height() >= theme_mod.px(18),
              f"height={menu_bar.height()}")

    # Whether the toolbar fits depends on the window, so the invariant is not
    # "it always fits" - it is that overflow stays REACHABLE. When buttons do
    # not fit, the chevron must be shown and must itself be scaled; Qt's own
    # 15px default is the sliver that made commands look like they vanished.
    for pct in (100, 150, 200, 300):
        scaled_win._config.ui_scale = pct / 100.0
        scaled_win._apply_appearance()
        wait(app, 250)
        bar_now = scaled_win.findChildren(QToolBar)[0]
        overflows = bar_now.sizeHint().width() > bar_now.width()
        chevron = [b for b in bar_now.findChildren(QToolButton)
                   if b.objectName() == "qt_toolbar_ext_button"]
        if overflows:
            ok = bool(chevron) and chevron[0].isVisible()                  and chevron[0].width() >= theme_mod.px(20)
            check(f"Overflowing toolbar stays reachable at {pct}%", ok,
                  f"chevron={chevron[0].width() if chevron else None}px "
                  f"min={theme_mod.px(20)}px")
        else:
            check(f"Toolbar fits outright at {pct}%", True,
                  f"hint={bar_now.sizeHint().width()} width={bar_now.width()}")

    # Toolbar entries mirror their menu action, including per-task visibility.
    scaled_win._set_task(TASK_SEGREGATION)
    wait(app, 300)
    proxies = {a.text(): a for a in scaled_win._tb_proxies}
    check("Toolbar follows the menu action when the task changes",
          proxies["Assign"].isVisible() and not proxies["Annotate"].isVisible(),
          f"assign={proxies['Assign'].isVisible()} "
          f"annotate={proxies['Annotate'].isVisible()}")
    scaled_win._set_task(TASK_ANNOTATION)
    wait(app, 300)
    check("And back again",
          not proxies["Assign"].isVisible() and proxies["Annotate"].isVisible())

    # Theme and scale are reachable from the menu as well as the status bar.
    scaled_win._scale_actions[150].trigger()
    wait(app, 200)
    check("View > Interface Scale changes the scale",
          abs(scaled_win._config.ui_scale - 1.5) < 1e-6,
          str(scaled_win._config.ui_scale))
    check("Status-bar control follows the menu",
          scaled_win._scale_combo.currentText() == "150%",
          scaled_win._scale_combo.currentText())
    scaled_win._act_dark.setChecked(True)
    scaled_win._on_dark_action()
    wait(app, 200)
    check("View > Dark Theme switches the theme",
          scaled_win._config.ui_theme == theme_mod.DARK
          and scaled_win._chk_dark.isChecked(),
          scaled_win._config.ui_theme)

    # Overflowing toolbar actions must stay reachable, not silently vanish.
    scaled_win._config.ui_scale = 2.0
    scaled_win._apply_appearance()
    wait(app, 300)
    bar = scaled_win.findChildren(QToolBar)[0]
    ext = [b for b in bar.findChildren(QToolButton)
           if b.objectName() == "qt_toolbar_ext_button"]
    check("Toolbar offers an overflow button when scaled up",
          bool(ext) and bar.sizeHint().width() > bar.width(),
          f"hint={bar.sizeHint().width()} width={bar.width()}")
    scaled_win.close()
    wait(app, 200)

    # ------------------------------------------------------------------ #
    print("")
    print("Labels - schema and legacy files")
    # ------------------------------------------------------------------ #
    legacy_json = {
        "image_name": "old.jpg", "denomination": "500",
        "currency_country_bounding_box": {
            "left": 5, "top": 6, "right": 70, "bottom": 80, "label": "USD"},
    }
    parsed = win._boxes_from_label(legacy_json)
    check("A pre-upgrade label file still opens with its box",
          len(parsed) == 1 and parsed[0]["label"] == "USD"
          and parsed[0]["left"] == 5, str(parsed))
    check("The legacy CSV form (JSON in a cell) also opens",
          len(win._boxes_from_label(
              {"currency_country_bounding_box":
               json.dumps(legacy_json["currency_country_bounding_box"])})) == 1)
    multi = {"boxes": [
        {"label": "USD", "left": 1, "top": 2, "right": 3, "bottom": 4},
        {"label": "GBP", "left": 5, "top": 6, "right": 7, "bottom": 8}]}
    check("Several labelled boxes round-trip",
          len(win._boxes_from_label(multi)) == 2)
    check("Several labelled boxes round-trip through CSV",
          len(win._boxes_from_label({"boxes": json.dumps(multi["boxes"])})) == 2)
    check("A file with no boxes yields none",
          win._boxes_from_label({}) == [] and win._boxes_from_label(None) == [])

    cfg = cfg_mod.ConfigManager()
    check("Annotation allows more than one box",
          cfg.annotation_max_boxes == 0, str(cfg.annotation_max_boxes))
    cfg.annotation_labels = ["Zed", " zed ", "", "Alpha"]
    check("Label list de-duplicates and trims",
          cfg.annotation_labels == ["Zed", "Alpha"], str(cfg.annotation_labels))


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
