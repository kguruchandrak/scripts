#!/usr/bin/env python3
"""
transfer_nested_archive_files.py

Reads an input CSV with (at minimum) PathToZipFile, GUID, caseName, fileName
columns. For each row:
  1. Resolves PathToZipFile to either:
       a) the longest real FILE prefix along the path (normal case: the
          real file, typically an archive, with possibly more literal path
          after it), or
       b) if no file-prefix exists at all, the full literal path itself as
          an existing real DIRECTORY -- meaning no archive is involved at
          all; the target file sits directly on disk inside that folder
          (possibly a level or two deeper), under whatever name fileName
          specifies.
  2a. For (a): copies that real file into a per-row staging subfolder and
      opens it as an archive (even if PathToZipFile's own tail looks like a
      plain file -- e.g. "...txt.gz" -- the whole thing may actually be a
      real file on disk AND still be an archive that needs opening).
      At each container level, lists its full contents (metadata only, no
      extraction) and tries to match fileName against that listing via
      suffix matching. If ambiguous, retries using extra context pulled
      from PathToZipFile's own folder names before giving up. If no name
      match but exactly one file entry exists (a single-file compressor
      like .gz can only ever hold one file), uses it unconditionally. If
      none of that resolves anything, falls back to using leftover literal
      path segments to find a nested archive to descend into, and repeats.
  2b. For (b): searches the real directory tree directly (no extraction at
      all) using the same matching/disambiguation logic.
  3. Extracts/copies only the single matched file, moves (or, for an
     original/untouched source file, copies) it to the destination folder
     renamed to "{fileName}_{caseName}_{GUID}{original extension}"
     (appending _1, _2... on a name collision), then deletes the row's
     staging subfolder.

Runs rows in parallel (ThreadPoolExecutor) and writes a summary CSV with
PathToZipFile, GUID, caseName, fileName, Status, Reason, TimeTakenSeconds,
TimeTakenMinutes.

No retries: a failure is logged in the summary and processing moves on.
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

SCRIPT_DIR = Path(__file__).resolve().parent

SEVEN_ZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]

ARCHIVE_EXTENSIONS = {
    ".zip", ".zipx", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".tbz2",
    ".xz", ".txz", ".cab", ".iso", ".ace", ".arj", ".lzh", ".z",
}

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MAX_NESTING_DEPTH = 25  # safety cap against pathological/degenerate archives

OUTPUT_FIELDNAMES = [
    "PathToZipFile", "GUID", "caseName", "fileName",
    "Status", "Reason", "TimeTakenSeconds", "TimeTakenMinutes",
]

# Expected columns and their 0-based fallback position (A=0, C=2, D=3, H=7),
# used when the header name itself can't be found (export naming drift).
COLUMN_SPECS = {
    "PathToZipFile": 0,
    "GUID": 2,
    "caseName": 3,
    "fileName": 7,
}

_dest_name_lock = threading.Lock()
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 7-Zip basics
# ---------------------------------------------------------------------------

def resolve_seven_zip() -> str:
    """Locate 7z.exe: known install paths first, then PATH. Fail fast if missing."""
    for candidate in SEVEN_ZIP_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return found
    raise RuntimeError(
        "Could not locate 7z.exe. Install 7-Zip or ensure it is on PATH. "
        f"Checked: {', '.join(SEVEN_ZIP_CANDIDATES)}, and PATH."
    )


def is_archive_ext(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_EXTENSIONS


def classify_failure(output_text: str) -> str:
    """Classify a failed 7z run's combined stdout/stderr into a reason."""
    lower = output_text.lower()
    if "wrong password" in lower or "enter password" in lower or "can not open encrypted archive" in lower:
        return "password-protected"
    if (
        "data error" in lower or "crc failed" in lower or "unexpected end of archive" in lower
        or "headers error" in lower or "is not archive" in lower or "unexpected end of data" in lower
    ):
        return "corrupt"
    if "cannot open the file as" in lower or "unsupported" in lower:
        return "unsupported-format"
    first_line = next((line.strip() for line in output_text.splitlines() if line.strip()), "unknown error")
    return f"extraction-failed: {first_line}"


# ---------------------------------------------------------------------------
# Metadata-only listing (no extraction, no decryption of entry contents)
# ---------------------------------------------------------------------------

class ArchiveEntry:
    __slots__ = ("segments", "size", "encrypted")

    def __init__(self, segments, size, encrypted):
        self.segments = segments
        self.size = size
        self.encrypted = encrypted


def split_path_segments(value: str):
    """Split a path on both '/' and '\\', dropping empty segments."""
    normalized = value.replace("\\", "/")
    return [seg for seg in normalized.split("/") if seg]


def parse_slt_listing(output_text: str):
    """Parse `7z l -slt` output into a list of ArchiveEntry (files only).

    The archive-level summary block (always emitted first/once per listing)
    is identified by the presence of a "Physical Size" key -- that key is
    unique to the summary block and never appears on a real per-file entry,
    regardless of archive format. This is deliberately NOT based on the
    presence of a "Folder" key: formats with no folder concept at all
    (gzip, bzip2, xz, .Z -- single-stream compressors) never emit "Folder"
    on their one real file entry either, so checking for "Folder" would
    wrongly mistake that entry for the summary block and silently drop it.
    """
    blocks = []
    current = {}
    for raw_line in output_text.splitlines():
        line = raw_line.rstrip("\r")
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = {}
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    if current:
        blocks.append(current)

    entries = []
    for block in blocks:
        if "Physical Size" in block:
            continue  # the archive's own summary block, not a real entry
        if block.get("Folder") == "+":
            continue  # directory entry, never a resolvable target
        raw_path = block.get("Path", "")
        segments = split_path_segments(raw_path)
        if not segments:
            continue
        size_text = block.get("Size", "")
        try:
            size = int(size_text)
        except ValueError:
            size = None
        encrypted = block.get("Encrypted") == "+"
        entries.append(ArchiveEntry(segments=segments, size=size, encrypted=encrypted))
    return entries


def list_archive_entries(seven_zip: str, archive_path: Path):
    """List archive_path's file entries without extracting anything.
    Raises RuntimeError(reason) if the archive can't be listed at all."""
    result = subprocess.run(
        [seven_zip, "l", "-slt", "-sccUTF-8", str(archive_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        raise RuntimeError(classify_failure(combined))
    return parse_slt_listing(result.stdout)


def list_directory_entries(base_dir: Path):
    """List real files under an existing plain folder on disk (recursively),
    as ArchiveEntry-like records -- used when PathToZipFile resolves to a
    real directory with no archive involved at all. encrypted is always
    False (nothing here is a compressed/encrypted entry)."""
    entries = []
    for p in base_dir.rglob("*"):
        if p.is_file():
            segments = list(p.relative_to(base_dir).parts)
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            entries.append(ArchiveEntry(segments=segments, size=size, encrypted=False))
    return entries


def matches_suffix(segments, suffix_norm):
    if not suffix_norm or len(suffix_norm) > len(segments):
        return False
    tail = segments[-len(suffix_norm):]
    return [os.path.normcase(s) for s in tail] == suffix_norm


def _resolve_by_suffix(entries, segments):
    """Longest-suffix-first match of `segments` against entries. Returns
    (entry, None) if exactly one entry matches at some suffix length,
    (None, "ambiguous-match") if more than one does, or
    (None, "file-not-found") if none ever does."""
    if not segments:
        return None, "file-not-found"
    for k in range(len(segments), 0, -1):
        suffix_norm = [os.path.normcase(s) for s in segments[-k:]]
        matches = [e for e in entries if matches_suffix(e.segments, suffix_norm)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "ambiguous-match"
    return None, "file-not-found"


def resolve_target(entries, file_name: str, context_segments=None):
    """Resolve file_name against entries. If the plain name-only match is
    ambiguous, retries using extra context pulled from the original
    PathToZipFile's own folder names (context_segments), growing the
    prepended context one ancestor folder at a time -- in case this
    container's/folder's internal layout mirrors the outer filesystem path
    it was pulled from. Only reports a genuine ambiguous-match if that
    still doesn't narrow it to exactly one entry.

    Returns (entry, reason) -- entry is None unless exactly one entry
    resolves.
    """
    fname_segments = split_path_segments(file_name)
    entry, reason = _resolve_by_suffix(entries, fname_segments)
    if entry is not None or reason != "ambiguous-match":
        return entry, reason

    if context_segments:
        for extra in range(1, len(context_segments) + 1):
            extended = context_segments[-extra:] + fname_segments
            attempt_entry, attempt_reason = _resolve_by_suffix(entries, extended)
            if attempt_entry is not None:
                return attempt_entry, None
            if attempt_reason == "file-not-found":
                break

    return None, "ambiguous-match"


def describe_ambiguous_candidates(entries, file_name: str) -> str:
    """Build a human-readable detail string listing exactly which entries
    collided on a name-only match, for diagnostics."""
    fname_segments = split_path_segments(file_name)
    for k in range(len(fname_segments), 0, -1):
        suffix_norm = [os.path.normcase(s) for s in fname_segments[-k:]]
        candidates = [e for e in entries if matches_suffix(e.segments, suffix_norm)]
        if len(candidates) > 1:
            return "; ".join(
                f"{'/'.join(c.segments)} ({c.size if c.size is not None else '?'} bytes)"
                for c in candidates
            )
    return "no candidate detail available"


def extract_single_entry(seven_zip: str, archive_path: Path, dest_dir: Path, entry_segments):
    """Targeted extraction of exactly one entry (never the whole archive) --
    this is what keeps traversal cheap even on 50GB archives."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    rel_arg = "/".join(entry_segments)
    result = subprocess.run(
        [seven_zip, "x", str(archive_path), f"-o{dest_dir}", "-y", "-bd", "-sccUTF-8", rel_arg],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        return None, classify_failure(combined)
    extracted_path = dest_dir / Path(*entry_segments)
    if not extracted_path.is_file():
        return None, "extraction-produced-no-file"
    return extracted_path, ""


# ---------------------------------------------------------------------------
# Resolving PathToZipFile: either a real file prefix, or a real directory
# ---------------------------------------------------------------------------

def resolve_source(path_to_zip: str):
    """Resolve PathToZipFile to either a real file or a real plain
    directory. Returns (kind, path, remainder):

      kind == "file": path is the longest real FILE prefix along
                       path_to_zip; remainder is the leftover path parts
                       beyond it (possibly empty) -- the normal
                       archive-traversal case.
      kind == "dir":  no file-prefix exists anywhere along path_to_zip, but
                       the full literal path itself exists as a real
                       directory -- meaning no archive is involved at all;
                       the target file sits directly inside it (or a
                       subfolder of it). remainder is always [].
      kind is None:   nothing along path_to_zip exists at all (true
                       source-not-found).

    Deliberately does not check is_archive() on the file case here: a real
    file that turns out not to actually be a recognized archive format is
    intentionally left for 7z/the archive-listing step to fail on and
    classify, rather than silently walking past it to a shorter prefix.
    """
    parts = Path(path_to_zip).parts
    for i in range(len(parts), 0, -1):
        candidate = Path(*parts[:i])
        if candidate.is_file():
            return "file", candidate, list(parts[i:])
    full_path = Path(path_to_zip)
    if full_path.is_dir():
        return "dir", full_path, []
    return None, None, None


def find_nested_archive_prefix(entries, remaining_segments):
    """Among entries that are themselves archives (by extension), find one
    whose path is an exact prefix of remaining_segments -- i.e. the literal
    leftover path really does point at a nested archive to descend into.
    Picks the longest (most specific) match if more than one qualifies.
    Returns (entry, consumed_count) or (None, 0).
    """
    best = None
    best_len = 0
    for e in entries:
        n = len(e.segments)
        if n == 0 or n > len(remaining_segments):
            continue
        if Path(e.segments[-1]).suffix.lower() not in ARCHIVE_EXTENSIONS:
            continue
        candidate_prefix = [os.path.normcase(s) for s in remaining_segments[:n]]
        entry_norm = [os.path.normcase(s) for s in e.segments]
        if candidate_prefix == entry_norm and n > best_len:
            best = e
            best_len = n
    return best, best_len


def locate_target_in_directory(real_dir_path: Path, file_name: str, path_context_segments):
    """Search real_dir_path directly on disk (recursively, no archive
    involved at all) for file_name, using the same suffix-matching and
    ambiguity-disambiguation logic used for archive listings. Returns
    (target_path, reason) -- target_path is None on failure."""
    entries = list_directory_entries(real_dir_path)
    target, err = resolve_target(entries, file_name, path_context_segments)
    if target is None:
        if err == "ambiguous-match":
            details = describe_ambiguous_candidates(entries, file_name)
            return None, f"ambiguous-match: {details}"
        return None, "file-not-found"
    return real_dir_path.joinpath(*target.segments), ""


def locate_target_file(seven_zip: str, real_zip_path: Path, remainder_parts, row_staging_dir: Path,
                        file_name: str, path_context_segments):
    """Returns (target_file_path, reason). reason is "" on success.

    At every container level, lists the full listing and tries to directly
    suffix-match file_name against it (regardless of how much literal path
    is left), using path_context_segments (the full original PathToZipFile,
    split into parts) to disambiguate a same-name collision before ever
    falling back to using leftover literal segments to find a nested
    archive to descend into.

    If no name match is found AND the container holds exactly one file
    entry, that entry is used unconditionally -- a single-file compressor
    (.gz/.bz2/.xz/.Z) can only ever contain one file, so there is no
    ambiguity to protect against even if that entry's internally-stored
    name doesn't textually match file_name. If that sole entry is itself an
    archive, descend into it and keep searching, rather than treating it as
    the final file.
    """
    row_staging_dir.mkdir(parents=True, exist_ok=True)
    container = row_staging_dir / real_zip_path.name
    shutil.copy2(real_zip_path, container)

    remaining = list(remainder_parts)
    depth = 0

    while True:
        depth += 1
        if depth > MAX_NESTING_DEPTH:
            return None, "max-nesting-depth-exceeded"

        try:
            entries = list_archive_entries(seven_zip, container)
        except RuntimeError as exc:
            reason = str(exc)
            if reason == "unsupported-format" and not remaining and depth == 1:
                # Not actually an archive at all -- the resolved real file
                # IS the final plain file, nothing further to extract.
                return real_zip_path, ""
            if reason == "password-protected":
                return None, f"encrypted-container: {container.name}"
            return None, f"extraction-failed ({container.name}): {reason}"

        # 1) Direct match (with path-context disambiguation on ambiguity):
        #    does file_name resolve uniquely against this container's full
        #    listing, regardless of literal path depth?
        target, err = resolve_target(entries, file_name, path_context_segments)
        if target is not None:
            extract_dir = row_staging_dir / f"_extract_{depth}_{uuid.uuid4().hex[:8]}"
            extracted_path, ex_reason = extract_single_entry(seven_zip, container, extract_dir, target.segments)
            if extracted_path is None:
                if ex_reason == "password-protected":
                    return None, f"encrypted-entry: {container.name}"
                return None, f"extraction-failed ({container.name}): {ex_reason}"
            return extracted_path, ""

        if err == "ambiguous-match":
            details = describe_ambiguous_candidates(entries, file_name)
            return None, f"ambiguous-match: {details}"

        # 2) No name match: if this container holds exactly one file, there
        #    is no ambiguity possible -- a single-file compressor (.gz etc.)
        #    can only ever hold one entry, so it must be the target (or, if
        #    it's itself an archive, the next container to search inside),
        #    regardless of what its internally-stored name happens to be.
        if len(entries) == 1:
            only_entry = entries[0]
            if Path(only_entry.segments[-1]).suffix.lower() in ARCHIVE_EXTENSIONS:
                extract_dir = row_staging_dir / f"_single_{depth}_{uuid.uuid4().hex[:8]}"
                nested_path, ex_reason = extract_single_entry(seven_zip, container, extract_dir, only_entry.segments)
                if nested_path is None:
                    if ex_reason == "password-protected":
                        return None, f"encrypted-entry: {container.name}"
                    return None, f"extraction-failed ({container.name}): {ex_reason}"
                container = nested_path
                continue
            else:
                extract_dir = row_staging_dir / f"_single_{depth}_{uuid.uuid4().hex[:8]}"
                extracted_path, ex_reason = extract_single_entry(seven_zip, container, extract_dir, only_entry.segments)
                if extracted_path is None:
                    if ex_reason == "password-protected":
                        return None, f"encrypted-entry: {container.name}"
                    return None, f"extraction-failed ({container.name}): {ex_reason}"
                return extracted_path, ""

        # 3) No direct match, multiple entries: see if the leftover literal
        #    path points at a nested archive to descend into.
        if not remaining:
            return None, "file-not-found-in-archive"

        nested_entry, consumed = find_nested_archive_prefix(entries, remaining)
        if nested_entry is None:
            return None, "file-not-found-in-archive"

        extract_dir = row_staging_dir / f"_nested_{depth}_{uuid.uuid4().hex[:8]}"
        nested_path, ex_reason = extract_single_entry(seven_zip, container, extract_dir, nested_entry.segments)
        if nested_path is None:
            if ex_reason == "password-protected":
                return None, f"encrypted-entry: {container.name}"
            return None, f"extraction-failed ({container.name}): {ex_reason}"

        container = nested_path
        remaining = remaining[consumed:]


# ---------------------------------------------------------------------------
# Filename / destination handling
# ---------------------------------------------------------------------------

def sanitize_component(value: str) -> str:
    """Turn a free-text value into a safe single filename component."""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", (value or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned or uuid.uuid4().hex


def reserve_destination_path(destination_root: Path, stem: str, case_name: str, guid: str, ext: str) -> Path:
    """Pick a free filename at destination_root and claim it (empty
    placeholder file) under a lock. The lock only guards this fast
    check-and-claim step -- never the actual (slow, large) copy/move -- so
    parallel workers don't serialize on disk I/O.
    """
    with _dest_name_lock:
        base = f"{stem}_{case_name}_{guid}"
        candidate = destination_root / f"{base}{ext}"
        counter = 1
        while candidate.exists():
            candidate = destination_root / f"{base}_{counter}{ext}"
            counter += 1
        candidate.touch()
        return candidate


def move_file(src: Path, dst: Path) -> None:
    """Move src onto dst (which may already exist as an empty placeholder).
    Falls back to copy+delete if src/dst are on different drives (os.replace
    can't rename across devices)."""
    try:
        os.replace(src, dst)
    except OSError:
        shutil.copy2(src, dst)
        src.unlink()


def is_within(path: Path, root: Path) -> bool:
    """True if path is root itself or lives somewhere under it. Used to
    decide whether a resolved target file is a disposable staged/extracted
    copy (safe to move/delete) or an original, untouched source file on
    disk (must only ever be copied, never moved or deleted)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Column lookup (by name, falling back to fixed position)
# ---------------------------------------------------------------------------

def get_column_value(row: dict, headers, name: str, position: int) -> str:
    """Case-insensitive lookup by header name; falls back to the fixed
    column position (A=0, C=2, D=3, H=7) if the name can't be found."""
    target = name.strip().casefold()
    for key, value in row.items():
        if key is not None and key.strip().casefold() == target:
            v = (value or "").strip()
            if v:
                return v
    if 0 <= position < len(headers):
        header_at_pos = headers[position]
        return (row.get(header_at_pos) or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Per-row processing
# ---------------------------------------------------------------------------

def process_single_row(seven_zip: str, row: dict, headers, row_index: int,
                        staging_root: Path, destination_root: Path) -> dict:
    start = time.perf_counter()

    path_to_zip = get_column_value(row, headers, "PathToZipFile", COLUMN_SPECS["PathToZipFile"])
    guid = get_column_value(row, headers, "GUID", COLUMN_SPECS["GUID"])
    case_name = get_column_value(row, headers, "caseName", COLUMN_SPECS["caseName"])
    file_name_col = get_column_value(row, headers, "fileName", COLUMN_SPECS["fileName"])

    result = {
        "PathToZipFile": path_to_zip,
        "GUID": guid,
        "caseName": case_name,
        "fileName": file_name_col,
        "Status": "Failed",
        "Reason": "",
        "TimeTakenSeconds": "",
        "TimeTakenMinutes": "",
    }

    row_staging_dir = staging_root / f"{row_index}_{sanitize_component(guid)}"

    try:
        if not path_to_zip:
            result["Reason"] = "missing-PathToZipFile"
            return result
        if not file_name_col:
            result["Reason"] = "missing-fileName"
            return result

        kind, real_path, remainder = resolve_source(path_to_zip)
        if kind is None:
            result["Reason"] = "source-not-found"
            return result

        path_context_segments = split_path_segments(path_to_zip)

        if kind == "dir":
            # No archive involved at all -- the target file sits directly
            # on disk inside this real folder (or a subfolder of it).
            target_file, reason = locate_target_in_directory(real_path, file_name_col, path_context_segments)
        else:
            target_file, reason = locate_target_file(
                seven_zip, real_path, remainder, row_staging_dir, file_name_col, path_context_segments
            )

        if target_file is None:
            result["Reason"] = reason
            return result

        # An original/untouched source file (never extracted into staging)
        # must only ever be copied -- moving or deleting it would destroy
        # real evidence data that this script didn't create.
        copy_only = not is_within(target_file, row_staging_dir)

        stem = sanitize_component(Path(file_name_col).stem)
        safe_case = sanitize_component(case_name) if case_name else "unknown-case"
        safe_guid = sanitize_component(guid) if guid else uuid.uuid4().hex
        ext = target_file.suffix

        destination_root.mkdir(parents=True, exist_ok=True)
        dest_path = reserve_destination_path(destination_root, stem, safe_case, safe_guid, ext)

        if copy_only:
            shutil.copy2(target_file, dest_path)
        else:
            move_file(target_file, dest_path)

        result["Status"] = "Successful"
        result["Reason"] = ""
        return result

    except Exception as exc:
        result["Reason"] = f"error: {type(exc).__name__}: {exc}"
        return result

    finally:
        shutil.rmtree(row_staging_dir, ignore_errors=True)
        elapsed = time.perf_counter() - start
        result["TimeTakenSeconds"] = f"{elapsed:.2f}"
        result["TimeTakenMinutes"] = f"{elapsed / 60:.2f}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Extract and move files out of nested archives, per a CSV manifest.")
    parser.add_argument("--input", type=Path, default=SCRIPT_DIR / "input.csv", help="Input CSV path.")
    parser.add_argument("--staging", type=Path, required=True, help="Staging/root folder used for extraction.")
    parser.add_argument("--destination", type=Path, required=True, help="Final destination folder for moved files.")
    parser.add_argument("--output", type=Path, default=SCRIPT_DIR / "transfer_summary.csv", help="Summary CSV output path.")
    parser.add_argument("--workers", type=int, default=8, help="Number of parallel workers (default: 8).")
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.input.is_file():
        print(f"Input CSV not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    seven_zip = resolve_seven_zip()

    staging_root = args.staging / "_processing"
    shutil.rmtree(staging_root, ignore_errors=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    args.destination.mkdir(parents=True, exist_ok=True)

    with open(args.input, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, restkey=None)
        headers = reader.fieldnames or []
        rows = list(reader)

    def _log(message: str) -> None:
        if tqdm is not None:
            tqdm.write(message, file=sys.stderr)
        else:
            print(message, file=sys.stderr)

    out_fh = open(args.output, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_fh, fieldnames=OUTPUT_FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    out_fh.flush()

    succeeded = 0
    failed = 0

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_single_row, seven_zip, row, headers, idx, staging_root, args.destination): idx
                for idx, row in enumerate(rows, start=1)
            }

            iterator = as_completed(futures)
            if tqdm is not None:
                iterator = tqdm(iterator, total=len(futures), desc="Processing rows", unit="row")

            for future in iterator:
                row_index = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "PathToZipFile": "", "GUID": "", "caseName": "", "fileName": "",
                        "Status": "Failed", "Reason": f"error: {type(exc).__name__}: {exc}",
                        "TimeTakenSeconds": "", "TimeTakenMinutes": "",
                    }
                    _log(f"ERROR processing row {row_index}: {exc}")
                    _log(traceback.format_exc())

                if result["Status"] == "Successful":
                    succeeded += 1
                else:
                    failed += 1

                with _write_lock:
                    writer.writerow(result)
                    out_fh.flush()
    finally:
        out_fh.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    print(f"Processed {len(rows)} row(s): {succeeded} succeeded, {failed} failed. Summary written to {args.output}")


if __name__ == "__main__":
    main()
