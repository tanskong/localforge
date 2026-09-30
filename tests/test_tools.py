import json
from pathlib import Path
import pytest

from tests.conftest import git
from server.security import MAX_DIFF, MAX_RESULT


def call(sample, tool, **kwargs):
    root = sample[1]
    if tool == "run_python":
        return sample[0].call(tool, {"root": str(root), **kwargs})
    if tool == "read_file":
        value = kwargs.pop("path")
        path = Path(value) if Path(value).is_absolute() else root / value
    elif tool in {"list_directory", "search_text"}:
        value = kwargs.pop("path", None)
        path = root if value in (None, ".") else root / value
    else:
        path = root
    return sample[0].call(tool, {"path": str(path), **kwargs})


def test_overview(sample):
    result = call(sample, "project_overview")
    assert result["status"] == "ok"
    assert result["preferred_context"] == "state_files"
    assert "Example" in result["readme"]["text"]
    assert not result["git"]["dirty"]
    assert result["git"]["latest_commit"]["message"] == "initial sample"


def test_history(sample):
    result = call(sample, "git_history")
    assert result["commits"][0]["message"] == "initial sample"
    assert result["commits"][0]["file_count"] == 5


def test_staged_unstaged_diff(sample):
    root = sample[1]
    (root / "main.py").write_text("print('second')\n", encoding="utf-8")
    git(root, "add", "main.py")
    (root / "main.py").write_text("print('third')\n", encoding="utf-8")
    result = call(sample, "git_diff")
    assert "+print('second')" in result["staged"][0]["diff"]
    assert "+print('third')" in result["unstaged"][0]["diff"]


def test_recent_changes(sample):
    result = call(sample, "recent_changes")
    assert result["commit_count"] == 1
    assert next(f for f in result["files"] if f["path"] == "main.py")["added_lines"] == 1
    assert result["modules"]


def test_report(sample):
    result = call(sample, "test_status")
    assert result["status"] == "passed" and result["latest"]["tests"] == 2
    assert result["latest"]["age_seconds"] >= 0


def test_logs(sample):
    result = call(sample, "recent_logs", limit=1)
    assert result["status"] == "ok" and result["error_signals_found"]
    assert "INFO" not in result["files"][0]["text"]
    assert result["truncated"]


def test_health(sample):
    result = call(sample, "project_health")
    assert result["git"]["status"] == "ok"
    assert "recent_log_error_signals" in result["warnings"]


def test_missing_sources(sample):
    (sample[1] / "logs/app.log").unlink()
    (sample[1] / "logs").rmdir()
    (sample[1] / "junit.xml").unlink()
    (sample[1] / "PROJECT_STATE.md").unlink()
    assert call(sample, "recent_logs")["status"] == "unavailable"
    assert call(sample, "test_status")["status"] == "no_report"
    assert "project_state_not_found" in call(sample, "project_health")["warnings"]


def test_non_git_directory(sample):
    root = sample[1].parent / "plain"
    root.mkdir()
    for tool in ("project_overview", "git_history", "git_diff", "recent_changes", "project_health"):
        result = sample[0].call(tool, {"path": str(root)})
        assert result["status"] != "error"


def test_large_diff(sample):
    (sample[1] / "main.py").write_text("\n".join(f"print('line {i} some substantial changed content')" for i in range(9000)))
    result = call(sample, "git_diff")
    assert result["truncated"]
    assert sum(len(i["diff"]) for key in ("staged", "unstaged") for i in result[key]) <= MAX_DIFF
    assert len(json.dumps(result).encode()) <= MAX_RESULT


def test_large_logs(sample):
    (sample[1] / "logs/app.log").write_text("INFO a long ordinary log line\n" * 20000 + "ERROR last error\n")
    result = call(sample, "recent_logs")
    assert result["truncated"] and "last error" in result["files"][0]["text"]
    assert len(result["files"][0]["text"].splitlines()) <= 100


@pytest.mark.parametrize("payload,expected", [
    ('<testsuite tests="2" failures="1"/>', "failed"),
    ('<testsuite tests="0"/>', "unknown"),
    ('<testsuite tests="2" skipped="2"/>', "unknown"),
    ('<testsuites tests="2" failures="0"/>', "passed"),
    ('<testsuite tests="-3"/>', "unknown"),
    ('invalid', "unknown"),
    ('<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><testsuite>&a;</testsuite>', "unknown"),
    ('<testsuites><testsuite tests="1"><testsuite tests="1"/></testsuite></testsuites>', "passed"),
])
def test_report_variants(sample, payload, expected):
    (sample[1] / "junit.xml").write_text(payload)
    assert call(sample, "test_status")["status"] == expected


def test_json_report(sample):
    (sample[1] / "report.json").write_text('{"summary":{"total":3,"passed":2,"failed":1}}')
    assert call(sample, "test_status")["status"] == "failed"


def test_empty_repo(sample):
    root = sample[1].parent / "empty"
    root.mkdir()
    git(root, "init", "-q")
    result = sample[0].call("project_overview", {"path": str(root)})
    assert result["git"]["status"] == "ok" and result["git"]["latest_commit"] is None


def test_tree_bounded(sample):
    for i in range(190):
        (sample[1] / f"file-{i}.txt").write_text("example")
    result = call(sample, "project_overview")
    assert not result["truncated"] and len(result["structure"]["entries"]) >= 190


def test_read_only_files_unchanged(sample):
    import hashlib
    root = sample[1]
    def snapshot():
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    before = snapshot()
    for tool in ("project_overview", "git_history", "git_diff", "recent_changes", "test_status", "recent_logs", "project_health", "list_directory", "read_file", "search_text"):
        if tool == "read_file":
            call(sample, tool, path="main.py")
            continue
        if tool == "search_text":
            call(sample, tool, query="print")
            continue
        call(sample, tool)
    assert before == snapshot()
