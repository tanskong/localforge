"""Write-task handoff adapter — focused security and correctness tests."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from adapters.handoff import MAX_CONTENT_BYTES, _gitignore_has_ai, _is_git_repo, write_task
from server.security import SecurityError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "-c", "core.autocrlf=false", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def tmp_root():
    """A plain (non-git) writable temp directory under project-mcp/tmp."""
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(base)) as loc:
        yield Path(loc)


@pytest.fixture
def git_root():
    """A git repo with root/.gitignore containing .ai/."""
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(base)) as loc:
        root = Path(loc) / "repo"
        root.mkdir()
        (root / "README.md").write_text("# hi\n", encoding="utf-8")
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".ai/\n", encoding="utf-8")
        git(root, "add", ".gitignore", "README.md")
        git(root, "commit", "-qm", "init")
        yield root


@pytest.fixture
def git_root_no_ignore():
    """A git repo WITHOUT .ai/ in .gitignore."""
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(base)) as loc:
        root = Path(loc) / "repo2"
        root.mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
        git(root, "add", ".gitignore")
        git(root, "commit", "-qm", "init")
        yield root


# ---------------------------------------------------------------------------
# 1. Git repo + root .gitignore includes .ai/ → success
# ---------------------------------------------------------------------------
def test_01_git_repo_with_ignore_success(git_root):
    r = write_task(git_root, "hello")
    assert r["status"] == "ok"
    assert r["operation"] == "created"
    assert (git_root / ".ai" / "TASK_CURRENT.md").is_file()


# ---------------------------------------------------------------------------
# 2. File generated exactly at .ai/TASK_CURRENT.md (not elsewhere)
# ---------------------------------------------------------------------------
def test_02_file_only_at_expected_path(git_root):
    write_task(git_root, "body")
    expected = git_root / ".ai" / "TASK_CURRENT.md"
    siblings = list(git_root.rglob("TASK_CURRENT.md"))
    assert siblings == [expected]
    assert expected.read_text(encoding="utf-8") == "body"


# ---------------------------------------------------------------------------
# 3. First call → operation = created
# ---------------------------------------------------------------------------
def test_03_first_call_created(git_root):
    r = write_task(git_root, "first")
    assert r["operation"] == "created"


# ---------------------------------------------------------------------------
# 4. Second call → operation = replaced
# ---------------------------------------------------------------------------
def test_04_second_call_replaced(git_root):
    write_task(git_root, "first")
    r2 = write_task(git_root, "second")
    assert r2["operation"] == "replaced"
    assert (git_root / ".ai" / "TASK_CURRENT.md").read_text(encoding="utf-8") == "second"


# ---------------------------------------------------------------------------
# 5. Content bytes correct (overwrite is safe, no corruption)
# ---------------------------------------------------------------------------
def test_05_content_bytes_exact(git_root):
    payload = "# Task\nA\nB\nC\n"
    write_task(git_root, payload)
    written = (git_root / ".ai" / "TASK_CURRENT.md").read_text(encoding="utf-8")
    assert written == payload


# ---------------------------------------------------------------------------
# 6. Returned bytes match file size on disk
# ---------------------------------------------------------------------------
def test_06_bytes_match_disk(git_root):
    payload = "x" * 1000
    r = write_task(git_root, payload)
    disk_size = (git_root / ".ai" / "TASK_CURRENT.md").stat().st_size
    assert r["bytes"] == disk_size == len(payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# 7. Returned SHA-256 matches file content
# ---------------------------------------------------------------------------
def test_07_sha256_matches(git_root):
    payload = "sha-check\n"
    r = write_task(git_root, payload)
    actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert r["sha256"] == actual


# ---------------------------------------------------------------------------
# 8. Content > 64 KiB → rejected
# ---------------------------------------------------------------------------
def test_08_content_too_large_rejected(git_root):
    with pytest.raises(SecurityError):
        write_task(git_root, "x" * (MAX_CONTENT_BYTES + 1))


# ---------------------------------------------------------------------------
# 9. NUL byte → rejected
# ---------------------------------------------------------------------------
def test_09_nul_byte_rejected(git_root):
    with pytest.raises(SecurityError):
        write_task(git_root, "a\x00b")


def test_09b_illegal_control_rejected(git_root):
    with pytest.raises(SecurityError):
        write_task(git_root, "a\x01b")


def test_09c_allowed_whitespace_ok(git_root):
    """LF, CR, TAB are allowed."""
    r = write_task(git_root, "a\tb\nc\rd")
    assert r["status"] == "ok"


# ---------------------------------------------------------------------------
# 10. Git repo missing .ai/ ignore → FAIL CLOSED
# ---------------------------------------------------------------------------
def test_10_git_repo_no_ignore_fail_closed(git_root_no_ignore):
    with pytest.raises(SecurityError) as exc:
        write_task(git_root_no_ignore, "x")
    assert "ai_directory_not_ignored" in str(exc.value)
    # The .ai/ dir must NOT have been created either
    assert not (git_root_no_ignore / ".ai").exists()


# ---------------------------------------------------------------------------
# 11. Non-git directory → allowed
# ---------------------------------------------------------------------------
def test_11_non_git_root_allowed(tmp_root):
    r = write_task(tmp_root, "non-git ok")
    assert r["status"] == "ok"
    assert (tmp_root / ".ai" / "TASK_CURRENT.md").read_text(encoding="utf-8") == "non-git ok"


# ---------------------------------------------------------------------------
# 12. .ai is a junction / symlink → BLOCK
# ---------------------------------------------------------------------------
def test_12_ai_dir_is_symlink_blocked(tmp_root):
    target_outside = tmp_root / "outside"
    target_outside.mkdir()
    link = tmp_root / ".ai"
    try:
        os.symlink(str(target_outside), str(link), target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not available on this host")
    with pytest.raises(SecurityError):
        write_task(tmp_root, "x")


def test_12b_ai_dir_is_junction_blocked(tmp_root):
    target_outside = tmp_root / "outside"
    target_outside.mkdir()
    link = tmp_root / ".ai"
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target_outside)], check=True, capture_output=True)
    except Exception:
        pytest.skip("junction creation not available")
    with pytest.raises(SecurityError):
        write_task(tmp_root, "x")


# ---------------------------------------------------------------------------
# 13. TASK_CURRENT.md is a symlink → BLOCK
# ---------------------------------------------------------------------------
def test_13_target_file_is_symlink_blocked(tmp_root):
    (tmp_root / ".ai").mkdir()
    real_file = tmp_root / "LEAKED"
    real_file.write_text("SECRET\n", encoding="utf-8")
    link = tmp_root / ".ai" / "TASK_CURRENT.md"
    try:
        os.symlink(str(real_file), str(link))
    except OSError:
        pytest.skip("symlink creation not available on this host")
    with pytest.raises(SecurityError):
        write_task(tmp_root, "x")
    # And the original secret must still be there, not replaced
    assert real_file.read_text(encoding="utf-8") == "SECRET\n"


# ---------------------------------------------------------------------------
# 14. TASK_CURRENT.md is a hard link → BLOCK
# ---------------------------------------------------------------------------
def test_14_target_file_is_hardlink_blocked(tmp_root):
    (tmp_root / ".ai").mkdir()
    real_file = tmp_root / "SECRET.txt"
    real_file.write_text("SECRET\n", encoding="utf-8")
    link = tmp_root / ".ai" / "TASK_CURRENT.md"
    try:
        os.link(str(real_file), str(link))
    except OSError:
        pytest.skip("hard link creation not available")
    with pytest.raises(SecurityError):
        write_task(tmp_root, "x")
    assert real_file.read_text(encoding="utf-8") == "SECRET\n"


# ---------------------------------------------------------------------------
# 15. Path escape must not happen (.. and similar)
# ---------------------------------------------------------------------------
def test_15_path_escape_via_dotdot_rejected(git_root):
    # Handled by validate_root — the root argument itself cannot escape
    with pytest.raises(SecurityError):
        write_task(git_root / "..", "x")


def test_15b_path_escape_via_dotdot_content_rejected(git_root):
    # Content can't change destination; only root matters
    # But if caller tries to give a root with .. it's blocked
    with pytest.raises(SecurityError):
        write_task(str(git_root) + "/../outside", "x")


def test_15c_unc_path_rejected():
    with pytest.raises(SecurityError):
        write_task(r"\\server\share\repo", "x")


# ---------------------------------------------------------------------------
# 16. write_task audit — no content leak
# ---------------------------------------------------------------------------
def test_16_service_audit_no_content_leak(git_root):
    """audit JSONL must NOT contain the task body plaintext, only summary."""
    from server.service import Service
    import tempfile as _tf
    import json as _json

    SECRET_BODY = "# Top secret task payload — DO NOT LEAK\n".rstrip() + "\n"
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with _tf.TemporaryDirectory(dir=str(base)) as svc_dir:
        svc = Service(Path(svc_dir))
        svc.call("write_task", {"root": str(git_root), "content": SECRET_BODY})
        audit_dir = svc.root / "logs"
        entries = sorted(audit_dir.glob("audit-*.jsonl"))
        assert entries
        # Scan every audit line — SECRET_BODY must never appear verbatim.
        for audit_file in entries:
            for line in audit_file.read_text(encoding="utf-8").splitlines():
                assert SECRET_BODY not in line, f"audit leak in {audit_file}"
                # Also confirm parameters section only has summary, no raw content
                log = _json.loads(line)
                params = log.get("parameters", {})
                for k, v in params.items():
                    assert not isinstance(v, str) or SECRET_BODY not in v, f"string param {k} leaked content"


# ---------------------------------------------------------------------------
# Gitignore strictness — positive cases
# ---------------------------------------------------------------------------
def test_gitignore_positive_exact(git_root):
    from adapters.handoff import _gitignore_has_ai
    assert _gitignore_has_ai(git_root) is True


def test_gitignore_positive_whitespace(tmp_root):
    from adapters.handoff import _gitignore_has_ai, _is_git_repo
    git(tmp_root, "init", "-q")
    (tmp_root / ".gitignore").write_text("   .ai/   \n", encoding="utf-8")
    git(tmp_root, "add", ".gitignore")
    git(tmp_root, "commit", "-qm", "init")
    assert _is_git_repo(tmp_root)
    assert _gitignore_has_ai(tmp_root) is True


# ---------------------------------------------------------------------------
# Gitignore strictness — negative cases (all must FAIL CLOSED)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("content,label", [
    ("# .ai/\n", "commented out"),
    ("!.ai/\n", "negation"),
    (".ai\n", "missing trailing slash"),
    (".ai/**\n", "glob"),
    (".airc\n", "similar prefix"),
    (".ai-other/\n", "hyphen suffix"),
    ("foo/.ai/\n", "nested path"),
    ("", "no gitignore"),
    ("*.log\n", "unrelated rule"),
])
def test_gitignore_negative_cases(tmp_root, content, label):
    from adapters.handoff import write_task as _wt, _is_git_repo
    git(tmp_root, "init", "-q")
    # Add a tiny tracked file so every test case has something to commit
    (tmp_root / "README.md").write_text("# sample\n", encoding="utf-8")
    if content:
        (tmp_root / ".gitignore").write_text(content, encoding="utf-8")
        git(tmp_root, "add", ".gitignore", "README.md")
    else:
        git(tmp_root, "add", "README.md")
    git(tmp_root, "commit", "-qm", f"init {label}")
    assert _is_git_repo(tmp_root)
    with pytest.raises(SecurityError):
        _wt(tmp_root, "x")


# ---------------------------------------------------------------------------
# 17. audit contains length and sha256 summary
# ---------------------------------------------------------------------------
def test_17_service_audit_has_summary(git_root):
    from server.service import Service
    import tempfile as _tf
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with _tf.TemporaryDirectory(dir=str(base)) as svc_dir:
        svc = Service(Path(svc_dir))
        payload = "# Task\n"
        expected_len = len(payload)
        expected_hash = hashlib.sha256(payload.encode()).hexdigest()[:12]
        svc.call("write_task", {"root": str(git_root), "content": payload})
        audit_dir = svc.root / "logs"
        entries = sorted(audit_dir.glob("audit-*.jsonl"))
        assert entries, "no audit file produced"
        last = entries[-1].read_text(encoding="utf-8").strip()
        import json as _json
        log = _json.loads(last.splitlines()[-1])
        args = log.get("parameters", {})
        assert "content_summary" in args
        cs = args["content_summary"]
        assert cs["length"] == expected_len
        assert cs["sha256"] == expected_hash


# ---------------------------------------------------------------------------
# 18. Other project files unchanged after write_task
# ---------------------------------------------------------------------------
def test_18_other_files_unchanged(git_root):
    (git_root / "src.py").write_text("print('ok')\n", encoding="utf-8")
    before = (git_root / "src.py").stat().st_mtime_ns
    write_task(git_root, "task")
    after = (git_root / "src.py").stat().st_mtime_ns
    assert before == after
    assert (git_root / "src.py").read_text(encoding="utf-8") == "print('ok')\n"


# ---------------------------------------------------------------------------
# 19. run_python real-project read-only still enforced
# ---------------------------------------------------------------------------
def test_19_run_python_read_only_unchanged(git_root):
    """write_task must not weaken run_python's project-root read-only guarantee."""
    from adapters.python_sandbox import run_python
    import tempfile as _tf

    with _tf.TemporaryDirectory() as svc_dir:
        sandbox = Path(svc_dir) / "sandbox"
        code = """
from pathlib import Path
try:
    Path("main.py").write_text("hacked")
    print("WRITE_OK")
except Exception as e:
    print(f"BLOCKED: {type(e).__name__}")
"""
        r = run_python(source=code, mode="analysis", project_root=git_root, sandbox_root=sandbox, timeout_seconds=10)
        assert r["status"] == "ok"
        assert "BLOCKED" in r.get("stdout", "")
        # Real file must be untouched
        assert not (git_root / "main.py").exists() or (git_root / "main.py").stat().st_size == 0


# ---------------------------------------------------------------------------
# 20. Existing read-only tools continue to pass
# ---------------------------------------------------------------------------
def test_20_read_tools_still_pass(git_root):
    from server.service import Service
    import tempfile as _tf
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with _tf.TemporaryDirectory(dir=str(base)) as svc_dir:
        svc = Service(Path(svc_dir))
        for tool, args in [
            ("list_directory", {"path": str(git_root), "depth": 1, "limit": 50}),
            ("project_overview", {"path": str(git_root), "depth": 1}),
        ]:
            r = svc.call(tool, args)
            assert r["status"] == "ok", f"{tool} failed: {r}"


# ---------------------------------------------------------------------------
# 21. MCP protocol discovers the public contract
# ---------------------------------------------------------------------------
def test_21_protocol_discovers_public_contract():
    import sys
    from pathlib import Path as _P
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "server.main"],
            cwd=str(_P(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=15) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [t.name for t in listed.tools]
                assert "work_update" in names
                assert len(names) == 16

    import asyncio
    asyncio.run(run())


# ---------------------------------------------------------------------------
# 22. Tool annotation correct
# ---------------------------------------------------------------------------
def test_22_tool_annotation_correct():
    import sys
    from pathlib import Path as _P
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "server.main"],
            cwd=str(_P(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=15) as session:
                await session.initialize()
                listed = await session.list_tools()
                wt = next(t for t in listed.tools if t.name == "work_update")
                assert wt.annotations.read_only_hint is False
                assert wt.annotations.destructive_hint is True
                assert wt.annotations.idempotent_hint is True
                assert wt.annotations.open_world_hint is False

    import asyncio
    asyncio.run(run())
