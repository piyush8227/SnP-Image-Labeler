"""
SnP Image Labeler - Rejection Manager

"Delete" in this app never destroys an image. A rejected image is MOVED into a
_rejected folder and written into _rejected/rejected_log.csv, so a wrong call
is undone by moving the file back — the log records where it came from.

Layout:

    {rejected_root}/_rejected/{image}
    {rejected_root}/_rejected/rejected_log.csv

The root is, in order of preference:
  1. an explicit override (config: rejected_directory)
  2. the segregation dataset root
  3. the folder the image itself lives in

so a rejected image always lands somewhere the user already knows about.
"""

import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

REJECTED_DIRNAME = "_rejected"
LOG_NAME         = "rejected_log.csv"
LOG_FIELDS       = ["rejected_at", "image_name", "original_path", "moved_to", "reason"]


def rejected_dir(image_path: str, override: str = "", dataset_root: str = "") -> Path:
    """Where this image's rejected copy belongs (the folder is not created)."""
    if override:
        base = Path(override)
        # An override that already points at a _rejected folder is used as-is
        return base if base.name == REJECTED_DIRNAME else base / REJECTED_DIRNAME
    if dataset_root:
        return Path(dataset_root) / REJECTED_DIRNAME
    return Path(image_path).parent / REJECTED_DIRNAME


def _unique_path(folder: Path, name: str) -> Path:
    """A free path inside `folder`, suffixing _1, _2, ... on collision."""
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        candidate = folder / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def move_to_rejected(
    image_path: str,
    override: str = "",
    dataset_root: str = "",
    reason: str = "",
) -> Tuple[bool, str]:
    """
    Move one image into its _rejected folder and log the move.

    Returns (ok, message) — on success the message is the new path, on failure
    the reason it could not be moved.
    """
    source = Path(image_path)
    if not source.is_file():
        return False, f"{source.name}: file no longer exists"

    folder = rejected_dir(image_path, override, dataset_root)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Cannot create {folder}: {exc}"

    dest = _unique_path(folder, source.name)
    try:
        shutil.move(str(source), str(dest))
    except (OSError, shutil.Error) as exc:
        return False, f"{source.name}: {exc}"

    _append_log(folder, source, dest, reason)
    return True, str(dest)


def _append_log(folder: Path, source: Path, dest: Path, reason: str) -> None:
    """Append one row to _rejected/rejected_log.csv (best effort)."""
    log = folder / LOG_NAME
    try:
        is_new = not log.exists()
        with open(log, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow({
                "rejected_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "image_name":    source.name,
                "original_path": str(source),
                "moved_to":      str(dest),
                "reason":        reason,
            })
    except OSError as exc:
        print(f"[RejectionManager] Log write failed: {exc}")


def read_log(folder: str) -> List[Dict[str, Any]]:
    """Rows of a _rejected/rejected_log.csv, newest last. [] when absent."""
    log = Path(folder) / LOG_NAME
    if not log.exists():
        return []
    try:
        with open(log, "r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def restore(moved_to: str, original_path: str) -> Tuple[bool, str]:
    """Move a rejected image back where it came from (used by an undo)."""
    src = Path(moved_to)
    if not src.is_file():
        return False, f"{src.name}: not in the rejected folder any more"
    dest = Path(original_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = _unique_path(dest.parent, dest.name)
        shutil.move(str(src), str(dest))
    except (OSError, shutil.Error) as exc:
        return False, f"{src.name}: {exc}"
    return True, str(dest)
