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
SnP Image Labeler - Duplicate Finder

Finds the same picture stored more than once, whatever the file is called.

Two levels of matching:

  exact    - identical bytes (SHA-256). The plain "copied twice under another
             name" case; free of false positives.
  similar  - visually the same picture after a resize, a re-encode, a crop of a
             few pixels or a quality change. Found with a 64-bit difference
             hash (dHash) compared by Hamming distance, so a threshold of 0
             means "byte-identical only" and a larger threshold accepts more
             drift.

A signature is built in one pass per file (stat -> sha256 -> dHash), so a scan
reads every image exactly once. Grouping is pure data work — no widgets, no
disk access — so it can be exercised without opening a window.
"""

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, qGray

HASH_CHUNK = 1 << 20        # 1 MiB
PHASH_SIDE = 8              # 8 x 8 comparisons -> 64-bit hash
PHASH_BITS = PHASH_SIDE * PHASH_SIDE

# Presets offered by the scan dialog: (label, max Hamming distance, hint)
SENSITIVITY: Tuple[Tuple[str, int, str], ...] = (
    ("Exact copies only",  0,  "Byte-identical files. No false matches."),
    ("Near-identical",     4,  "Also catches re-saves, resizes and quality changes."),
    ("Similar",            8,  "Also catches small crops, borders and light edits."),
    ("Loose",             14,  "Widest net — expect some unrelated matches."),
)
DEFAULT_SENSITIVITY = 1


# ------------------------------------------------------------------ #
#  Hashing                                                             #
# ------------------------------------------------------------------ #

def file_sha256(path: str) -> str:
    """SHA-256 of a file's contents, or "" if it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def perceptual_hash_image(image: QImage) -> Optional[int]:
    """
    64-bit difference hash of an already-loaded image.

    Grey, shrunk to 9x8, then one bit per horizontal neighbour pair: 1 when the
    left pixel is brighter. Scale and file format drop out; the layout of light
    and dark does not.
    """
    if image is None or image.isNull():
        return None
    small = image.convertToFormat(QImage.Format_Grayscale8).scaled(
        PHASH_SIDE + 1, PHASH_SIDE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    )
    if small.isNull() or small.width() < PHASH_SIDE + 1 or small.height() < PHASH_SIDE:
        return None

    bits = 0
    for y in range(PHASH_SIDE):
        for x in range(PHASH_SIDE):
            left  = qGray(small.pixel(x, y))
            right = qGray(small.pixel(x + 1, y))
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def perceptual_hash(path: str) -> Optional[int]:
    """Difference hash of an image file, or None if it cannot be decoded."""
    image = QImage(str(path))
    if image.isNull():
        return None
    return perceptual_hash_image(image)


def hamming(a: int, b: int) -> int:
    """Number of differing bits — 0 means the two hashes are identical."""
    return bin(a ^ b).count("1")


def similarity_percent(distance: int) -> int:
    return int(round((1.0 - distance / float(PHASH_BITS)) * 100))


# ------------------------------------------------------------------ #
#  Signatures                                                          #
# ------------------------------------------------------------------ #

class Signature(NamedTuple):
    """Everything the grouper needs to know about one image."""
    path:       str
    size_bytes: int
    mtime:      float
    sha:        str
    phash:      Optional[int]
    in_dataset: bool = False      # already recorded in processed_images.csv
    note:       str  = ""         # e.g. "Currency_data/train"
    key:        str  = ""         # record key, for dataset entries

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def folder(self) -> str:
        return str(Path(self.path).parent)


def build_signature(
    path: str,
    want_phash: bool = True,
    in_dataset: bool = False,
    note: str = "",
    key: str = "",
    known_sha: str = "",
    known_phash: Optional[int] = None,
    probe_path: str = "",
) -> Optional[Signature]:
    """
    Read one image and describe it. Returns None when the file is unreadable.

    `known_sha` / `known_phash` come from processed_images.csv and skip the
    matching work; `probe_path` is the file to read when the original source is
    gone but a copy inside the dataset survives.
    """
    target = path if os.path.isfile(path) else (probe_path or path)
    try:
        stat = os.stat(target)
    except OSError:
        return None

    sha = known_sha or file_sha256(target)
    phash = known_phash
    if want_phash and phash is None:
        phash = perceptual_hash(target)
    return Signature(
        path=str(path),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        sha=sha,
        phash=phash,
        in_dataset=in_dataset,
        note=note,
        key=key,
    )


# ------------------------------------------------------------------ #
#  Grouping                                                            #
# ------------------------------------------------------------------ #

def _keep_order(sig: Signature):
    """Sort key deciding which copy a group keeps: recorded first, then oldest."""
    return (not sig.in_dataset, sig.mtime, sig.path.lower())


def group_duplicates(
    signatures: List[Signature], threshold: int = 0
) -> List[Dict[str, Any]]:
    """
    Cluster signatures into duplicate groups.

    Identical SHA-256 always groups. With `threshold` > 0, groups whose
    difference hashes are within that many bits are merged as well — compared
    one representative per exact group, so a folder full of literal copies
    costs one comparison, not one per copy.

    Returns [{'kind': 'exact'|'similar', 'distance': int, 'members': [Signature]}]
    with the best copy to keep first in every group's member list.
    """
    count = len(signatures)
    parent = list(range(count))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_sha: Dict[str, List[int]] = {}
    for i, sig in enumerate(signatures):
        if sig.sha:
            by_sha.setdefault(sig.sha, []).append(i)
    for idxs in by_sha.values():
        for other in idxs[1:]:
            union(idxs[0], other)

    if threshold > 0:
        # One representative per exact group: a folder of literal copies costs
        # one comparison, not one per copy.
        reps: Dict[int, int] = {}
        for i, sig in enumerate(signatures):
            if sig.phash is not None:
                reps.setdefault(find(i), i)
        order = sorted(reps.values(), key=lambda i: _keep_order(signatures[i]))

        # Cluster around a seed rather than merging every close pair: chaining
        # a~b and b~c would otherwise drag a and c into one group even when
        # they look nothing alike. Every member here matches the seed itself.
        taken: set = set()
        for seed in order:
            if seed in taken:
                continue
            taken.add(seed)
            for other in order:
                if other in taken:
                    continue
                if hamming(signatures[seed].phash, signatures[other].phash) <= threshold:
                    union(seed, other)
                    taken.add(other)

    buckets: Dict[int, List[int]] = {}
    for i in range(count):
        buckets.setdefault(find(i), []).append(i)

    groups: List[Dict[str, Any]] = []
    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        members = sorted((signatures[i] for i in idxs), key=_keep_order)
        shas = {m.sha for m in members}
        exact = len(shas) == 1 and "" not in shas
        distance = 0
        if not exact:
            anchor = next((m.phash for m in members if m.phash is not None), None)
            distance = max(
                (hamming(anchor, m.phash) for m in members
                 if anchor is not None and m.phash is not None),
                default=0,
            )
        groups.append({
            "kind":     "exact" if exact else "similar",
            "distance": distance,
            "members":  members,
        })

    groups.sort(key=lambda g: (g["kind"] != "exact", -reclaimable_bytes(g)))
    return groups


def reclaimable_bytes(group: Dict[str, Any]) -> int:
    """Bytes freed by keeping only the first member of a group."""
    return sum(m.size_bytes for m in group["members"][1:])


def group_headline(group: Dict[str, Any]) -> str:
    members = group["members"]
    if group["kind"] == "exact":
        what = "Identical copies"
    else:
        what = f"Similar — {similarity_percent(group['distance'])}% match"
    return (f"{what}   ·   {len(members)} files   ·   "
            f"{human_size(reclaimable_bytes(group))} redundant")


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} GB"


def summarise(groups: Iterable[Dict[str, Any]]) -> str:
    groups = list(groups)
    if not groups:
        return "No duplicates found."
    files = sum(len(g["members"]) for g in groups)
    extra = sum(len(g["members"]) - 1 for g in groups)
    waste = sum(reclaimable_bytes(g) for g in groups)
    exact = sum(1 for g in groups if g["kind"] == "exact")
    return (f"{len(groups)} group(s) — {exact} identical, {len(groups) - exact} similar "
            f"— {files} files, {extra} redundant ({human_size(waste)}).")
