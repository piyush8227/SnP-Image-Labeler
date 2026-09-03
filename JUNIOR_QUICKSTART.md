# SnP Image Labeler — Quick Start

You have been given a zip of **SnP_Image_Labeler**. No Python, no installation, no admin
rights needed.

---

## 1. Set it up (once, 2 minutes)

1. **Unzip the folder to your local disk** — e.g. `C:\Tools\SnP_Image_Labeler`.
   Do **not** run it from a network drive or straight out of the zip: it will be painfully
   slow, and settings may not save.
2. Keep `SnP_Image_Labeler.exe` and the `_internal` folder **together**. Moving the EXE out
   on its own breaks it. Make a shortcut instead if you want it on the Desktop.
3. Double-click `SnP_Image_Labeler.exe`.
   Windows may show *"Windows protected your PC"* — that is only because the app is not
   code-signed. Click **More info → Run anyway**. It appears once.
4. If a `config.json` came with the zip, copy it to `%USERPROFILE%\.snp_labeler\` **before**
   the first launch (create that folder if it does not exist). That gives you the same class
   list and telemetry fields as everyone else — do not invent your own class names.

---

## 2. The two tasks

Switch with the toolbar (`Task: Annotation | Segregation`) or `Ctrl+1` / `Ctrl+2`.

| Task | What you produce |
|---|---|
| **Annotation** | One label file per image (CSV or JSON): telemetry fields + one bounding box |
| **Segregation** | Images sorted into class folders — `{Class}_data/train\|test\|val/` |

---

## 3. Segregation — the normal day

1. **Dataset Dir** → pick **your own** dataset root (see the warning in §6). This is where
   the class folders and `processed_images.csv` are created.
2. **Import** → Select Folder → your assigned batch of images.
3. **Ctrl+D → Scan** *before you start* — see §4.
4. For each image: tick the class(es), pick Train / Test / Val (`1` / `2` / `3`), press
   **Enter** to assign and jump to the next unsorted image.
5. Your progress is saved continuously in `processed_images.csv`. Close the app whenever you
   like — reopen it, point at the same dataset root, and already-sorted images show
   **✓ Sorted**. The **Pending** filter hides them so you only see what is left.

An image can go into several classes at once, each with its own split.

---

## 4. Before you start a batch: find the duplicates (`Ctrl+D`)

Datasets get polluted by the same photo arriving twice under different names. The scanner
catches that.

- **Match: Exact copies only** — identical files. Always safe.
- **Near-identical / Similar / Loose** — also catches resized, re-saved or lightly cropped
  copies of the same picture.
- Leave **"Also compare with already-sorted images"** ticked — it catches a picture you
  already sorted last week coming back under a new name.

Results are grouped, one group per picture. **The first file in each group is the one being
kept** (already-sorted, else oldest) and the extras are already ticked for you. Click a group
to compare the images side by side before you act — for "Similar" matches, trust your eyes,
not the percentage. Double-click a row to jump to that image in the queue.

Then either:

- **Remove Ticked from Queue** — only takes them out of today's list. No file is touched.
- **Move Ticked to `_rejected`** — moves the files out of the working set (see §5).

If you tick *every* copy in a group, the app warns you first — never throw away all versions
of a picture.

---

## 5. An image that does not belong: `Shift+Del`

Blurred, wrong subject, corrupt, unusable. Press **Shift+Del**, use the **Delete Image**
toolbar button, or right-click the image in the queue → **Delete Image…**

**Never delete images in File Explorer.** Doing that leaves the copies inside the class
folders and a stale row in `processed_images.csv`, and the counts stop matching reality.

What the app does instead, in one step:

- moves the file into `{dataset root}\_rejected\`;
- deletes any copies already made into the class folders and clears its row from
  `processed_images.csv`;
- offers to delete the label file too, if you had annotated it;
- takes it out of the queue and moves on.

**Nothing is destroyed.** `_rejected\rejected_log.csv` records the original path of every
image, so a mistake is undone by moving the file back. `_rejected` folders are skipped when
importing, so rejected images do not come back on your next import.

---

## 6. Rules for working as a team

**One dataset root per person.** Never point two people at the same dataset root — not even
on a shared drive. `processed_images.csv` is rewritten in full every time you assign an
image, so two people working on one root silently erase each other's progress. Your lead
merges everyone's folders at the end.

- Sort only the batch you were assigned; do not re-import somebody else's images.
- Use the class names that came with the config. If you think one is missing, ask — do not
  add it yourself, or the merged dataset ends up with `Note_data` and `Notes_data`.
- **Open Record** in the toolbar opens your `processed_images.csv` — that, plus the class
  folders and `_rejected`, is what you hand in.
- Hand in the **whole dataset root folder**, including `processed_images.csv` and
  `_rejected`.

---

## 7. Keyboard shortcuts

The four you will use all day:

| Key | Action |
|---|---|
| `D` | **Next image** |
| `A` | **Previous image** |
| `Ctrl+S` | **Save** |
| `Ctrl+Z` | **Undo** (`Ctrl+Y` redoes) |

Everything else:

| Key | Action |
|---|---|
| `←` / `→` | Previous / Next image (same as `A` / `D`) |
| `Ctrl+1` / `Ctrl+2` | Annotation / Segregation task |
| `Ctrl+O` | Import images |
| `N` / `B` | Navigate / Annotate (Box) mode *(annotation)* |
| `Del` | Delete the selected box *(annotation)* |
| `Alt+←↑→↓` | Nudge the box 1 px (`Shift` too = 10 px) |
| `Space` (hold) or middle-mouse | Pan the image, even while annotating |
| `1` … `9` | Quick label *(annotation)* / Train, Test, Val *(segregation)* |
| `Ctrl+F` | Flag this image for review (come back to it later) |
| `Ctrl+G` | Review Grid — see every image and its box at once |
| `Ctrl+L` | Lock zoom — the next image keeps this zoom and pan |
| `Enter` | Assign and go to the next image *(segregation)* |
| `Ctrl+D` | Find duplicate / near-identical images |
| `Shift+Del` | Delete current image (move to `_rejected`) |
| `F` / `R` | Fit to window / Reload image |
| `Ctrl++` / `Ctrl+-` / `Ctrl+0` | Zoom in / out / 100% |

Typing in a text box? Letter keys go into the box, not to the shortcuts.

---

## 8. If something goes wrong

- **The app will not start / closes instantly** — check `snp_crash.log` next to the EXE and
  send it to your lead.
- **"Windows protected your PC"** — More info → Run anyway (§1).
- **An image you sorted is back as Pending** — its copy was deleted or moved outside the app.
  Sort it again.
- **Your settings vanished** — they live in `%USERPROFILE%\.snp_labeler\config.json`. It is
  per-user, so it does not follow you to another machine.
