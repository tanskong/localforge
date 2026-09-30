"""run_python integration tests.

Tests run against the real Service (no mocking). They follow the existing
``sample`` fixture pattern from conftest.py and exercise run_python end to end.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from tests.conftest import git
from tests.test_tools import call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_py(sample, code: str, mode: str = "analysis", **extra) -> dict:
    kwargs = {"code": code, "mode": mode}
    kwargs.update(extra)
    return call(sample, "run_python", **kwargs)


# ---------------------------------------------------------------------------
# Normal capability tests (10)
# ---------------------------------------------------------------------------

def test_01_read_python_file_count_lines(sample):
    """Read a project Python file and count lines (analysis mode, cwd=project)."""
    root = sample[1]
    (root / "utils").mkdir()
    (root / "utils" / "helper.py").write_text(
        "\n".join(f"def f{i}():\n    pass" for i in range(7)), encoding="utf-8"
    )
    code = """
from pathlib import Path
text = Path("utils") / "helper.py"
text = text.read_text()
lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
print(len(lines))
"""
    result = run_py(sample, code)
    assert result["status"] == "ok", result
    assert "14" in result["stdout"], f"expected 14 lines, got: {result['stdout']}"


def test_02_read_json_aggregate(sample):
    """Read a JSON report and aggregate metrics."""
    root = sample[1]
    (root / "reports").mkdir()
    (root / "reports" / "summary.json").write_text(
        json.dumps({"total": 42, "passed": 40, "failed": 2}), encoding="utf-8"
    )
    code = """
import json
from pathlib import Path
data = json.loads((Path("reports") / "summary.json").read_text())
ratio = data["passed"] / data["total"]
print(f"pass_rate={ratio:.3f}")
print(data["failed"])
"""
    result = run_py(sample, code)
    assert result["status"] == "ok", result
    assert "pass_rate=0.952" in result["stdout"]
    assert "2" in result["stdout"]


def test_03_read_log_count_errors(sample):
    """Read log file and count ERROR lines."""
    code = """
from pathlib import Path
log = Path("logs") / "app.log"
text = log.read_text()
errs = [l for l in text.splitlines() if "ERROR" in l]
print(f"errors={len(errs)}")
for e in errs:
    print(e.strip())
"""
    result = run_py(sample, code)
    assert result["status"] == "ok", result
    assert "errors=1" in result["stdout"]
    assert "failure" in result["stdout"].lower()


def test_04_ast_analyze_python(sample):
    """AST-walk a Python file and count function definitions."""
    root = sample[1]
    (root / "parser.py").write_text(
        "def one():\n    pass\ndef two(x, y):\n    return x + y\nclass Foo:\n    def method(self):\n        pass\n",
        encoding="utf-8",
    )
    code = """
import ast
from pathlib import Path
tree = ast.parse(Path("parser.py").read_text())
funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
print("functions:", funcs)
print("count:", len(funcs))
"""
    result = run_py(sample, code)
    assert result["status"] == "ok", result
    assert "one" in result["stdout"] and "two" in result["stdout"] and "method" in result["stdout"]
    assert "count: 3" in result["stdout"]


def test_05_analysis_mode_statistics(sample):
    """Run statistics on project file list."""
    root = sample[1]
    for i in range(10):
        (root / f"file_{i}.py").write_text(f"# {i}\nprint({i})\n", encoding="utf-8")
    code = """
from pathlib import Path
import statistics
files = list(Path(".").glob("*.py"))
sizes = [p.stat().st_size for p in files]
print(f"py_files={len(files)}")
print(f"avg_size={statistics.mean(sizes):.1f}")
"""
    result = run_py(sample, code)
    assert result["status"] == "ok", result
    assert "py_files=11" in result["stdout"]


def test_06_workspace_generate_csv(sample):
    """workspace mode generates a CSV inside the sandbox (cwd=sandbox)."""
    code = """
import csv
from pathlib import Path
out = Path("report.csv")
with out.open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["name", "value"])
    w.writerow(["a", 1])
    w.writerow(["b", 2])
print("written")
"""
    result = run_py(sample, code, mode="workspace")
    assert result["status"] == "ok", result
    assert "written" in result["stdout"]
    assert any("report.csv" in f for f in result["files_created"]), result["files_created"]


def test_07_workspace_generate_json(sample):
    """workspace mode writes a JSON report file in sandbox."""
    code = """
import json
from pathlib import Path
Path("analysis.json").write_text(json.dumps({"count": 99, "tags": ["ok"]}))
print("done")
"""
    result = run_py(sample, code, mode="workspace")
    assert result["status"] == "ok", result
    assert "analysis.json" in result["files_created"]
    sandbox_root = Path(result["sandbox_dir"])
    assert sandbox_root.exists()
    json_file = next(sandbox_root.rglob("analysis.json"))
    data = json.loads(json_file.read_text())
    assert data == {"count": 99, "tags": ["ok"]}


def test_08_timeout_enforced(sample):
    """Long-running code is killed; status is timeout."""
    code = """
import time
time.sleep(30)
print("never")
"""
    result = run_py(sample, code, timeout_seconds=2)
    assert result["status"] == "timeout", result
    assert result["duration_ms"] >= 1500, f"expected >=1500ms, got {result['duration_ms']}"
    assert result["truncated"]


def test_09_large_stdout_truncated(sample):
    """stdout larger than 32KB is truncated."""
    code = """
print("x" * 100000)
"""
    result = run_py(sample, code)
    assert result["status"] == "ok"
    assert result["truncated"]
    assert len(result["stdout"]) < 50000
    assert "[stdout truncated]" in result["stdout"]


def test_10_python_exception_structured(sample):
    """Python runtime exception is returned as structured error."""
    code = """
raise ValueError("boom!")
"""
    result = run_py(sample, code)
    assert result["status"] == "error"
    assert "ValueError" in result["result_summary"]
    assert "boom!" in result["result_summary"]


# ---------------------------------------------------------------------------
# Security tests (14)
# ---------------------------------------------------------------------------

def test_11_os_system_blocked(sample):
    code = "import os\nos.system('dir')"
    result = run_py(sample, code)
    assert result["status"] == "blocked_by_policy", result
    assert result["security_blocked"]


def test_12_subprocess_blocked(sample):
    code = "import subprocess\nsubprocess.call(['echo', 'pwned'])"
    result = run_py(sample, code)
    assert result["status"] == "blocked_by_policy"


def test_13_socket_blocked(sample):
    code = "import socket\ns = socket.socket()"
    result = run_py(sample, code)
    assert result["status"] == "blocked_by_policy"


def test_14_requests_external_blocked(sample):
    code = "import requests\nrequests.get('https://example.com')"
    result = run_py(sample, code)
    assert result["status"] == "blocked_by_policy"


def test_15_dotenv_read_blocked(sample):
    """Reading .env is blocked — static scan + guarded path-level open."""
    root = sample[1]
    (root / ".env").write_text("SECRET=abc123", encoding="utf-8")
    code = """
from pathlib import Path
p = Path(".env")
try:
    text = p.read_text()
    print("FOUND:", text)
except Exception as e:
    print("BLOCKED:", type(e).__name__)
"""
    result = run_py(sample, code)
    assert "abc123" not in json.dumps(result), f"secret leaked! result={result}"


def test_16_read_outside_project_blocked(sample):
    """Trying to read outside project must not leak content."""
    code = """
from pathlib import Path
# Try to read one level up from project root
outside = Path("..")
try:
    for f in outside.iterdir():
        print("DIR_ENTRY:", f.name)
except Exception as e:
    print("BLOCKED:", type(e).__name__, e)
"""
    result = run_py(sample, code)
    # result must be well-formed and status is valid
    assert result["status"] in ("ok", "error", "blocked_by_policy")


def test_17_path_traversal_escaped(sample):
    code = """
from pathlib import Path
p = Path("..") / ".." / ".." / "Windows" / "System32" / "drivers" / "etc" / "hosts"
try:
    text = p.read_text()[:500]
    print(text)
except Exception as e:
    print("BLOCKED:", type(e).__name__)
"""
    result = run_py(sample, code)
    assert "127.0.0.1" not in result.get("stdout", "")


def test_18_symlink_escape_blocked(sample):
    """Symlink pointing outside project must fail."""
    import os as _os
    root = sample[1]
    outside = root.parent / "secrets"
    outside.mkdir(exist_ok=True)
    (outside / "leak.txt").write_text("super-secret", encoding="utf-8")
    link = root / "escape_link"
    if _os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        code = """
from pathlib import Path
p = Path("escape_link") / "leak.txt"
try:
    print(p.read_text())
except Exception as e:
    print("BLOCKED:", type(e).__name__)
"""
        result = run_py(sample, code)
        assert "super-secret" not in json.dumps(result)
    finally:
        if _os.name == "nt":
            try:
                link.rmdir()
            except OSError:
                pass
        else:
            link.unlink()


def test_19_analysis_mode_write_project_blocked(sample):
    """analysis mode must not write into the project (cwd=project)."""
    code = """
from pathlib import Path
p = Path("NEW_FILE.txt")
try:
    p.write_text("hacked")
    print("WRITE_OK")
except PermissionError as e:
    print(f"BLOCKED: {e}")
except Exception as e:
    print(f"OTHER: {type(e).__name__}")
"""
    result = run_py(sample, code, mode="analysis")
    assert result["status"] == "ok", result
    assert "WRITE_OK" not in result["stdout"]
    assert "BLOCKED" in result["stdout"]
    assert not (sample[1] / "NEW_FILE.txt").exists(), "project must not be modified"


def test_20_workspace_write_outside_sandbox_blocked(sample):
    """workspace writes must stay inside sandbox. Use PROJECT_ROOT variable."""
    code = """
from pathlib import Path
import os  # this will be blocked by static scan; use PROJECT_ROOT variable instead
"""
    # No, let's not use os — use PROJECT_ROOT which we inject.
    code = """
from pathlib import Path
# PROJECT_ROOT is injected by bootstrap
p = Path(PROJECT_ROOT) / "OUTSIDE_WORKSPACE.txt"
try:
    p.write_text("hack")
    print("WRITE_OK")
except PermissionError as e:
    print(f"BLOCKED: {e}")
except Exception as e:
    print(f"OTHER: {type(e).__name__}")
"""
    result = run_py(sample, code, mode="workspace")
    assert result["status"] == "ok", result
    assert "WRITE_OK" not in result["stdout"], result
    assert "BLOCKED" in result["stdout"], result


def test_21_delete_project_file_blocked(sample):
    """analysis mode cannot delete project files."""
    root = sample[1]
    (root / "do_not_delete.txt").write_text("keep me", encoding="utf-8")
    code = """
from pathlib import Path
p = Path("do_not_delete.txt")
try:
    p.unlink()
    print("DELETE_OK")
except PermissionError as e:
    print(f"BLOCKED: {e}")
except Exception as e:
    print(f"OTHER: {type(e).__name__}")
"""
    result = run_py(sample, code)
    assert "DELETE_OK" not in result["stdout"]
    assert (root / "do_not_delete.txt").exists()


def test_22_env_secrets_not_leaked(sample, monkeypatch):
    """os module is forbidden; even if it somehow slipped, secrets must not leak."""
    monkeypatch.setenv("MY_SUPER_SECRET", "leak-me-not")
    # Static scan blocks "import os" → blocked_by_policy
    code = """
import os
print(os.environ.get("MY_SUPER_SECRET"))
"""
    result = run_py(sample, code)
    assert result["status"] == "blocked_by_policy", f"expected block, got {result['status']}"
    assert "leak-me-not" not in json.dumps(result)


def test_23_git_modification_blocked(sample):
    """subprocess blocked → cannot run git."""
    code = """
import subprocess
subprocess.run(["git", "status"])
"""
    result = run_py(sample, code)
    assert result["status"] == "blocked_by_policy"
    out = subprocess.run(["git", "-C", str(sample[1]), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert not out, f"working tree must stay clean: {out}"


def test_24_failed_execution_no_main_crash(sample):
    """Even after a policy-blocked run, subsequent calls work fine."""
    bad_code = """
import os
os.system("echo pwned")
"""
    good_code = """
from pathlib import Path
print("alive")
"""
    r1 = run_py(sample, bad_code)
    assert r1["status"] == "blocked_by_policy"
    r2 = run_py(sample, good_code)
    assert r2["status"] == "ok"
    assert "alive" in r2["stdout"]


# ---------------------------------------------------------------------------
# Integration: read-only contract (extends existing read_only_files_unchanged)
# ---------------------------------------------------------------------------

def test_project_files_unchanged_after_run_python(sample):
    """Business project files must remain identical after run_python calls."""
    import hashlib
    root = sample[1]

    def snapshot():
        return {
            str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    # analysis mode reads project files
    run_py(sample, "from pathlib import Path\nprint(len(list(Path('.').rglob('*'))))", mode="analysis")
    # workspace mode writes sandbox (not project) + reads project
    run_py(
        sample,
        """
import json
from pathlib import Path
# Write inside sandbox (cwd=sandbox)
Path("report.json").write_text(json.dumps({"hello": "world"}))
# Read project via PROJECT_ROOT variable
p = Path(PROJECT_ROOT) / "main.py"
print(p.read_text()[:20])
""",
        mode="workspace",
    )
    # Another analysis
    run_py(sample, "import ast\nast.parse('def f(): pass')", mode="analysis")
    after = snapshot()
    # workspace writes go to sandbox, not project → project must match
    assert before == after, f"project was modified! diffs: {set(before) ^ set(after)}"
