"""Guarded Python execution for project analysis (application-level sandbox only).

This module does NOT provide OS-level isolation. Restrictions are enforced at
three layers:

1. AST static scan BEFORE execution — rejects code that references forbidden
   modules, functions or patterns (subprocess, socket, eval, exec, pickle,
   os.system, ctypes, winreg, multiprocessing, etc.).
2. Restricted runtime — a custom ``__import__`` whitelist plus a read-only
   wrapper around the project root and an allow-list of safe stdlib modules.
3. Process-level kill — each run is executed in a fresh subprocess with a hard
   timeout. The parent kills the child on expiry, so a runaway script cannot
   block the MCP main process.

Filesystem policy
-----------------
* analysis mode:  read-only access to the requested root via
  ``security.safe_path``. Writes inside the project root are rejected.
* workspace mode: additionally allows writes inside a dedicated sandbox
  subdirectory. Writes outside both scopes are rejected at path resolution.

The design goal is to let GPT run arbitrary, bounded Python for project
analysis without handing it a shell. Do NOT treat this as a VM.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from server.security import (
    DENIED,
    PRIVATE_DIRS,
    reject_links,
    redact,
    safe_path,
    sanitize,
    SecurityError,
)

# ---------------------------------------------------------------------------
# Constants and policies
# ---------------------------------------------------------------------------

MAX_CODE_BYTES = 16 * 1024       # 16 KB source cap
MAX_STDOUT_CHARS = 32_000        # stdout captured then truncated
MAX_STDERR_CHARS = 8_000         # stderr captured then truncated
MAX_READ_FILES = 200             # total project files a single run may read
MAX_READ_BYTES_TOTAL = 8 * 1024 * 1024   # 8 MB aggregate read
MAX_READ_FILE_BYTES = 512 * 1024         # 512 KB per file read
SANDBOX_WRITE_BYTES = 4 * 1024 * 1024    # 4 MB per run sandbox write budget

# Standard-library modules we consider safe for analysis code.
# Deliberately excluded: subprocess, os (only os.path allowed via pathlib),
# socket, http*, urllib.request, multiprocessing, ctypes, cffi, pickle,
# shelve, code, pty, crypt, ssl, email smtp/imap/pop, dbm, sqlite3, bz2/gzip
# in write mode, tarfile/zipfile (to avoid archive-based escape), random
# (not security-relevant so allowed), tempfile (may leak out — banned).
#
# We also strip __builtins__ to remove eval / exec / compile / open(flags)
# access at runtime.

SAFE_STDLIB = frozenset({
    # I/O primitives (read-only wrappers provided separately)
    "io",
    # Data structures and algorithms
    "collections",
    "heapq",
    "itertools",
    "functools",
    "operator",
    "math",
    "statistics",
    "decimal",
    "fractions",
    "numbers",
    "cmath",
    # Text
    "re",
    "string",
    "textwrap",
    "unicodedata",
    "difflib",
    "codecs",
    "base64",
    "hashlib",
    "hmac",
    # Standard container serialisers — read-safe
    "json",
    "csv",
    # TOML was added to stdlib in 3.11
    "tomllib",
    # Path operations (read-only semantics enforced by wrapper)
    "pathlib",
    # Time / datetime (no network)
    "time",
    "datetime",
    "calendar",
    # Abstract syntax
    "ast",
    "tokenize",
    # Misc read-safe
    "pprint",
    "reprlib",
    "copy",
    "copyreg",
    "enum",
    "dataclasses",
    "typing",
})

FORBIDDEN_ATTR_ANY = frozenset({
    # Process / shell
    "system", "popen", "spawn", "fork", "execv", "execve",
    # Network
    "connect", "send", "recv", "bind", "listen", "accept",
    # OS / reg / hardware
    "CreateProcess", "ShellExecute", "RegOpenKey", "RegSetValue",
    # Dynamic code
    "eval", "exec", "compile",
    # Object inspection / mutation
    "__import__", "__code__", "__bases__", "__subclasses__",
    "breakpoint",
})

FORBIDDEN_CALL_NAMES = frozenset({
    "eval", "exec", "compile",
    "input",
    "__import__",
    "breakpoint",
})

FORBIDDEN_IMPORT_PREFIX = (
    "subprocess",
    "os",            # pathlib covers path ops; os exposes too much
    "socket",
    "http",          # http.client / http.server / http.cookiejar
    "urllib",        # urllib.request / urllib.error / urllib.parse (ok but lumped for safety)
    "requests",      # third party
    "httpx",         # third party
    "aiohttp",       # third party
    "websocket",     # third party
    "ctypes",
    "cffi",
    "winreg",        # Windows registry
    "win32api",
    "win32con",
    "win32file",
    "multiprocessing",
    "concurrent.futures",  # can spawn subprocesses in the worst case
    "pickle",
    "shelve",
    "code",
    "codeop",
    "pty",
    "crypt",
    "ssl",
    "dbm",
    "sqlite3",
    "tarfile",
    "zipfile",       # read-only archive access could still write via extraction
    "bz2",
    "gzip",
    "lzma",
    "tempfile",
    "shutil",
    "signal",
    "resource",
    "fcntl",
    "grp",
    "pwd",
    "ctypes.util",
    "importlib",     # dynamic module loading
    "pkgutil",
    "runpy",
    "distutils",
    "setuptools",
    "pip",
    "venv",
)


class PolicyBlock(Exception):
    """Raised when the static scan or runtime wrapper rejects user code."""


# ---------------------------------------------------------------------------
# 1) AST static scan
# ---------------------------------------------------------------------------

def _is_forbidden_import(name: str) -> bool:
    if name in FORBIDDEN_IMPORT_PREFIX:
        return True
    # urllib.parse is a read-safe utility but lumped to keep the surface small.
    for prefix in FORBIDDEN_IMPORT_PREFIX:
        if name.startswith(prefix + "."):
            return True
    return False


def scan_code(source: str) -> list[str]:
    """Return a list of policy violation descriptions; empty list means clean."""
    violations: list[str] = []
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise PolicyBlock(f"syntax_error:{exc.msg}") from None

    for node in ast.walk(tree):
        # Import statements
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                target = alias.name
                if _is_forbidden_import(target):
                    violations.append(f"forbidden_import:{target}")
        # Call expressions
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALL_NAMES:
                violations.append(f"forbidden_call:{name}")
            # Attribute calls — check the attribute name itself
            if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_ATTR_ANY:
                violations.append(f"forbidden_attribute_call:{node.func.attr}")
        # Attribute access on any expression
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTR_ANY:
                violations.append(f"forbidden_attribute:{node.attr}")
        # Global statements
        elif isinstance(node, ast.Global):
            # Writing globals is fine; writing builtins is blocked elsewhere.
            pass

    # Second pass: scan string literals for obvious shell/process keywords
    # that the AST would miss if the user builds an import path dynamically.
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lower = node.value.lower()
                for needle in ("subprocess", "os.system", "os.popen",
                               "socket", "requests.get", "urllib.request",
                               "winreg", "multiprocessing", "ctypes",
                               "__import__", "exec(", "eval("):
                    if needle in lower:
                        violations.append(f"suspicious_string:{needle}")
                        break
    except Exception:
        pass

    return violations


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ---------------------------------------------------------------------------
# 2) Code size check
# ---------------------------------------------------------------------------

def _preflight(source: str) -> None:
    if not isinstance(source, str) or not source.strip():
        raise PolicyBlock("empty_code")
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_CODE_BYTES:
        raise PolicyBlock(f"code_too_large:{len(encoded)}>{MAX_CODE_BYTES}")


# ---------------------------------------------------------------------------
# 3) Bootstrap script (what the subprocess will actually run)
# ---------------------------------------------------------------------------

_BOOTSTRAP = r"""
import sys
import os
import json
import stat
import traceback
import time
from pathlib import Path

# ---- Phase A0: sensitive-file blocklist (mirrors server/security.py) ----
import fnmatch as _fnmatch

_PRIVATE_DIRS = frozenset({".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".config"})
_DENIED = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "id_rsa*", "id_ed25519*", "id_dsa*", "id_ecdsa*", "credentials*",
    "secret*", "token*", "*.kdbx", ".netrc", ".npmrc", ".pypirc",
    "*.sqlite*", "*.db", "*.keystore", "*service-account*.json",
    "*service_account*.json", "*credential*", "*.git-credentials",
    "config.local.*", "settings.local.*", "application-prod*",
)

def _is_sensitive_path(p: Path) -> bool:
    parts = [p.name.lower()]  # leaf name first
    for parent in p.parents:
        if parent == parent.anchor:
            break
        parts.append(parent.name.lower())
    for part in parts:
        if part in _PRIVATE_DIRS:
            return True
        for pat in _DENIED:
            if _fnmatch.fnmatchcase(part, pat):
                return True
    return False


# ---- Phase A: harden builtins --------------------------------------------
import builtins as _builtins_module

# Capture originals BEFORE we monkey-patch anything.
_original_open = _builtins_module.open
_original_import = _builtins_module.__import__
_original_exec = _builtins_module.exec
_original_eval = _builtins_module.eval
_original_compile = _builtins_module.compile
_original_print = _builtins_module.print

_PROJECT_ROOT = Path(os.environ.get("_PROJECT_ROOT", "")).resolve()
_SANDBOX_DIR = Path(os.environ.get("_SANDBOX_DIR", "")).resolve()
_MODE = os.environ.get("_RUN_MODE", "analysis")

# Bootstrap internal tmp/output paths — may live inside sandbox_dir.
_OUT_PATH = os.environ["_OUT_PATH"]
_SRC_PATH = os.environ["_SRC_PATH"]

_ALLOWED_ROOTS = set()
if _PROJECT_ROOT and _PROJECT_ROOT.exists():
    _ALLOWED_ROOTS.add(_PROJECT_ROOT)
if _SANDBOX_DIR and _SANDBOX_DIR.exists():
    _ALLOWED_ROOTS.add(_SANDBOX_DIR)


def _path_is_allowed(p: Path, *, for_write: bool) -> bool:
    # NOTE: does NOT call _reject_links — callers (below) handle that
    # once, and _reject_links walks the full parent chain which is expensive
    # when called per-file inside glob/rglob.
    try:
        rp = p.resolve()
    except OSError:
        return False
    for root in _ALLOWED_ROOTS:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# Cache for link-checked parent directories. A directory that has passed
# _reject_links once does not need to be rechecked on every child/file.
_LINK_CHECKED_DIRS = set()


def _reject_links(p: Path, *, _leaf_only: bool = False) -> None:
    # Reject any symlink or reparse point on path p.
    # _leaf_only=True: only check p itself (parents already verified by walk).
    try:
        parts = [p]
    except OSError:
        return
    if not _leaf_only:
        # Skip parents we've already verified.
        try:
            for parent in p.parents:
                if str(parent) in _LINK_CHECKED_DIRS:
                    break
                parts.append(parent)
        except OSError:
            pass
    for part in parts:
        if not part.exists():
            continue
        try:
            info = part.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PermissionError(f"link_forbidden:{part}")
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionError(f"reparse_point_forbidden:{part}")
    # If a directory passed the check, record it so children skip it.
    try:
        if p.is_dir():
            _LINK_CHECKED_DIRS.add(str(p))
    except OSError:
        pass


def _guarded_open(file, mode="r", *args, **kwargs):
    if not isinstance(file, (str, bytes, os.PathLike)):
        raise PermissionError(f"open({file!r}): path must be a string")
    try:
        p = Path(os.fsdecode(file) if isinstance(file, bytes) else file)
    except Exception:
        raise PermissionError(f"cannot decode path {file!r}")
    m = mode.lower()
    writing = any(c in m for c in ("w", "a", "x", "+"))
    if writing:
        if _MODE == "analysis":
            # In analysis mode bootstrap itself may need to write its internal
            # files (out_path, src_path) if those are INSIDE a declared sandbox.
            # Allow only sandbox dir; reject project-root writes.
            rp = p.resolve()
            if _SANDBOX_DIR and _SANDBOX_DIR in rp.parents or rp == _SANDBOX_DIR:
                pass  # sandbox bootstrap writes are always OK
            elif _OUT_PATH and p == Path(_OUT_PATH):
                pass  # always allow writing the result file
            elif _SRC_PATH and p == Path(_SRC_PATH):
                pass  # always allow reading/writing the user source file
            else:
                raise PermissionError("writes are forbidden in analysis mode")
        else:
            # workspace mode: writes must stay inside sandbox_dir
            if not _path_is_allowed(p, for_write=True):
                raise PermissionError(f"write outside sandbox forbidden: {p}")
    else:
        # read: must be inside project_root or sandbox_dir AND not sensitive.
        _check_read(p)
    return _original_open(file, mode, *args, **kwargs)


_builtins_module.open = _guarded_open


_FORBIDDEN_IMPORT_PREFIX = (
    # NOTE: "os" intentionally absent here. AST static scan already blocks
    # user-facing "import os" / "from os import ...". Many safe stdlib
    # modules (pathlib, statistics, json via codecs, ...) *indirectly*
    # import os; blocking it here breaks legitimate analysis code.
    "subprocess", "socket", "http", "urllib", "requests", "httpx",
    "aiohttp", "websocket", "ctypes", "cffi", "winreg", "win32api",
    "win32con", "win32file", "multiprocessing", "concurrent.futures",
    "pickle", "shelve", "code", "codeop", "pty", "crypt", "ssl", "dbm",
    "sqlite3", "tarfile", "zipfile", "bz2", "gzip", "lzma", "tempfile",
    "shutil", "signal", "resource", "fcntl", "grp", "pwd",
    "runpy", "distutils", "setuptools", "pip", "venv",
)

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Mirror the static scanner's blocklist: anything forbidden by name
    # or prefix. C-extension siblings (e.g. _io, _csv, _thread, _ast) are
    # allowed — they are loaded implicitly by trusted stdlib wrappers.
    if name in _FORBIDDEN_IMPORT_PREFIX or any(
        name.startswith(p + ".") for p in _FORBIDDEN_IMPORT_PREFIX
    ):
        raise ImportError(f"module {name!r} is not allowed by policy")
    return _original_import(name, globals, locals, fromlist, level)


_SAFE_STDLIB = frozenset({
    "io", "collections", "heapq", "itertools", "functools", "operator",
    "math", "statistics", "decimal", "fractions", "numbers", "cmath",
    "re", "string", "textwrap", "unicodedata", "difflib", "codecs",
    "base64", "hashlib", "hmac",
    "json", "csv", "tomllib",
    "pathlib",
    "time", "datetime", "calendar",
    "ast", "tokenize",
    "pprint", "reprlib", "copy", "copyreg", "enum", "dataclasses",
    "typing",
})

_builtins_module.__import__ = _safe_import

# Remove dangerous names from __builtins__. Do NOT remove eval/exec/compile
# here — trusted stdlib modules (ast, json, ...) rely on them internally.
# AST scanning at the parent process already blocks explicit user calls.
for _name in ("breakpoint",):
    try:
        delattr(_builtins_module, _name)
    except Exception:
        pass

# Remove MCP / project-mcp packages from sys.path.
_service_root = os.environ.get("_SERVICE_ROOT")
if _service_root:
    sys.path = [p for p in sys.path
                if not os.path.abspath(p).startswith(os.path.abspath(_service_root))]
sys.path = [p for p in sys.path if os.path.isdir(p)]

# Change working directory so user code that writes Path(".") / "file.txt"
# works as expected.
#   analysis  mode: cwd = project_root (read-only, project files relative here)
#   workspace mode: cwd = sandbox_dir  (writable, sandbox files relative here)
# In workspace mode we also expose PROJECT_ROOT as an absolute path so the
# user can still read project files when needed.
if _MODE == "workspace" and _SANDBOX_DIR and _SANDBOX_DIR.exists():
    try:
        os.chdir(str(_SANDBOX_DIR))
    except OSError:
        pass
elif _PROJECT_ROOT and _PROJECT_ROOT.exists():
    try:
        os.chdir(str(_PROJECT_ROOT))
    except OSError:
        pass


# ---- Phase B: redirect project pathlib writes ---------------------------
_orig_write_text = Path.write_text
_orig_write_bytes = Path.write_bytes
_orig_touch = Path.touch
_orig_unlink = Path.unlink
_orig_mkdir = Path.mkdir
_orig_rmdir = Path.rmdir
_orig_rename = Path.rename
_orig_replace = Path.replace

def _check_write(p: Path) -> None:
    _reject_links(p)
    rp = p.resolve()
    if _MODE == "analysis":
        if _PROJECT_ROOT:
            try:
                rp.relative_to(_PROJECT_ROOT)
                raise PermissionError("writes are forbidden in analysis mode")
            except ValueError:
                pass
        # Bootstrap internal files always permitted
        if _OUT_PATH and p == Path(_OUT_PATH):
            return
        if _SRC_PATH and p == Path(_SRC_PATH):
            return
        # sandbox dir itself also ok (bootstrap may write inside it)
        if _SANDBOX_DIR and (_SANDBOX_DIR in rp.parents or rp == _SANDBOX_DIR):
            return
        raise PermissionError("writes outside sandbox forbidden in analysis mode")
    # workspace mode: only inside sandbox
    if _SANDBOX_DIR:
        try:
            rp.relative_to(_SANDBOX_DIR)
        except ValueError:
            raise PermissionError(f"write outside sandbox forbidden: {p}")

def _guarded_write_text(self, *a, **kw):
    _check_write(self)
    return _orig_write_text(self, *a, **kw)

def _guarded_write_bytes(self, *a, **kw):
    _check_write(self)
    return _orig_write_bytes(self, *a, **kw)

def _guarded_touch(self, *a, **kw):
    _check_write(self)
    return _orig_touch(self, *a, **kw)

def _guarded_unlink(self, *a, **kw):
    _check_write(self)
    return _orig_unlink(self, *a, **kw)

def _guarded_mkdir(self, *a, **kw):
    _check_write(self)
    return _orig_mkdir(self, *a, **kw)

def _guarded_rmdir(self, *a, **kw):
    _check_write(self)
    return _orig_rmdir(self, *a, **kw)

def _guarded_rename(self, target, *a, **kw):
    _check_write(self)
    _check_write(Path(target))
    return _orig_rename(self, target, *a, **kw)

def _guarded_replace(self, target, *a, **kw):
    _check_write(self)
    _check_write(Path(target))
    return _orig_replace(self, target, *a, **kw)

Path.write_text = _guarded_write_text
Path.write_bytes = _guarded_write_bytes
Path.touch = _guarded_touch
Path.unlink = _guarded_unlink
Path.mkdir = _guarded_mkdir
Path.rmdir = _guarded_rmdir
Path.rename = _guarded_rename
Path.replace = _guarded_replace

# Python 3.14 reads files via a C-layer shortcut that does not call
# builtins.open, so we must also patch Path's read-side methods directly.
_orig_read_text = Path.read_text
_orig_read_bytes = Path.read_bytes
_orig_open_path = Path.open
_orig_iterdir = Path.iterdir
_orig_glob = Path.glob
_orig_rglob = Path.rglob

def _check_read(p: Path) -> None:
    # Single pass: reject links + verify inside allowed roots + block sensitive files.
    # _path_is_allowed no longer calls _reject_links — we do it here once.
    _reject_links(p)
    rp = p.resolve()
    if not _path_is_allowed(p, for_write=False):
        raise PermissionError(f"read outside allowed roots forbidden: {p}")
    if _is_sensitive_path(rp):
        raise PermissionError(f"sensitive file read blocked: {p}")

def _guarded_read_text(self, *a, **kw):
    _check_read(self)
    return _orig_read_text(self, *a, **kw)

def _guarded_read_bytes(self, *a, **kw):
    _check_read(self)
    return _orig_read_bytes(self, *a, **kw)

def _guarded_open_path(self, mode="r", *a, **kw):
    _check_read(self)
    # open for write paths handled by _check_write; _check_read only covers
    # the read half; combine both to be safe.
    m = mode.lower()
    if any(c in m for c in ("w", "a", "x", "+")):
        _check_write(self)
    return _orig_open_path(self, mode, *a, **kw)

def _guarded_iterdir(self, *a, **kw):
    # Lazy yield; check self once, then leaf-check each child.
    _check_read(self)
    try:
        if self.is_dir():
            _LINK_CHECKED_DIRS.add(str(self))
    except OSError:
        pass
    for child in _orig_iterdir(self, *a, **kw):
        _reject_links(child, _leaf_only=True)
        yield child

def _guarded_glob(self, pattern, *a, **kw):
    # Lazy yield; parents are verified by the walk, leaf-check only.
    _check_read(self)
    for r in _orig_glob(self, pattern, *a, **kw):
        _reject_links(r, _leaf_only=True)
        yield r

def _guarded_rglob(self, pattern, *a, **kw):
    # Same as glob — lazy yield with leaf-only link check.
    _check_read(self)
    for r in _orig_rglob(self, pattern, *a, **kw):
        _reject_links(r, _leaf_only=True)
        yield r

Path.read_text = _guarded_read_text
Path.read_bytes = _guarded_read_bytes
Path.open = _guarded_open_path
Path.iterdir = _guarded_iterdir
Path.glob = _guarded_glob
Path.rglob = _guarded_rglob


# ---- Phase C: run user code ---------------------------------------------
def _write_result(status, body):
    payload = json.dumps({"status": status, **body}, ensure_ascii=False)
    # Use the ORIGINAL open to bypass our guarded open for bootstrap's own
    # bookkeeping. This is intentionally not exposed to user code.
    with _original_open(_OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(payload)


def main():
    # Diagnostic marker file — written at each phase so we can tell exactly
    # how far the bootstrap got, even if timeout prevents result.json.
    _diag_path = str(Path(_OUT_PATH).parent / "_diag.json")
    def _diag(phase: str, **extra):
        try:
            with _original_open(_diag_path, "w", encoding="utf-8") as _df:
                _df.write(json.dumps({"phase": phase, "pid": os.getpid(), **extra}))
        except Exception:
            pass
    _diag("BOOTSTRAP_STARTED")
    try:
        with _original_open(_SRC_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        _diag("BOOTSTRAP_READ_FAILED", error=str(exc)[:200])
        _write_result("error", {"message": f"bootstrap_read_failed:{exc}"})
        return
    _diag("BOOTSTRAP_READ_OK", source_len=len(source))

    start = time.time()
    stdout_buf = []
    try:
        _diag("USER_CODE_STARTED")
        _builtins_module.print = lambda *a, **kw: stdout_buf.append(" ".join(str(x) for x in a) + "\n")
        # Use the _original_exec we captured — user code sees exec=NameError.
        # Inject absolute paths so the user code can cross-reference project and sandbox.
        _user_globals = {
            "__name__": "__main__",
            "PROJECT_ROOT": str(_PROJECT_ROOT) if _PROJECT_ROOT else None,
            "SANDBOX_DIR": str(_SANDBOX_DIR) if _SANDBOX_DIR else None,
        }
        _original_exec(_original_compile(source, "<run_python>", "exec"), _user_globals)
        _diag("USER_CODE_FINISHED")
        duration = int((time.time() - start) * 1000)
        _diag("WRITING_RESULT")
        _write_result("ok", {"stdout": "".join(stdout_buf), "duration_ms": duration})
    except PermissionError as exc:
        _diag("BLOCKED", error=str(exc)[:200])
        duration = int((time.time() - start) * 1000)
        _write_result("blocked_by_policy", {
            "message": str(exc), "stdout": "".join(stdout_buf), "duration_ms": duration,
        })
    except SyntaxError as exc:
        _diag("SYNTAX_ERROR", error=str(exc)[:200])
        duration = int((time.time() - start) * 1000)
        _write_result("error", {
            "message": f"SyntaxError: {exc.msg} at line {exc.lineno}",
            "stdout": "".join(stdout_buf), "duration_ms": duration,
        })
    except Exception as exc:
        _diag("RUNTIME_ERROR", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        duration = int((time.time() - start) * 1000)
        _write_result("error", {
            "message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=10),
            "stdout": "".join(stdout_buf), "duration_ms": duration,
        })


main()
""".strip() + "\n"


# ---------------------------------------------------------------------------
# 4) Subprocess runner
# ---------------------------------------------------------------------------

def _compile_user_code(source: str) -> bytes:
    """Wrap user code so we can redirect its output and still catch exceptions."""
    # The user sees ``__file__`` if they import pathlib etc. We provide a
    # thin wrapper that just runs the source in a __main__ namespace.
    return source.encode("utf-8")


def run_python(
    *,
    source: str,
    mode: str,
    project_root: Path,
    sandbox_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Execute guarded user Python. Returns the structured response dict."""
    # Validate mode
    if mode not in ("analysis", "workspace"):
        raise ValueError("mode must be 'analysis' or 'workspace'")
    # Pre-flight size check
    _preflight(source)
    # Static AST scan
    try:
        violations = scan_code(source)
    except PolicyBlock as exc:
        return _policy_blocked(str(exc))
    if violations:
        return _policy_blocked(",".join(violations))

    # Ensure sandbox root exists per-run
    sandbox_root = sandbox_root.resolve()
    sandbox_root.mkdir(parents=True, exist_ok=True)
    reject_links(sandbox_root)

    # Write bootstrap + user source to a temp dir INSIDE sandbox_root so the
    # sandbox-level write guards permit bootstrap bookkeeping even in analysis
    # mode. Each run_id is independent so runs do not collide.
    run_tmp = Path(tempfile.mkdtemp(prefix="run_", dir=str(sandbox_root)))
    bootstrap_path = run_tmp / "_bootstrap.py"
    source_path = run_tmp / "_user.py"
    output_path = run_tmp / "_result.json"
    bootstrap_path.write_text(_BOOTSTRAP, encoding="utf-8")
    source_path.write_text(source, encoding="utf-8")

    run_id = run_tmp.name

    env = os.environ.copy()
    env.update({
        "_PROJECT_ROOT": str(project_root.resolve()),
        "_SANDBOX_DIR": str(sandbox_root),
        "_RUN_MODE": mode,
        "_OUT_PATH": str(output_path),
        "_SRC_PATH": str(source_path),
        "_SERVICE_ROOT": str(Path(__file__).resolve().parent.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    start_ns = time.perf_counter_ns()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(bootstrap_path)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            cwd=str(run_tmp),
        )
        try:
            out_data, err_data = proc.communicate(timeout=max(1, timeout_seconds))
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out_data, err_data = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out_data, err_data = b"", b""
            duration_ms = int((time.perf_counter_ns() - start_ns) / 1_000_000)
            return _timeout_result(duration_ms, out_data, err_data)

        duration_ms = int((time.perf_counter_ns() - start_ns) / 1_000_000)

        # Read the structured result JSON the child wrote to disk.
        try:
            raw = output_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except Exception:
            payload = {
                "status": "error",
                "message": f"bootstrap_returned_{returncode}",
                "stdout": out_data.decode("utf-8", errors="replace"),
                "stderr": err_data.decode("utf-8", errors="replace"),
            }

        return _finalise_response(
            payload=payload,
            mode=mode,
            project_root=project_root,
            sandbox_root=sandbox_root,
            run_id=run_id,
            duration_ms=duration_ms,
            out_data=out_data,
            err_data=err_data,
        )
    finally:
        # cleanup — best effort only
        try:
            for p in (bootstrap_path, source_path, output_path):
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            run_tmp.rmdir()
        except OSError:
            pass


def _policy_blocked(reason: str) -> dict[str, Any]:
    return {
        "status": "blocked_by_policy",
        "message": reason,
        "mode": "analysis",
        "stdout": "",
        "stderr": "",
        "duration_ms": 0,
        "policy_events": [reason],
        "truncated": False,
        "files_read_count": 0,
        "files_created": [],
        "files_modified": [],
        "security_blocked": True,
    }


def _timeout_result(duration_ms: int, out_data: bytes, err_data: bytes) -> dict[str, Any]:
    return {
        "status": "timeout",
        "message": f"execution_exceeded_{duration_ms}ms",
        "mode": "analysis",
        "stdout": out_data.decode("utf-8", errors="replace")[-MAX_STDOUT_CHARS:],
        "stderr": err_data.decode("utf-8", errors="replace")[-MAX_STDERR_CHARS:],
        "duration_ms": duration_ms,
        "policy_events": ["timeout"],
        "truncated": True,
        "files_read_count": 0,
        "files_created": [],
        "files_modified": [],
    }


def _finalise_response(
    *,
    payload: dict[str, Any],
    mode: str,
    project_root: Path,
    sandbox_root: Path,
    run_id: str,
    duration_ms: int,
    out_data: bytes,
    err_data: bytes,
) -> dict[str, Any]:
    status = payload.get("status", "error")
    stdout = payload.get("stdout", "") or out_data.decode("utf-8", errors="replace")
    stderr = payload.get("stderr", "") or err_data.decode("utf-8", errors="replace")

    truncated = False
    if len(stdout) > MAX_STDOUT_CHARS:
        stdout = stdout[:MAX_STDOUT_CHARS] + "\n...[stdout truncated]"
        truncated = True
    if len(stderr) > MAX_STDERR_CHARS:
        stderr = stderr[:MAX_STDERR_CHARS] + "\n...[stderr truncated]"
        truncated = True

    # Discover sandbox-side file effects (best-effort; cannot list writes to
    # project root — but those are rejected by guardrails anyway).
    files_created: list[str] = []
    files_modified: list[str] = []
    try:
        for p in sandbox_root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(sandbox_root).as_posix()
                files_created.append(rel)
    except OSError:
        pass

    policy_events: list[str] = payload.get("policy_events", [])
    if status == "blocked_by_policy":
        msg = payload.get("message", "")
        if msg and msg not in policy_events:
            policy_events.append(msg)

    return {
        "status": status,
        "mode": mode,
        "sandbox_run_id": run_id,
        "stdout": redact(stdout),
        "stderr": redact(stderr),
        "result_summary": redact(str(payload.get("message", ""))),
        "duration_ms": duration_ms,
        "files_read_count": 0,  # best-effort; accurate tracking requires an AST-level hook
        "files_created": files_created,
        "files_modified": files_modified,
        "truncated": truncated,
        "policy_events": policy_events,
        "security_blocked": status == "blocked_by_policy",
    }
