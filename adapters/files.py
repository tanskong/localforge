"""Bounded, read-only project file browsing and literal text search."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import stat

from server.security import SCAN_LIMIT, SKIP_DIRS, SecurityError, redact, safe_path

MAX_SOURCE_FILE = 2 * 1024 * 1024
MAX_SOURCE_TEXT = 24_000
MAX_SEARCH_FILES = 500
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_SEARCH_FILE = 256 * 1024
MAX_MATCH_TEXT = 500

BROWSE_SKIP_DIRS = SKIP_DIRS | {
    ".cache", "cache", "caches", "tmp", "temp", ".tmp", ".tox", ".nox",
    ".mypy_cache", ".ruff_cache", ".coverage", "coverage", "vendor", "packages",
}
TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".less", ".yaml", ".yml", ".json",
    ".toml", ".ini", ".cfg", ".conf", ".properties", ".md", ".markdown",
    ".txt", ".rst", ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd",
    ".ps1", ".psm1", ".psd1", ".xml", ".csv", ".tsv", ".sql",
}
TEXT_NAMES = {"dockerfile", "makefile", "procfile", ".gitignore", ".gitattributes", ".editorconfig"}


def _text_file(path: Path) -> bool:
    # Dev mode probes content for binary data instead of guessing from filenames.
    return True


def _decode(raw: bytes) -> str | None:
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
        if b"\x00" in raw[:8192]:
            return None
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _read_bounded(root: Path, relative: str, limit: int) -> tuple[bytes, int, bool]:
    target = safe_path(root, relative)
    with target.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise SecurityError("Unsafe file handle")
        raw = stream.read(limit + 1)
    safe_path(root, relative)
    return raw[:limit], info.st_size, info.st_size > limit or len(raw) > limit


def _walk(root: Path, stats: dict, depth: int | None = None):
    queue = [(root, 0)]
    while queue and stats["scanned"] < SCAN_LIMIT:
        parent, level = queue.pop(0)
        try:
            with os.scandir(parent) as iterator:
                items = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError:
            stats["filtered"] += 1
            continue
        for item in items:
            stats["scanned"] += 1
            if stats["scanned"] > SCAN_LIMIT:
                break
            if item.name.lower() in BROWSE_SKIP_DIRS:
                stats["filtered"] += 1
                continue
            relative = Path(item.path).relative_to(root).as_posix()
            try:
                path = safe_path(root, relative)
            except (SecurityError, OSError):
                stats["filtered"] += 1
                continue
            directory = path.is_dir()
            yield path, relative, directory
            if directory and (depth is None or level + 1 < depth):
                queue.append((path, level + 1))


def list_directory(root: Path, depth: int, limit: int) -> dict:
    entries = []
    stats = {"scanned": 0, "filtered": 0}
    truncated = False
    for item, relative, directory in _walk(root, stats, depth):
        if len(entries) >= limit:
            truncated = True
            break
        entries.append({
            "path": relative,
            "type": "directory" if directory else "file",
            "size": None if directory else item.stat().st_size,
            "security_filtered": False,
        })
    truncated |= stats["scanned"] >= SCAN_LIMIT
    return {
        "status": "ok", "path": str(root), "depth": depth, "entries": entries,
        "returned": len(entries), "filtered_entries": stats["filtered"],
        "security_blocked": stats["filtered"] > 0, "truncated": truncated,
    }


def read_file(target: Path, start_line: int | None, end_line: int | None) -> dict:
    path = str(target)
    if not _text_file(target):
        return {"status": "unsupported_file_type", "path": path, "truncated": False}
    root, relative = target.parent, target.name
    raw, size, too_large = _read_bounded(root, relative, MAX_SOURCE_FILE)
    if too_large:
        return {
            "status": "file_too_large", "path": path, "bytes": size,
            "max_file_bytes": MAX_SOURCE_FILE, "truncated": True,
            "suggestion": "Use a smaller source artifact; the hard file-size limit cannot be bypassed",
        }
    text = _decode(raw)
    if text is None:
        return {"status": "unsupported_binary", "path": path, "bytes": size, "truncated": False}
    lines = text.splitlines()
    total = len(lines)
    first = start_line or 1
    last = min(end_line or total, total)
    if total and first > total:
        return {"status": "line_range_out_of_bounds", "path": path, "total_lines": total, "truncated": False}
    selected = lines[first - 1:last] if total else []
    returned = []
    characters = 0
    for line in selected:
        addition = len(line) + (1 if returned else 0)
        if characters + addition > MAX_SOURCE_TEXT:
            break
        returned.append(line)
        characters += addition
    actual_last = first + len(returned) - 1 if returned else None
    truncated = len(returned) < len(selected)
    return {
        "status": "ok", "path": path, "bytes": size, "total_lines": total,
        "returned_line_range": [first, actual_last] if returned else None,
        "content": redact("\n".join(returned)), "truncated": truncated,
        "next_start_line": actual_last + 1 if truncated and actual_last else None,
    }


def search_text(root: Path, query: str, file_glob: str | None, max_results: int) -> dict:
    if not query or len(query) > 200 or any(ord(char) < 32 for char in query):
        raise ValueError("Query must be 1..200 printable characters")
    if file_glob is not None and (not file_glob or len(file_glob) > 100 or ".." in file_glob or ":" in file_glob or file_glob.startswith(("/", "\\")) or any(ord(char) < 32 for char in file_glob)):
        raise ValueError("Invalid file glob")
    results = []
    scanned_files = scanned_bytes = skipped_files = 0
    stats = {"scanned": 0, "filtered": 0}
    truncated = False
    needle = query.casefold()
    for target, relative, directory in _walk(root, stats):
        if directory or not _text_file(target) or (file_glob and not fnmatch.fnmatchcase(relative, file_glob)):
            continue
        size = target.stat().st_size
        if size > MAX_SEARCH_FILE:
            skipped_files += 1
            continue
        if scanned_files >= MAX_SEARCH_FILES or scanned_bytes + size > MAX_SEARCH_BYTES:
            truncated = True
            break
        raw, size, too_large = _read_bounded(root, relative, MAX_SEARCH_FILE)
        if too_large:
            skipped_files += 1
            continue
        if scanned_bytes + size > MAX_SEARCH_BYTES:
            truncated = True
            break
        text = _decode(raw)
        scanned_files += 1
        scanned_bytes += size
        if text is None:
            skipped_files += 1
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle in line.casefold():
                results.append({"path": relative, "line": number, "text": redact(line[:MAX_MATCH_TEXT])})
                if len(results) >= max_results:
                    truncated = True
                    break
        if truncated and len(results) >= max_results:
            break
    truncated |= stats["scanned"] >= SCAN_LIMIT
    return {
        "status": "ok", "path": str(root), "query": redact(query), "file_glob": file_glob,
        "results": results, "returned": len(results), "matches_found_at_least": len(results),
        "scanned_files": scanned_files, "scanned_bytes": scanned_bytes,
        "skipped_files": skipped_files, "filtered_entries": stats["filtered"],
        "security_blocked": stats["filtered"] > 0, "truncated": truncated,
    }
