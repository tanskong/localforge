"""Small safety boundaries for a trusted, local development MCP.

Dev mode deliberately permits normal project files (including .env and local
databases). The checks here only protect machine credentials and Windows core
areas; this is not an execution sandbox.
"""
from __future__ import annotations
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Any

MAX_FILE = 2 * 1024 * 1024
MAX_TEXT = 64_000
MAX_DIFF = 256_000
MAX_LOG = 64_000
MAX_RESULT = 512_000
MAX_ENTRIES = 1_000
SCAN_LIMIT = 10_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
# Legacy safe-Python policy imports these; dev path policy intentionally ignores names.
DENIED = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "credentials*", "secret*", "token*")
PRIVATE_DIRS = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".config"}

class SecurityError(ValueError): pass

def _parts(value: str) -> list[str]:
    if not isinstance(value, str) or not value or len(value) > 4000 or any(ord(c) < 32 for c in value): raise SecurityError("Invalid path")
    if value.startswith(("\\\\", "//")) or "\x00" in value: raise SecurityError("UNC and device paths are forbidden")
    return value.replace("\\", "/").split("/")

def protected_location(path: Path) -> bool:
    """Protect actual machine-level stores, never ordinary project filenames."""
    parts = [part.casefold() for part in path.resolve(strict=False).parts]
    if any(part in {"windows", "boot", "efi", "recovery", "system volume information"} for part in parts): return True
    if any(part in {".ssh", ".gnupg", "credential manager", "login data", "cookies", "web data", "sam", "security"} for part in parts): return True
    return "tunnel-client" in parts or ("project-mcp" in parts and ".secrets" in parts)

def reject_links(path: Path) -> None:
    # Links are common in dev layouts; resolve final target before deciding.
    return None

def _absolute(value: str, *, exists: bool = True, directory: bool | None = None) -> Path:
    parts = _parts(value)
    if ".." in parts or any(p.endswith((" ", ".")) for p in parts if p): raise SecurityError("Non-canonical path is forbidden")
    path = Path(value)
    if not path.is_absolute() or ":" in value[2:]: raise SecurityError("Path must be an absolute local path")
    path = path.resolve(strict=exists)
    if protected_location(path): raise SecurityError("Machine credential or Windows core location blocked")
    if directory is True and exists and not path.is_dir(): raise SecurityError("Path must be a directory")
    if directory is False and exists and not path.is_file(): raise SecurityError("Path must be a file")
    return path

def validate_root(value: str) -> Path:
    path = _absolute(value, directory=True)
    if path == Path(path.anchor): raise SecurityError("Drive root is too broad")
    return path

def validate_absolute_path(value: str, *, directory: bool | None = None) -> Path: return _absolute(value, directory=directory)

def relative_parts(value: str) -> list[str]:
    if not isinstance(value, str) or not value or len(value) > 2000 or "%" in value or ":" in value or "~" in value or any(ord(c) < 32 for c in value): raise SecurityError("Invalid relative path")
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or value.startswith(("/", "\\")): raise SecurityError("Absolute paths are forbidden")
    parts = value.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) or p.split(".")[0].casefold() in RESERVED for p in parts): raise SecurityError("Path traversal or ambiguous path is forbidden")
    return parts

def is_sensitive(parts: list[str]) -> bool: return False

def safe_path(root: Path, relative: str, excludes: tuple[str, ...] = ()) -> Path:
    parts = relative_parts(relative); normalized = "/".join(parts).casefold()
    if parts and parts[0].casefold() == ".git": raise SecurityError("Project .git metadata is only accessible through git tools")
    if any(normalized == x.casefold() or normalized.startswith(x.casefold() + "/") for x in excludes): raise SecurityError("Project exclusion blocked")
    target = root.joinpath(*parts); resolved = target.resolve(strict=False)
    if protected_location(resolved): raise SecurityError("Path enters a protected location")
    return target

SENSITIVE_KEY = r"(?:api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|authorization|password|passwd|pwd|secret|token|cookie|session(?:[_-]?id)?|connection[_-]?string|database[_-]?url|dsn)"
ASSIGNMENT = re.compile(r"(?im)^(\s*[^\r\n:=]{0,128}" + SENSITIVE_KEY + r"[^\r\n:=]{0,128}[\"']?\s*[:=]\s*[\"']?)[^\r\n\"']*", re.I)
PEM = re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----[\s\S]*?(?:-----END [^-]+-----|\Z)")
TOKENS = re.compile(r"(?i)\b(?:sk-[\w-]+|(?:gh[pousr]_|github_pat_)[\w]+|xox[baprs]-[\w-]+|AKIA[A-Z0-9]{12,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)")
BEARER = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"']+")
URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s\"'<>]+|(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|mssql|amqp)://[^\s\"'<>]+)")

def redact(text: str) -> str:
    return TOKENS.sub("[REDACTED TOKEN]", BEARER.sub("[REDACTED AUTH]", URL.sub("[REDACTED CONNECTION]", ASSIGNMENT.sub(r"\1[REDACTED]", PEM.sub("[REDACTED PRIVATE MATERIAL]", text)))))

def sanitize(value: Any) -> Any:
    if isinstance(value, str): return redact(value)
    if isinstance(value, list): return [sanitize(v) for v in value]
    if isinstance(value, dict): return {str(k): sanitize(v) for k, v in value.items()}
    return value

def read_text(root: Path, relative: str, excludes: tuple[str, ...] = (), *, tail: bool = False, limit: int = MAX_TEXT) -> dict:
    target = safe_path(root, relative, excludes)
    if not target.is_file(): return {"status": "not_found", "truncated": False}
    size = target.stat().st_size
    with target.open("rb") as stream:
        if tail and size > MAX_FILE: stream.seek(-MAX_FILE, os.SEEK_END)
        raw = stream.read(MAX_FILE)
    if b"\x00" in raw: return {"status": "unsupported_binary", "truncated": size > MAX_FILE}
    text = redact(raw.decode("utf-8-sig", errors="replace")); cut = size > MAX_FILE or len(text) > limit
    return {"status": "ok", "text": text[-limit:] if tail else text[:limit], "truncated": cut, "bytes": size, "modified_at": target.stat().st_mtime}
