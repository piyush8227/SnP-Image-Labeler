"""
SnP Image Labeler - Segregation Manager

Class-wise dataset segregation with a resumable, human-readable record.

Folder layout produced under the dataset (output) root:

    {root}/processed_images.csv        <- the record of every processed image
    {root}/{Class}_data/{split}/{image}

    e.g.  Currency_data/train/Image_1.jpg
          Currency_data/test/Image_2.jpg

Behaviour:
  - Images are COPIED (shutil.copy2); the source folder is never modified.
  - An image may belong to several classes, each with its own train/test/val split.
  - Directories are created only if missing (mkdir parents=True, exist_ok=True).
  - processed_images.csv is the single source of truth: one row per image, so a
    part-finished dataset resumes cleanly and the same image is never processed
    twice by accident. An older .snp_segregation/manifest.json is migrated on
    first load.

Duplicate detection (see find_record):
  1. same source path            -> match
  2. same file contents (SHA-256) -> match, even under a different name or folder
  Content hashing only runs when a recorded image has the same byte size, and a
  size index finds those candidates without scanning the record, so importing a
  folder of new images costs no hashing at all.

  Content matching runs at ASSIGN time, where catching a renamed copy matters.
  The status queries (is_done / assignments / destinations) use lookup(), which
  is a path-only dict hit and touches no files: they run once per queue row on
  import and several times per key press while navigating, so a stat plus a
  possible full file read per call made a 10,000 image folder unusable.

Record writes:
  assign() APPENDS its row (O(1)); _read_csv keys rows by source path and keeps
  the last one, so an appended row supersedes an earlier one for the same image.
  The file is compacted by a full rewrite every APPEND_BUDGET rows, whenever a
  row is removed, and on flush(). Rewriting all rows per assign was ~34 ms of a
  42 ms assign at 6,000 rows, against 0.5 ms to actually copy the image.
"""

import csv
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Canonical split identifiers (also the sub-directory names on disk)
SPLITS: Tuple[str, ...] = ("train", "test", "val")

SPLIT_LABELS: Dict[str, str] = {
    "train": "Train",
    "test":  "Test",
    "val":   "Validation",
}

# Multi-value separator inside a CSV cell. Safe: sanitize_class_name strips "|",
# and it cannot appear in a Windows path.
MULTI_SEP = "|"

_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_class_name(name: str) -> str:
    """Make a user-typed class name safe for use as a directory name."""
    cleaned = _INVALID_PATH_CHARS.sub("", (name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned


def class_folder_name(name: str) -> str:
    """'currency' -> 'Currency_data'  (first letter capitalised, rest as typed)."""
    cleaned = sanitize_class_name(name)
    if not cleaned:
        return ""
    return f"{cleaned[:1].upper()}{cleaned[1:]}_data"


class SegregationManager:
    """Copies images into {Class}_data/{split}/ and records what was processed."""

    RECORD_NAME     = "processed_images.csv"
    RECORD_BACKUP   = ".processed_images.backup.csv"
    LEGACY_DIRNAME  = ".snp_segregation"       # pre-CSV manifest location
    LEGACY_NAME     = "manifest.json"

    FIELDNAMES = [
        "image_name", "source_path", "content_hash", "phash", "size_bytes",
        "classes", "splits", "destinations", "processed_at",
    ]

    HASH_CHUNK = 1 << 20      # 1 MiB

    # A full rewrite of the record costs O(rows); an append costs O(1). Rows are
    # appended until this many have piled up, then the file is compacted once.
    APPEND_BUDGET = 400

    def __init__(self, root: str):
        self._root: Path = Path(root) if root else Path.home()
        self._records: Dict[str, Dict[str, Any]] = {}   # key -> record
        self._hash_memo: Dict[Tuple[str, int, float], str] = {}
        # size -> [key, ...]. Content-duplicate candidates in O(1) instead of a
        # linear scan of every record on every lookup.
        self._by_size: Dict[int, List[str]] = {}
        self._counts_cache: Optional[Dict[str, Dict[str, int]]] = None
        self._appended = 0                  # rows appended since last compaction
        self._load()
        self._reindex()

    # ------------------------------------------------------------------ #
    #  Root                                                                #
    # ------------------------------------------------------------------ #

    @property
    def root(self) -> str:
        return str(self._root)

    def set_root(self, path: str) -> None:
        """Point at a different dataset root and load that root's record."""
        self.flush()                        # don't lose pending rows from the old root
        self._root = Path(path)
        self._records = {}
        self._load()
        self._reindex()

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def record_path(self) -> Path:
        return self._root / self.RECORD_NAME

    # ------------------------------------------------------------------ #
    #  Path helpers                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _key(image_path: str) -> str:
        """Record key: normalised absolute source path (case-insensitive on Windows)."""
        return os.path.normcase(os.path.abspath(str(image_path)))

    def destination_dir(self, class_name: str, split: str) -> Path:
        """{root}/{Class}_data/{split}"""
        return self._root / class_folder_name(class_name) / split

    def _rel(self, path: Path) -> str:
        """Store destinations relative to the root so the dataset stays portable."""
        try:
            return str(path.relative_to(self._root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _abs(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self._root / p)

    # ------------------------------------------------------------------ #
    #  Record index + mutation                                            #
    # ------------------------------------------------------------------ #
    #
    # Every write to self._records goes through _put / _drop so the size index
    # and the counts cache can never drift out of step with the records.

    def _reindex(self) -> None:
        """Rebuild the size index and drop cached counts."""
        self._by_size = {}
        for key, rec in self._records.items():
            self._by_size.setdefault(int(rec.get("size_bytes") or 0), []).append(key)
        self._counts_cache = None

    def _put(self, key: str, record: Dict[str, Any]) -> None:
        """Insert or replace a record, keeping the size index in step."""
        self._drop(key)
        self._records[key] = record
        self._by_size.setdefault(int(record.get("size_bytes") or 0), []).append(key)
        self._counts_cache = None

    def _drop(self, key: str) -> Optional[Dict[str, Any]]:
        """Remove a record (if present) and un-index it."""
        rec = self._records.pop(key, None)
        if rec is None:
            return None
        bucket = self._by_size.get(int(rec.get("size_bytes") or 0))
        if bucket:
            try:
                bucket.remove(key)
            except ValueError:
                pass
            if not bucket:
                self._by_size.pop(int(rec.get("size_bytes") or 0), None)
        self._counts_cache = None
        return rec

    def _touch(self) -> None:
        """A record's contents changed in place; counts may no longer hold."""
        self._counts_cache = None

    # ------------------------------------------------------------------ #
    #  Hashing                                                            #
    # ------------------------------------------------------------------ #

    def _hash_file(self, path: Path) -> str:
        """SHA-256 of a file's contents, memoised per (path, size, mtime)."""
        try:
            stat = path.stat()
        except OSError:
            return ""
        memo_key = (os.path.normcase(str(path)), stat.st_size, round(stat.st_mtime, 3))
        if memo_key in self._hash_memo:
            return self._hash_memo[memo_key]
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(self.HASH_CHUNK), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        value = digest.hexdigest()
        self._hash_memo[memo_key] = value
        return value

    def _record_hash(self, record: Dict[str, Any]) -> str:
        """
        Hash stored on a record. Rows migrated from the old manifest have none;
        for those the hash is derived from a copy that is already in the dataset
        (identical content) or from the source, then cached back into the record.
        """
        if record.get("content_hash"):
            return record["content_hash"]

        candidates = [self._abs(d) for d in record.get("destinations", [])]
        source = record.get("source_path", "")
        if source:
            candidates.append(Path(source))
        for cand in candidates:
            if cand.is_file():
                value = self._hash_file(cand)
                if value:
                    record["content_hash"] = value
                    if not record.get("size_bytes"):
                        try:
                            record["size_bytes"] = cand.stat().st_size
                        except OSError:
                            pass
                    return value
        return ""

    # ------------------------------------------------------------------ #
    #  Record I/O                                                          #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        """Load processed_images.csv, its backup, or migrate the legacy manifest."""
        for candidate in (self.record_path, self._root / self.RECORD_BACKUP):
            if candidate.exists():
                try:
                    self._records = self._read_csv(candidate)
                    return
                except (OSError, csv.Error, ValueError):
                    continue

        legacy = self._root / self.LEGACY_DIRNAME / self.LEGACY_NAME
        if legacy.exists():
            migrated = self._read_legacy(legacy)
            if migrated:
                self._records = migrated
                self.save()          # write the CSV the user asked for
                return
        self._records = {}

    def _read_csv(self, path: Path) -> Dict[str, Dict[str, Any]]:
        records: Dict[str, Dict[str, Any]] = {}
        with open(path, "r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                source = (row.get("source_path") or "").strip()
                if not source:
                    continue
                classes = self._split_cell(row.get("classes"))
                splits  = self._split_cell(row.get("splits"))
                dests   = self._split_cell(row.get("destinations"))
                if not classes:
                    continue
                # Pad defensively so a hand-edited file cannot desynchronise
                while len(splits) < len(classes):
                    splits.append(SPLITS[0])
                while len(dests) < len(classes):
                    dests.append("")
                try:
                    size = int(row.get("size_bytes") or 0)
                except ValueError:
                    size = 0
                records[self._key(source)] = {
                    "image_name":   row.get("image_name") or Path(source).name,
                    "source_path":  source,
                    "content_hash": (row.get("content_hash") or "").strip(),
                    "phash":        (row.get("phash") or "").strip(),
                    "size_bytes":   size,
                    "classes":      classes,
                    "splits":       splits,
                    "destinations": dests,
                    "processed_at": row.get("processed_at") or "",
                }
        return records

    def _read_legacy(self, path: Path) -> Dict[str, Dict[str, Any]]:
        """One-time import of the old .snp_segregation/manifest.json."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        records: Dict[str, Dict[str, Any]] = {}
        for entry in (data.get("entries") or {}).values():
            if not isinstance(entry, dict):
                continue
            source = entry.get("source", "")
            items  = entry.get("assignments", []) or []
            if not source or not items:
                continue
            records[self._key(source)] = {
                "image_name":   entry.get("file") or Path(source).name,
                "source_path":  source,
                "content_hash": "",                     # filled in on demand
                "phash":        "",                     # filled in on demand
                "size_bytes":   entry.get("size") or 0,
                "classes":      [i.get("class", "")  for i in items],
                "splits":       [i.get("split", "")  for i in items],
                "destinations": [i.get("dest", "")   for i in items],
                "processed_at": entry.get("updated", ""),
            }
        return records

    @staticmethod
    def _split_cell(value: Optional[str]) -> List[str]:
        if not value:
            return []
        return [part for part in str(value).split(MULTI_SEP) if part != ""]

    def _row_of(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """One record as the flat dict the CSV writer wants."""
        return {
            "image_name":   rec.get("image_name", ""),
            "source_path":  rec.get("source_path", ""),
            "content_hash": rec.get("content_hash", ""),
            "phash":        rec.get("phash", ""),
            "size_bytes":   rec.get("size_bytes", 0),
            "classes":      MULTI_SEP.join(rec.get("classes", [])),
            "splits":       MULTI_SEP.join(rec.get("splits", [])),
            "destinations": MULTI_SEP.join(rec.get("destinations", [])),
            "processed_at": rec.get("processed_at", ""),
        }

    def record_row(self, key: str) -> bool:
        """
        Persist ONE record by appending it to the CSV - O(1) regardless of how
        many images are already in the dataset.

        Rewriting the whole file on every assign made sorting 10,000 images
        quadratic: at 6,000 rows the rewrite was ~34 ms of a 42 ms assign while
        the image copy itself took 0.5 ms. _read_csv keys rows by source path
        and takes the last one it sees, so an appended row correctly supersedes
        an earlier one for the same image. Repeated re-assigns would still grow
        the file, so every APPEND_BUDGET rows it is compacted by a full rewrite.

        Falls back to a full write when there is no file to append to yet.
        """
        rec = self._records.get(key)
        if rec is None:
            return self.save()
        if self._appended >= self.APPEND_BUDGET:
            return self.save()

        try:
            self.ensure_root()
        except OSError as exc:
            print(f"[SegregationManager] Cannot create dataset root: {exc}")
            return False

        dest = self.record_path
        try:
            fresh = not dest.exists() or dest.stat().st_size == 0
        except OSError:
            fresh = True
        if fresh:
            return self.save()

        try:
            with open(dest, "a", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=self.FIELDNAMES).writerow(self._row_of(rec))
            self._appended += 1
            return True
        except OSError as exc:
            print(f"[SegregationManager] Record append failed: {exc}")
            return self.save()

    def flush(self) -> bool:
        """
        Compact the record if rows have been appended since the last full write.
        Called at the durability points: closing the app, switching task or
        dataset root, and before anything that re-reads the file.
        """
        if self._appended:
            return self.save()
        return True

    def save(self) -> bool:
        """Atomically write the whole record (temp -> rename -> mirror to backup)."""
        try:
            self.ensure_root()
        except OSError as exc:
            print(f"[SegregationManager] Cannot create dataset root: {exc}")
            return False

        dest = self.record_path
        tmp  = dest.with_suffix(".tmp")
        rows = sorted(self._records.values(), key=lambda r: r.get("image_name", "").lower())
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=self.FIELDNAMES)
                writer.writeheader()
                for rec in rows:
                    writer.writerow(self._row_of(rec))
            tmp.replace(dest)
            shutil.copy2(dest, self._root / self.RECORD_BACKUP)
            self._appended = 0
            return True
        except OSError as exc:
            print(f"[SegregationManager] Record save failed: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # ------------------------------------------------------------------ #
    #  Duplicate lookup                                                    #
    # ------------------------------------------------------------------ #

    def lookup(self, image_path: str) -> Optional[Dict[str, Any]]:
        """
        Path-only record lookup: one dict hit, no stat, no hashing, no disk.

        This is what the status queries use. They run once per queue row on
        import and three times per key press while navigating, so they must not
        touch the filesystem - on a 10,000 image folder the content-match
        fallback was costing a stat plus a possible full file read per row.
        """
        key = self._key(image_path)
        rec = self._records.get(key)
        if rec is None:
            return None
        rec["key"] = key
        rec["matched_by"] = "path"
        return rec

    def find_record(self, image_path: str,
                    content_match: bool = True) -> Optional[Dict[str, Any]]:
        """
        Return the record for this image, or None.

        Adds two keys to the returned dict (a live reference into the store):
          key        - the record's key in the store
          matched_by - "path" or "content"

        With content_match (the default) a renamed or re-downloaded copy of an
        already-processed image is still recognised by its contents. That costs
        disk reads, so callers on the interactive path pass content_match=False
        or use lookup() instead.
        """
        rec = self.lookup(image_path)
        if rec is not None or not content_match:
            return rec

        source = Path(image_path)
        try:
            size = source.stat().st_size
        except OSError:
            return None

        # Only files whose size matches an existing row can be content
        # duplicates, and the size index finds those without scanning.
        candidates = [
            (k, self._records[k])
            for k in self._by_size.get(size, ())
            if k in self._records
        ]
        if not candidates:
            return None

        digest = self._hash_file(source)
        if not digest:
            return None
        for k, rec in candidates:
            if self._record_hash(rec) == digest:
                rec["key"] = k
                rec["matched_by"] = "content"
                return rec
        return None

    # ------------------------------------------------------------------ #
    #  Duplicate-scan signatures                                           #
    # ------------------------------------------------------------------ #

    def signatures(self) -> List[Dict[str, Any]]:
        """
        One entry per recorded image, in the shape the duplicate scanner wants.

        `probe_path` is the file to read when the original source is gone: a
        copy inside the dataset has identical contents, so hashing it gives the
        same answer. Rows written before the phash column existed report None
        and are hashed by the scan (which caches the result back with
        remember_phashes).
        """
        out: List[Dict[str, Any]] = []
        for key, rec in self._records.items():
            dests = [self._abs(d) for d in rec.get("destinations", []) if d]
            probe = next((str(d) for d in dests if d.is_file()), "")
            source = rec.get("source_path", "")
            if not source and not probe:
                continue
            try:
                phash = int(rec["phash"]) if rec.get("phash") else None
            except (TypeError, ValueError):
                phash = None
            out.append({
                "key":         key,
                "path":        source or probe,
                "probe_path":  probe,
                "sha":         rec.get("content_hash", ""),
                "phash":       phash,
                "note":        self.summary_of(rec),
            })
        return out

    @staticmethod
    def summary_of(record: Dict[str, Any]) -> str:
        """'Currency_data/train, Note_data/test' for one record."""
        return ", ".join(
            f"{class_folder_name(c)}/{s}"
            for c, s in zip(record.get("classes", []), record.get("splits", []))
            if c and s
        )

    def remember_phashes(self, values: Dict[str, int]) -> None:
        """Cache scan-computed hashes on their records so the next scan is fast."""
        changed = False
        for key, value in (values or {}).items():
            rec = self._records.get(key)
            if rec is not None and value is not None and not rec.get("phash"):
                rec["phash"] = str(value)
                changed = True
        if changed:
            self._touch()
            self.save()

    def forget(self, image_path: str) -> bool:
        """Drop the record for an image without touching any file on disk."""
        rec = self.find_record(image_path)
        if not rec:
            return False
        self._drop(rec.get("key", self._key(image_path)))
        self.save()
        return True

    # ------------------------------------------------------------------ #
    #  Status queries                                                      #
    # ------------------------------------------------------------------ #

    def is_done(self, image_path: str) -> bool:
        return self.lookup(image_path) is not None

    def assignments(self, image_path: str) -> List[Dict[str, str]]:
        """[{'class': 'currency', 'split': 'train'}, ...] for this image."""
        rec = self.lookup(image_path)
        if not rec:
            return []
        return [
            {"class": c, "split": s}
            for c, s in zip(rec.get("classes", []), rec.get("splits", []))
            if c and s
        ]

    def destinations(self, image_path: str) -> List[str]:
        rec = self.lookup(image_path)
        if not rec:
            return []
        return [str(self._abs(d)) for d in rec.get("destinations", []) if d]

    def processed_at(self, image_path: str) -> str:
        rec = self.lookup(image_path)
        return rec.get("processed_at", "") if rec else ""

    def done_count(self) -> int:
        return len(self._records)

    def class_counts(self, class_name: str) -> Dict[str, int]:
        """{'train': n, 'test': n, 'val': n} for one class."""
        cached = self.all_counts().get(class_name)
        return dict(cached) if cached else {s: 0 for s in SPLITS}

    def all_counts(self) -> Dict[str, Dict[str, int]]:
        """
        {class: {'train': n, 'test': n, 'val': n}} across the whole record.

        Cached: the segregation panel asks for this on every navigation, and
        walking every record per key press is wasted work when nothing changed.
        The cache is dropped by _put / _drop / _touch on any record change.
        """
        if self._counts_cache is not None:
            return self._counts_cache

        result: Dict[str, Dict[str, int]] = {}
        for rec in self._records.values():
            for cls, split in zip(rec.get("classes", []), rec.get("splits", [])):
                if not cls or split not in SPLITS:
                    continue
                result.setdefault(cls, {s: 0 for s in SPLITS})[split] += 1
        self._counts_cache = result
        return result

    def known_classes(self) -> List[str]:
        return sorted(self.all_counts().keys(), key=str.lower)

    def suggest_split(self, class_name: str, ratios: Dict[str, float]) -> str:
        """
        Auto-ratio pick: the split furthest below its target share *for this class*.
        Splits with a zero ratio are never chosen.
        """
        counts = self.class_counts(class_name)
        total  = sum(counts.values()) + 1          # counting the image about to be added
        best, best_deficit = None, None
        for split in SPLITS:
            share = float(ratios.get(split, 0) or 0)
            if share <= 0:
                continue
            deficit = (share / 100.0) * total - counts.get(split, 0)
            if best_deficit is None or deficit > best_deficit:
                best, best_deficit = split, deficit
        return best or "train"

    # ------------------------------------------------------------------ #
    #  Verification / self-healing                                         #
    # ------------------------------------------------------------------ #

    def verify(self) -> int:
        """
        Drop record entries whose copied files no longer exist on disk (deleted
        by hand), so those images go back to Pending. Returns rows changed.
        """
        changed = 0
        for key in list(self._records.keys()):
            rec   = self._records[key]
            trio  = list(zip(rec.get("classes", []), rec.get("splits", []),
                             rec.get("destinations", [])))
            alive = [t for t in trio if t[2] and self._abs(t[2]).exists()]
            if len(alive) != len(trio):
                changed += 1
                if alive:
                    rec["classes"]      = [t[0] for t in alive]
                    rec["splits"]       = [t[1] for t in alive]
                    rec["destinations"] = [t[2] for t in alive]
                    self._touch()
                else:
                    self._drop(key)
        if changed:
            self.save()
        return changed

    # ------------------------------------------------------------------ #
    #  Assign                                                              #
    # ------------------------------------------------------------------ #

    def _unique_destination(self, dest_dir: Path, source: Path) -> Path:
        """
        Resolve a non-clobbering destination path.
        A file of the same name AND size is treated as this same image
        (idempotent re-run / lost record); a different file gets _1, _2, ...
        """
        try:
            src_size = source.stat().st_size
        except OSError:
            src_size = -1

        dest = dest_dir / source.name
        if not dest.exists():
            return dest
        try:
            if dest.stat().st_size == src_size:
                return dest
        except OSError:
            pass

        stem, suffix = source.stem, source.suffix
        i = 1
        while True:
            candidate = dest_dir / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                return candidate
            try:
                if candidate.stat().st_size == src_size:
                    return candidate
            except OSError:
                pass
            i += 1

    def assign(
        self, image_path: str, assignments: List[Dict[str, str]]
    ) -> Tuple[bool, str]:
        """
        Copy the image into {Class}_data/{split}/ for every (class, split) pair.

        Re-assigning an image (including one matched by content under another
        name) reuses copies that are still wanted and deletes the rest, so
        changing your mind never leaves duplicates behind.
        """
        source = Path(image_path)
        if not source.is_file():
            return False, f"Source image not found: {source.name}"

        # Normalise + de-duplicate the requested pairs
        wanted: List[Tuple[str, str]] = []
        seen = set()
        for item in assignments or []:
            cls   = (item.get("class") or "").strip()
            split = (item.get("split") or "").strip().lower()
            if not sanitize_class_name(cls) or split not in SPLITS:
                continue
            pair = (cls, split)
            if pair not in seen:
                seen.add(pair)
                wanted.append(pair)
        if not wanted:
            return False, "Select at least one class (with a split) first."

        previous = self.find_record(image_path)
        keep: Dict[Tuple[str, str], str] = {}
        stale: List[Path] = []
        if previous:
            for cls, split, dest in zip(previous.get("classes", []),
                                        previous.get("splits", []),
                                        previous.get("destinations", [])):
                abs_dest = self._abs(dest) if dest else None
                if (cls, split) in seen and abs_dest and abs_dest.exists():
                    keep[(cls, split)] = dest
                elif abs_dest:
                    stale.append(abs_dest)

        dests:   List[str]  = []
        written: List[Path] = []
        try:
            self.ensure_root()
            for pair in wanted:
                if pair in keep:
                    dests.append(keep[pair])
                    continue
                cls, split = pair
                dest_dir = self.destination_dir(cls, split)
                dest_dir.mkdir(parents=True, exist_ok=True)   # created only if missing
                dest = self._unique_destination(dest_dir, source)
                shutil.copy2(source, dest)
                written.append(dest)
                dests.append(self._rel(dest))
        except (OSError, shutil.Error) as exc:
            for path in written:                              # roll back partial work
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False, f"Copy failed: {exc}"

        # Only now remove copies this image no longer needs
        for path in stale:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

        # A content match under a different path replaces the old row. That is a
        # removal, which an append cannot express, so the file is compacted.
        replaced = False
        if previous and previous.get("key") and previous["key"] != self._key(image_path):
            self._drop(previous["key"])
            replaced = True

        try:
            size = source.stat().st_size
        except OSError:
            size = 0

        # phash is deliberately left blank: computing it here decoded the image
        # on the GUI thread (~4 ms per assign). Find Duplicates fills it in and
        # caches it back via remember_phashes, which is what it already did for
        # rows written before the column existed.
        key = self._key(image_path)
        self._put(key, {
            "image_name":   source.name,
            "source_path":  str(source),
            "content_hash": self._hash_file(source),
            "phash":        "",
            "size_bytes":   size,
            "classes":      [c for c, _ in wanted],
            "splits":       [s for _, s in wanted],
            "destinations": dests,
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        if replaced:
            self.save()
        else:
            self.record_row(key)

        summary = ", ".join(f"{class_folder_name(c)}/{s}" for c, s in wanted)
        return True, summary

    # ------------------------------------------------------------------ #
    #  Unassign                                                            #
    # ------------------------------------------------------------------ #

    def unassign(self, image_path: str) -> Tuple[bool, str]:
        """Delete every copy made for this image and remove it from the record."""
        rec = self.find_record(image_path)
        if not rec:
            return False, "This image has not been processed yet."

        removed = 0
        for dest in rec.get("destinations", []):
            path = self._abs(dest)
            try:
                if path.exists():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                return False, f"Could not remove {path.name}: {exc}"

        self._drop(rec.get("key", self._key(image_path)))
        self.save()
        return True, f"Removed {removed} copy(ies) and cleared the record entry."
