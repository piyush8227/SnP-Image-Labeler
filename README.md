<p align="center">
  <img src="assets/logo_wordmark.png" alt="SnP Image Labeler" width="620">
</p>

<p align="center">
  <b>Desktop ML dataset annotation, segregation, and ground-truth preparation tool built with Python and PyQt5.</b>
</p>

---

## Overview

**SnP Image Labeler** is a private portfolio project designed to simplify image dataset preparation for Machine Learning and Computer Vision workflows.

The application combines two common dataset-preparation tasks inside a single desktop interface:

| Mode            | Purpose                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------- |
| **Annotation**  | Create structured image-level metadata and bounding-box annotations with CSV or JSON export |
| **Segregation** | Organize raw images into class-wise Train / Test / Validation datasets                      |

The project focuses on practical ML engineering requirements such as:

* Structured ground-truth generation
* Configurable annotation schemas
* Large image dataset handling
* Dataset segregation
* Duplicate detection
* Review workflows
* Safe file operations
* Session recovery
* Performance-oriented image loading

> **Project status:** Privately developed and maintained as part of my personal ML engineering portfolio.
> The repository is intended to demonstrate software architecture, ML tooling, dataset engineering, and desktop application development.

---

## Why I Built It

Preparing datasets for real-world ML systems often requires more than drawing bounding boxes.

Different ML pipelines may require combinations of:

* categorical metadata
* boolean fields
* textual attributes
* bounding boxes
* structured ground-truth files
* train/test/validation organization
* review states
* duplicate detection

Generic annotation tools do not always fit these workflows cleanly.

SnP Image Labeler was designed around that problem: provide a configurable desktop workflow that helps transform raw image collections into **structured, reviewable, and reproducible ML datasets**.

---

# Core Capabilities

## 1. Image Annotation

The annotation workflow supports configurable image metadata together with bounding-box labeling.

### Features

* Import individual images, multiple images, or complete directories
* Managed label list, defined once and reused across the dataset
* Multiple bounding boxes per image, each independently labelled
* Bounding-box drawing, moving and resizing from any of the four corners
* High-contrast box rendering that stays legible over any photograph
* Undo / Redo
* Autosave
* Existing-label pre-fill
* Review flags
* Review contact sheet
* Zoom and pan
* Session recovery
* CSV and JSON exports

### Annotation Workflow

```text
Import Images
      ↓
Define Labels
      ↓
Review Image
      ↓
Draw Bounding Boxes
      ↓
Assign a Label to Each Box
      ↓
Validate / Flag
      ↓
Save Ground Truth
```

---

## Structured Output

Each image can produce an independent structured annotation file.

### JSON

```json
{
  "image_name": "Image_1.jpg",
  "boxes": [
    {"label": "currency", "left": 12.0, "top": 30.0, "right": 220.0, "bottom": 180.0},
    {"label": "document", "left": 240.0, "top": 60.0, "right": 400.0, "bottom": 210.0}
  ]
}
```

### CSV

The box list is written as a single JSON cell, so the file stays one row per image.

```csv
image_name,boxes
Image_1.jpg,"[{""label"":""currency"",""left"":12.0,""top"":30.0,""right"":220.0,""bottom"":180.0}]"
```

The label set is defined per project, allowing the same application to support
different dataset requirements.

### Reading Files Written by Earlier Versions

Label files that use the older single `currency_country_bounding_box` key still
open, with their box and label intact, in both the JSON and CSV forms. Nothing
on disk is rewritten until that image is saved again, at which point it is
written in the schema above. Field names from an older configuration are
carried over as the starting label list on first run.

---

# 2. Dataset Segregation

The segregation workflow converts a flat image collection into a structured ML dataset.

Example:

```text
Raw Images
│
├── image_001.jpg
├── image_002.jpg
├── image_003.jpg
└── image_004.jpg

            ↓ SnP Image Labeler

Dataset
│
├── Class_A_data/
│   ├── train/
│   ├── test/
│   └── val/
│
└── Class_B_data/
    ├── train/
    ├── test/
    └── val/
```

Images are **copied rather than destructively moved**, preserving the original input dataset.

### Features

* Custom class creation
* Train / Test / Validation assignment
* Multi-class assignment
* Optional automatic split ratios
* Resume partially completed datasets
* Dataset progress tracking
* Existing-assignment detection
* Safe reassignment
* Filename collision handling
* Dataset manifest generation

---

## Dataset Processing Record

Every segregated image is tracked using a structured processing record.

Example fields include:

| Field          | Purpose                              |
| -------------- | ------------------------------------ |
| `image_name`   | Original image filename              |
| `source_path`  | Input image location                 |
| `content_hash` | Exact duplicate identification       |
| `phash`        | Visual similarity detection          |
| `size_bytes`   | File size                            |
| `classes`      | Assigned classes                     |
| `splits`       | Train / Test / Validation assignment |
| `destinations` | Generated dataset locations          |
| `processed_at` | Processing timestamp                 |

This makes dataset construction traceable and resumable.

---

# 3. Duplicate Detection

Large datasets commonly accumulate repeated or near-identical images.

SnP Image Labeler includes dataset-hygiene tooling for identifying these cases.

### Exact Duplicate Detection

Uses **SHA-256 content hashing** to identify files with identical contents even when their filenames differ.

### Visual Similarity Detection

Uses perceptual image hashing to identify visually similar images such as:

* resized copies
* recompressed versions
* screenshots
* slightly modified versions
* renamed duplicates

The application presents duplicate groups for visual review before any action is taken.

---

# 4. Safe Image Rejection

Images that are:

* blurred
* corrupt
* irrelevant
* incorrectly captured
* unsuitable for training

can be removed from the active dataset workflow.

Instead of permanently deleting files, rejected images are moved into a dedicated location with a corresponding record.

```text
_rejected/
├── rejected_image.jpg
└── rejected_log.csv
```

This provides a recoverable dataset-cleaning workflow rather than destructive deletion.

---

# 5. Review Workflow

Annotations can be marked for later inspection.

The application supports:

* Flag for review
* Pending / Completed / Flagged states
* Review-grid visualization
* Existing bounding-box previews
* Queue filters
* Annotation progress
* Session restoration

This makes the tool suitable for iterative labeling rather than one-pass annotation.

---

# Interface

### Theme

The application ships a light (white/black) theme and a dark theme, switchable
from the status bar or with `Ctrl+Shift+D`. Colours are not written into the
stylesheet: `style.qss` is a template of named tokens resolved per theme by
`core/theme.py`, which is also where the painting code -- bounding boxes,
thumbnails, the review grid -- reads its colours, so the two cannot drift apart.

### Interface Scale

The whole interface scales from 75% to 300%, applied live, from the status bar
or `Ctrl+Shift+` `+` / `-`. One setting moves text, padding, borders, thumbnails
and bounding-box handles together. Widget sizes are derived from the scale
rather than hard-coded, so panels grow with their text instead of clipping it.

### Menu Bar

Every command is reachable from the menu bar -- **File**, **Edit**, **View**,
**Task**, **Tools**, **Help** -- regardless of interface scale, because pop-up
menus size themselves to their contents. The toolbar below it carries the
frequently used actions and mirrors the same command definitions, so a command
has a single state wherever it appears.

---

# Engineering Highlights

The project was designed as more than a basic PyQt labeling interface.

### Background Image Loading

Image decoding runs outside the main GUI workflow so loading large images does not unnecessarily block interaction.

### Image Prefetching

Nearby images are prefetched to reduce navigation latency.

### Lazy Thumbnail Loading

Only thumbnails close to the visible queue area are loaded rather than decoding an entire dataset at startup.

### Image Cache

A byte-budgeted LRU cache controls memory consumption when working with high-resolution images.

### Atomic File Writes

Important configuration and dataset files use a temporary-write-and-replace strategy to reduce the risk of corrupted output.

### Session Recovery

The application stores enough session state to restore:

* working queue
* current image
* active task
* application configuration

after restarting.

### Indexed Label State

Label status is indexed instead of repeatedly performing filesystem checks for every image.

---

# Performance

The application has been tested with large synthetic workloads to ensure that the user interface remains responsive as dataset size increases.

Representative benchmark scenarios include:

| Scenario                         | Design                               |
| -------------------------------- | ------------------------------------ |
| High-resolution image navigation | Background decoding + image prefetch |
| Large queues                     | Lazy thumbnail loading               |
| Thumbnail reuse                  | Persistent thumbnail cache           |
| Full-resolution images           | Byte-budgeted LRU cache              |
| Annotation-state lookup          | In-memory index                      |
| Queue status updates             | Constant-time dictionary lookup      |

The repository includes a dedicated load test for reproducing these measurements on different machines.

Performance values depend on:

* storage speed
* CPU
* image dimensions
* image compression
* available memory

and should therefore be interpreted as implementation benchmarks rather than hardware-independent guarantees.

---

# Application Architecture

```text
                         ┌──────────────────────┐
                         │      PyQt5 UI        │
                         │     Main Window      │
                         └──────────┬───────────┘
                                    │
                  ┌─────────────────┼──────────────────┐
                  │                 │                  │
          Annotation Flow    Segregation Flow    Dataset Hygiene
                  │                 │                  │
                  ▼                 ▼                  ▼
           Label Manager     Segregation Manager  Duplicate Finder
                  │                 │              Rejection Manager
                  │                 │                  │
                  └─────────────────┼──────────────────┘
                                    │
                             Storage Layer
                                    │
                 ┌──────────────────┼───────────────────┐
                 │                  │                   │
           Config Manager      Session Store       Cache Layer
                                              Image / Thumbnail
```

The codebase separates UI widgets from dataset and persistence logic so the core workflow does not depend entirely on the graphical interface.

---

# Project Structure

```text
snp_labeler/
│
├── main.py
├── main_window.py
├── style.qss
│
├── core/
│   ├── config_manager.py
│   ├── duplicate_finder.py
│   ├── image_cache.py
│   ├── label_index.py
│   ├── label_manager.py
│   ├── rejection_manager.py
│   ├── segregation_manager.py
│   ├── session_store.py
│   ├── theme.py
│   ├── thumbnail_cache.py
│   └── undo_stack.py
│
├── widgets/
│   ├── image_viewer.py
│   ├── annotation_viewer.py
│   ├── image_queue.py
│   ├── label_panel.py
│   ├── bbox_panel.py
│   ├── review_grid.py
│   ├── segregation_panel.py
│   ├── duplicate_scan_dialog.py
│   └── class_editor.py
│
├── tests/
│   ├── smoke_test.py
│   └── load_test.py
│
├── tools/
│   ├── make_icons.py
│   └── pick_icon.py
│
├── assets/
│
├── requirements.txt
├── SnP_Image_Labeler.spec
├── runtime_hook.py
├── version_info.txt
├── build_windows.bat
└── build_linux_mac.sh
```

---

# Installation

### Requirements

* Python 3.x
* PyQt5

Clone the repository:

```bash
git clone https://github.com/piyush8227/snp_labeler.git
cd snp_labeler
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

# Running the Application

```bash
python main.py
```

---

# Keyboard Shortcuts

| Shortcut    | Action               |
| ----------- | -------------------- |
| `D` / `→`   | Next image           |
| `A` / `←`   | Previous image       |
| `Ctrl+S`    | Save / Assign        |
| `Ctrl+Z`    | Undo                 |
| `Ctrl+Y`    | Redo                 |
| `Ctrl+1`    | Annotation mode      |
| `Ctrl+2`    | Segregation mode     |
| `Ctrl+O`    | Import images        |
| `N`         | Navigate mode        |
| `B`         | Bounding-box mode    |
| `Del`       | Delete selected box  |
| `Ctrl+F`    | Flag for review      |
| `Ctrl+G`    | Review grid          |
| `Ctrl+L`    | Lock zoom            |
| `Ctrl+D`    | Find duplicates      |
| `Shift+Del` | Reject current image |
| `Enter`     | Assign and continue  |
| `1` - `9`   | Quick label / split  |
| `Ctrl+Shift+D` | Toggle dark theme |
| `Ctrl+Shift+` `+` / `-` | Interface scale up / down |

---

# Testing

The project contains both functional and load-oriented tests.

### Functional / Smoke Tests

```bash
python tests/smoke_test.py
```

These tests cover important behaviors such as:

* browsing must not create empty label files
* edited annotations must persist
* bounding boxes must remain inside image boundaries
* undo / redo must restore annotation state
* switching between tasks must preserve workflow state

### Load Tests

```bash
python tests/load_test.py
```

The load test evaluates scenarios including:

* high-resolution images
* large queue sizes
* navigation latency
* image-cache behavior
* thumbnail loading
* annotation-state lookup performance

---

# Configuration

Application configuration is stored locally under:

```text
~/.snp_labeler/
```

Configuration includes application preferences such as:

* annotation fields
* class definitions
* output locations
* active workflow
* split preferences
* duplicate-detection sensitivity
* cache settings

No dataset images are stored inside the application configuration directory.

---

# Technology Stack

### Application

* Python
* PyQt5

### ML Dataset Tooling

* Bounding-box annotation
* Structured metadata
* Dataset segregation
* Ground-truth generation
* Dataset review workflows

### Data Integrity

* SHA-256 hashing
* Perceptual image hashing
* Atomic filesystem operations
* Processing manifests
* Session persistence

### Performance

* Background image decoding
* Lazy loading
* Image prefetching
* Thumbnail caching
* LRU caching

---

# Project Goals

SnP Image Labeler is intended to demonstrate practical engineering around the parts of Machine Learning that exist **outside model training itself**:

```text
Raw Data
   ↓
Dataset Preparation
   ↓
Annotation
   ↓
Dataset Validation
   ↓
Ground Truth
   ↓
Model Evaluation / Training
```

Reliable ML systems depend heavily on the quality and traceability of this layer.

---

## Private Project Notice

This repository represents a **privately developed portfolio project** and is maintained primarily to demonstrate my approach to:

* ML dataset engineering
* desktop application development
* data annotation workflows
* software reliability
* performance optimization
* maintainable Python systems

Any examples, filenames, labels, schemas, or workflows shown in the documentation are intended solely to demonstrate the application's technical capabilities.

---

## License

This project is licensed under the **GNU General Public License v3.0 or later**.
The full text is in [LICENSE](LICENSE).

GPL-3.0 is not an arbitrary choice here. The application is built on **PyQt5**,
which Riverbank Computing dual-licenses under the GNU GPL v3 and a commercial
licence, and which -- unlike Qt itself -- is *not* available under the LGPL.
Releasing a PyQt5 application publicly under a permissive licence such as MIT
would advertise freedoms that cannot be granted without a commercial Riverbank
licence, so the project adopts the licence its dependency already requires.

In short: you are free to use, study, modify and redistribute this software,
provided derivative works remain under the GPL-3.0 and carry the same notice.

---

## Author

**Piyush More**
Machine Learning Engineer

[LinkedIn](https://www.linkedin.com/in/piyushm9034/) • [GitHub](https://github.com/piyush8227)
