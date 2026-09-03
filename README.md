<img src="assets/logo_wordmark.png" alt="SnP Image Labeler" width="620">

A desktop image tool built with **Python + PyQt5**, with two tasks that share one window:

| Task | What it does |
|---|---|
| **Annotation** | Label images with configurable telemetry fields + one bounding box; export CSV or JSON |
| **Segregation** | Sort images into class folders (`{Class}_data/train\|test\|val/`) to build a training dataset |

Switch tasks from the toolbar (`Task: Annotation | Segregation`) or with `Ctrl+1` / `Ctrl+2`.
The choice is remembered between sessions.

Both tasks share two dataset-hygiene tools: **Find Duplicates** (`Ctrl+D`) to catch the same
picture stored more than once under different names, and **Delete Image** (`Shift+Del`) to
move a useless image out of the working set into `_rejected`.

---

## Annotation task

| Feature | Detail |
|---|---|
| Import | Single images, multiple images, or entire folder (scanned in the background, cancellable) |
| Image Queue | Lazy thumbnails with ✓ Labeled / ○ Pending / ⛑ Review status and an All / Pending / Done / Flagged filter |
| Navigation | `D` / `A` or → / ←, or the Prev / Next buttons |
| Telemetry Fields | Text Input, Dropdown, Checkboxes, Radio Buttons, Boolean — configurable at runtime |
| Bounding Box | Draw / move / resize / relabel in Annotate mode; always clamped to the image; label saved with the coordinates |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Y` over box add, move, resize, relabel, delete and clear |
| Autosave | Debounced and **dirty-driven** — only an actual edit writes a file, so browsing never creates empty labels |
| Flag for Review | `Ctrl+F` marks an image to come back to; stored in `_review_flags.json` beside the labels |
| Review Grid | `Ctrl+G` — contact sheet of the queue with every saved box drawn on its thumbnail |
| Zoom Lock | `Ctrl+L` keeps your zoom and pan across images; a window resize never throws your zoom away |
| Progress / ETA | Status bar shows done/total, images per minute and time remaining |
| Session Resume | The queue and your position are restored after a restart |
| Auto-save Config | Field definitions persist across sessions (`~/.snp_labeler/config.json`) |
| Label Pre-fill | Existing labels + box auto-loaded when revisiting an image |
| Export Formats | CSV or JSON, selectable in toolbar |
| Output Naming | `{image_name}.csv` / `{image_name}.json` |
| Failsafe Writes | Atomic write-to-temp-then-rename; config backup on every save |

### Output file

**CSV** (`Image_1.csv`):
```
image_name,Category,Severity,currency_country_bounding_box
Image_1.jpg,Firework,High,"{""left"": 12.0, ""top"": 30.0, ""right"": 220.0, ""bottom"": 180.0}"
```

**JSON** (`Image_1.json`):
```json
{
  "image_name": "Image_1.jpg",
  "Category": "Firework",
  "Severity": "High",
  "currency_country_bounding_box": {
    "left": 12.0, "top": 30.0, "right": 220.0, "bottom": 180.0
  }
}
```

---

## Segregation task

Sorts a flat input folder into a class-wise dataset. Images are **copied** — the source
folder is never modified.

```
Input                          Output (dataset root)
-----                          ---------------------
dataset/                       Currency_data/
  Image_1.jpg   →  currency/train      train/  Image_1.jpg
  Image_2.jpg   →  currency/test       test/   Image_2.jpg
  Image_3.jpg   →  currency/train    Document_data/
                   document/val         val/   Image_3.jpg
```

### Workflow

1. **Task → Segregation**, then **Dataset Dir** to pick the dataset root.
2. **Manage Classes** (or `+ Add Class`) to define your classes — `currency` becomes `Currency_data/`.
3. **Import** the input folder. Each image is previewed in the centre.
4. Tick one or more classes; each class has its **own** Train / Test / Validation choice.
5. **Assign & Next** (or `Enter`) copies the image into every selected `{Class}_data/{split}/`
   and jumps to the next image that still needs sorting.

Directories are created only when missing, and an existing dataset is always added to,
never replaced.

### The processed record — `processed_images.csv`

Every processed image is logged in **`{dataset_root}/processed_images.csv`**, sitting in the
main output folder next to the class folders. It is a plain CSV you can open in Excel:

```
output_root/
  processed_images.csv        ← the record
  Currency_data/
  Document_data/
```

| Column | Meaning |
|---|---|
| `image_name` | File name as processed |
| `source_path` | Where it was imported from |
| `content_hash` | SHA-256 of the file contents (exact-duplicate detection) |
| `phash` | 64-bit perceptual hash (near-duplicate detection; blank on rows written before it existed, filled in by the next scan) |
| `size_bytes` | File size |
| `classes` / `splits` | Pipe-separated and positionally paired: `currency\|document` + `test\|val` |
| `destinations` | Every copy made, relative to the dataset root |
| `processed_at` | When it was processed |

The file is written atomically (temp → rename) with a mirror copy in
`.processed_images.backup.csv`. An older `.snp_segregation/manifest.json` from a previous
version is migrated into the CSV automatically the first time that dataset root is opened.

### Not processing the same image twice

Before copying, the app looks the image up in the record — first by source path, then by
**file contents**, so a renamed or re-downloaded copy of the same picture is caught too.
(Content hashing only runs when a recorded image has the same byte size, so importing a
folder of genuinely new images costs no hashing.)

If it is already there, you get a prompt:

```
'renamed_copy.jpg' has the same contents as 'Image_1.jpg', which was already processed.

Processed on 2026-08-29 13:52:35 into:
    • Currency_data/train/
    • Document_data/val/

Skip it, or update it with the classes selected now?

[x] Apply my choice to all remaining images        [ Skip ] [ Update ] [ Cancel ]
```

- **Skip** — leaves the existing copies alone and moves to the next image.
- **Update** — re-assigns it to the classes/splits selected now, deleting copies it no longer needs.
- **Cancel** — changes nothing.
- **Apply my choice to all remaining** — reuses that answer for the rest of the session; it is
  cleared when you import images again or change the dataset folder.

**Open Record** in the toolbar opens the CSV.

### Resuming a part-finished job

Sort 200 of 1000 images today; tomorrow, point at the same dataset root and:

- already-sorted images show **✓ Sorted** in the queue, with a `Segregated: 200 / 1000` counter;
- the **Pending** filter in the queue hides finished images so only the remaining 800 are shown;
- new work is appended — nothing already in the dataset is overwritten or duplicated;
- changing an image's class/split deletes the old copy, so no stray duplicate is left behind;
- if you delete a copied file by hand, that image goes back to Pending automatically.

### Options

| Option | Detail |
|---|---|
| Multi-class | One image can go into several classes at once, each with its own split |
| Auto split by ratio | Optional. Picks the split per class to track a target ratio (default 70/20/10); still overridable per class |
| Keep selection | Keeps the class/split selection for the next image — fast for long runs of one class |
| Remove from dataset | Deletes the copies made for the current image, clears its record row, and marks it Pending again |
| Open Dataset | Opens the dataset root in the file browser |
| Open Record | Opens `processed_images.csv` |

### Finding repeated images  —  `Find Duplicates` (Ctrl+D)

The assign-time prompt only fires on the image you are about to sort. **Find Duplicates**
sweeps the whole working set in one go and shows every picture that is stored more than
once, whatever the files are called:

- **Exact copies only** — identical bytes (SHA-256). No false matches.
- **Near-identical / Similar / Loose** — a 64-bit difference hash (dHash) compares what the
  picture *looks* like, so a resize, a re-save at another quality, a screenshot of the same
  photo or a small crop is still recognised.

Tick `Also compare with already-sorted images` (on by default) to scan the queue against
every row of `processed_images.csv` too — that catches re-importing something you sorted
weeks ago under a different name.

Results are grouped, one group per picture. The first file in each group is the one worth
keeping — the copy already in the dataset if there is one, otherwise the oldest file — and
every other copy is ticked for you. Selecting a group shows the members side by side so a
"similar" match can be judged by eye; double-click a row to jump the queue to that image.

Then either:

| Button | Effect |
|---|---|
| Remove Ticked from Queue | Takes them out of this session only. No file is touched. |
| Move Ticked to `_rejected` | Moves the files out of the working set (see below). |

Groups where every copy is ticked are flagged before anything happens, so you never
accidentally throw away all versions of a picture.

### Deleting a useless image  —  `Delete Image` (Shift+Del)

For an image that simply does not belong in the dataset — blurred, wrong subject, corrupt.
Also on the right-click menu of any queue row, and available in both tasks.

Nothing is ever destroyed. The image is **moved** to a `_rejected` folder:

```
{dataset root}/_rejected/{image}
{dataset root}/_rejected/rejected_log.csv
```

`rejected_log.csv` records the time, the original path, where the file went and why — so an
image is put back by moving it from `_rejected` to the path in that row.

In one action, Delete Image:

- moves the source file into `_rejected`;
- deletes any copies already made into `{Class}_data/{split}/` and clears the row from
  `processed_images.csv`, so the class counts stay honest;
- offers to delete the label file too, if the annotation task wrote one;
- removes the image from the queue and moves on to the next one.

`_rejected` folders are skipped when importing a folder, so rejected images never come back
in on the next import. Set `rejected_directory` in the config to send them somewhere else;
left blank they go to the dataset root, or next to the image itself if no dataset root is
set yet.

### Name collisions

If a different image with the same filename already exists in a destination folder, the new
copy is saved as `name_1.jpg`, `name_2.jpg`, … An identical re-copy of the same image is
skipped instead of duplicated.

---

## Installation

```bash
pip install -r requirements.txt
```

## Run

```bash
cd snp_labeler
python main.py
```

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `D` / `→` | **Next image** |
| `A` / `←` | **Previous image** |
| `Ctrl+S` | **Save label** *(annotation)* — Assign *(segregation)* |
| `Ctrl+Z` / `Ctrl+Y` | **Undo / Redo** the last box change *(annotation)* |
| `Ctrl+1` / `Ctrl+2` | Switch to Annotation / Segregation task |
| `Ctrl+O` | Import images |
| `N` / `B` | Navigate / Annotate (Box) mode *(annotation task)* |
| `Del` | Delete selected box *(annotation task)* |
| `Alt+←↑→↓` | Nudge the selected box 1 px (add `Shift` for 10 px) |
| `Space` (hold) or middle-mouse | Pan — works while annotating |
| `1` … `9` | Quick label *(annotation)* / Train, Test, Val *(segregation)* |
| `Ctrl+F` | Flag the current image for review |
| `Ctrl+G` | Open the Review Grid (contact sheet) |
| `Ctrl+L` | Lock zoom — keep this zoom/pan on the next image |
| `Ctrl++` / `Ctrl+-` / `Ctrl+0` | Zoom in / out / 100% |
| `F` | Fit image to window |
| `R` | Reload the current image from disk |
| `Ctrl+D` | Find duplicate / near-identical images |
| `Shift+Del` | Delete current image — move it to `_rejected` |
| `Enter` | Assign & Next *(segregation task)* |

Single-letter keys never fire while you are typing in a text box or a dropdown.

---

## Performance notes

Built to stay responsive on batches in the tens of thousands:

| Under load | How it behaves |
|---|---|
| Image decoding | Off the GUI thread, with the next two and previous image prefetched. One Next blocks the UI for ~0.6 ms (median, 12 MP JPEGs) instead of decoding inline. |
| Held-down Next/Prev | Coalesced — skimming 40 images decodes the one you stop on, not all 40. |
| Queue thumbnails | Scaled decode (~4x faster than a full decode), a small thread pool, a disk cache in `~/.snp_labeler/thumbs`, and only the rows near the viewport are requested. A 20,000 image queue asks for ~40 thumbnails, not 20,000. |
| Full-size image cache | LRU with a **byte** budget (`image_cache_mb`, default 384 MB). The old count-based cache could hold 2.4 GB of 12 MP photos. |
| "Is this labelled?" | One directory scan into an in-memory index instead of two file stats per image. A status refresh over 20,000 rows takes ~14 ms. |
| Queue updates | Row lookup is a dict; marking an image done is ~0.005 ms regardless of queue size. |

Cached thumbnails live in `~/.snp_labeler/thumbs` and can be deleted at any time —
they are rebuilt on demand.

---

## Brand assets

| File | What it is |
|---|---|
| `assets/logo_master.png` | The 1254 px master the app icon is built from. Regenerate icons from this. |
| `assets/logo_wordmark.png` | Horizontal lockup (symbol + name) for the README header, the About box and slides. |
| `icon.ico` | Multi-resolution app icon: 16, 24, 32, 48, 64, 128, 256 px. Used by the .exe and by `setWindowIcon()`. |
| `icon.png` | 256 px runtime fallback. |

Rebuilding after a new master, or trying a different candidate:

```bash
python tools/pick_icon.py logo_candidates          # compare candidates at 16/32/64 px
python tools/make_icons.py assets/logo_master.png  # writes icon.ico + icon.png
python main.py                                     # check the title bar and taskbar
```

`tools/make_icons.py` knocks the flat background corners out to transparency so the
rounded tile does not sit on a black or white square in Explorer; pass
`--no-knockout` to keep them.

If Windows still shows the old icon after a rebuild, its icon cache is stale:

```bash
ie4uinit.exe -show
```

---

## Project layout

```
snp_labeler/
├─ main.py                  Entry point: crash hook, styling, app icon
├─ main_window.py           The window: toolbar, navigation, autosave, both tasks
├─ style.qss                Catppuccin Mocha stylesheet for the whole UI
│
├─ core/                    No Qt widgets in here - logic and storage only
│  ├─ config_manager.py     ~/.snp_labeler/config.json, atomic writes + backup
│  ├─ label_manager.py      Reads and writes the per-image CSV/JSON label files
│  ├─ label_index.py        One directory scan answering "is this image done?"
│  ├─ image_cache.py        Byte-budgeted pixmap LRU + threaded decode/prefetch
│  ├─ thumbnail_cache.py    Scaled-decode thumbnails with an on-disk cache
│  ├─ session_store.py      Restores the queue and your place after a restart
│  ├─ undo_stack.py         Snapshot undo/redo for bounding boxes
│  ├─ segregation_manager.py  Class folders, splits, processed_images.csv
│  ├─ duplicate_finder.py   SHA-256 + perceptual hashing, grouping
│  └─ rejection_manager.py  Moving images into _rejected with a log
│
├─ widgets/                 Qt widgets only
│  ├─ image_viewer.py       Async loading, zoom/pan, zoom lock
│  ├─ annotation_viewer.py  Bounding boxes: draw, move, resize, clamp, label
│  ├─ image_queue.py        Left panel: lazy thumbnails, states, filters
│  ├─ telemetry_panel.py    Right panel: the configurable fields
│  ├─ bbox_panel.py         Right panel: the box list
│  ├─ review_grid.py        Ctrl+G contact sheet with boxes drawn on
│  ├─ segregation_panel.py  Right panel: classes and splits
│  ├─ duplicate_scan_dialog.py / duplicate_dialog.py
│  ├─ field_editor.py       Manage Fields dialog
│  └─ class_editor.py       Manage Classes dialog
│
├─ tests/                   Headless, no fixtures needed
│  ├─ smoke_test.py         43 checks over the real widgets
│  └─ load_test.py          Benchmarks against 12 MP images and a 20k queue
│
├─ tools/                   Development utilities, not shipped
│  ├─ make_icons.py         Master PNG -> multi-resolution icon.ico + PNGs
│  └─ pick_icon.py          Compares icon candidates at 16/32/64 px
│
├─ assets/                  Brand source files (not bundled into the exe)
├─ logo_candidates/         The generated icon options and their comparison sheet
│
├─ icon.ico / icon.png      The app icon, bundled and used at runtime
├─ requirements.txt         PyQt5
├─ SnP_Image_Labeler.spec   PyInstaller build definition
├─ runtime_hook.py          Resolves bundled asset paths inside the .exe
├─ version_info.txt         Windows .exe version resource
├─ build_windows.bat        One-command Windows build
└─ build_linux_mac.sh       The same for Linux and macOS
```

---

## Development

Everything runs headless - no display, no sample images to prepare.

```bash
python tests/smoke_test.py     # 43 checks: correctness, autosave, undo, both tasks
python tests/load_test.py      # navigation latency, cache budget, 20,000-row queue
```

`smoke_test.py` generates its own images in a temp folder and redirects the
config, session and thumbnail cache there, so it never touches your real
settings or output. Run it after any change to the viewer, the queue or the
autosave path - it covers the regressions that are easy to reintroduce:

- browsing the queue must not write label files or mark images done
- an edit must write, and the box label must survive a reload
- boxes must stay inside the image
- undo/redo must restore box state
- switching tasks must not corrupt queue status

`load_test.py` prints the numbers quoted under **Performance notes** so you can
confirm them on your own hardware.

---

## Config location
`~/.snp_labeler/config.json` — telemetry fields, class list, both output directories,
active task, split preferences, duplicate-scan sensitivity, and an optional
`rejected_directory` override.
