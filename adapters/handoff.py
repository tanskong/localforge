"""Scoped task handoff writer.

Writes ONLY to <root>/.ai/TASK_CURRENT.md — nothing else.
This is the sole real-project task-handoff write capability in LocalForge.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from server.security import SecurityError, reject_links, validate_root


MAX_CONTENT_BYTES = 64 * 1024
HANDOFF_SUBDIR = ".ai"
HANDOFF_FILENAME = "TASK_CURRENT.md"


def _is_git_repo(root: Path) -> bool:
    meta = root / ".git"
    return meta.is_dir()


def _gitignore_has_ai(root: Path) -> bool:
    """Return True iff root/.gitignore contains an exact effective line `.ai/`.

    Accepts surrounding whitespace. Comments, negations (`!`), patterns that
    merely start with `.ai`, subdirectory paths or globs all return False —
    LocalForge uses a deliberately strict check; callers must spell it
    exactly `.ai/` on its own line.
    """
    gi = root / ".gitignore"
    if not gi.is_file():
        return False
    try:
        raw = gi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s == ".ai/":
            return True
    return False


def _ensure_safe_dir(dir_path: Path) -> None:
    """Create dir_path if missing, else verify it's a plain, non-linked directory."""
    if dir_path.exists():
        info = dir_path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SecurityError("Links are forbidden")
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise SecurityError("Reparse points are forbidden")
        if not stat.S_ISDIR(info.st_mode):
            raise SecurityError("Expected directory, found different type")
    else:
        # Create it — but ensure no parent is a link
        reject_links(dir_path.parent)
        try:
            dir_path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # Race — re-check
            _ensure_safe_dir(dir_path)


def _safe_file_to_overwrite(path: Path) -> None:
    """Verify an existing file is safe to overwrite — plain file, no links."""
    if not path.exists():
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise SecurityError("Links are forbidden")
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise SecurityError("Reparse points are forbidden")
    if not stat.S_ISREG(info.st_mode):
        raise SecurityError("Expected regular file")
    if info.st_nlink > 1:
        raise SecurityError("Hard links are forbidden")


def _sanitize_content(content: str) -> bytes:
    """Validate and encode content; return UTF-8 bytes.

    Rejects: NUL byte, control chars (except CR/LF/TAB), size > 64 KiB.
    """
    if not isinstance(content, str):
        raise SecurityError("content must be a string")
    # Reject NUL
    if "\x00" in content:
        raise SecurityError("NUL byte is forbidden in task content")
    # Reject non-printable control chars (except common whitespace)
    allowed_ctrl = {"\n", "\r", "\t"}
    for ch in content:
        if ord(ch) < 32 and ch not in allowed_ctrl:
            raise SecurityError("Illegal control character in task content")
    raw = content.encode("utf-8")
    if len(raw) > MAX_CONTENT_BYTES:
        raise SecurityError(f"content exceeds {MAX_CONTENT_BYTES} bytes")
    return raw


def write_task(root: Path, content: str, filename: str = HANDOFF_FILENAME) -> dict:
    """Atomically write <root>/.ai/TASK_CURRENT.md with safety checks.

    Returns on success:
        {"status": "ok", "path": ".ai/TASK_CURRENT.md",
         "operation": "created" | "replaced", "bytes": int, "sha256": str}
    """
    if filename not in {"TASK_CURRENT.md", "RESULT_CURRENT.md", "CHECKPOINT_CURRENT.md"}:
        raise SecurityError("Unsupported handoff file")
    root = validate_root(str(root))
    reject_links(root)

    # Git repo → .gitignore must whitelist .ai/ (fail closed if missing)
    if _is_git_repo(root):
        if not _gitignore_has_ai(root):
            raise SecurityError(
                "ai_directory_not_ignored: root .gitignore must contain an effective rule for .ai/ before handoff update can run"
            )

    target_dir = root / HANDOFF_SUBDIR
    target_file = target_dir / filename

    # Validate the path isn't trying to escape root
    try:
        target_file.resolve().relative_to(root)
    except ValueError:
        raise SecurityError("Target path leaves root")

    # Ensure .ai/ directory is safe (create if missing)
    _ensure_safe_dir(target_dir)
    reject_links(target_dir)  # second line of defense

    # Check target file state (created vs replaced) and safety
    existed = target_file.exists()
    if existed:
        _safe_file_to_overwrite(target_file)
        reject_links(target_file)

    # Validate and encode content
    raw = _sanitize_content(content)
    sha256 = hashlib.sha256(raw).hexdigest()

    # Atomic write via tempfile + os.replace
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=".TASK_CURRENT.", suffix=".tmp", dir=str(target_dir)
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_fh:
            tmp_fh.write(raw)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        os.replace(tmp_path, target_file)
    except Exception:
        # Best-effort cleanup of temp file on any failure
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    return {
        "status": "ok",
        "path": f"{HANDOFF_SUBDIR}/{filename}",
        "operation": "replaced" if existed else "created",
        "bytes": len(raw),
        "sha256": sha256,
    }
