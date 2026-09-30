import json
import os
import subprocess

import pytest

import adapters.files as file_adapter
from tests.test_tools import call


def test_list_directory_depth_limit_and_exclusions(sample):
    root = sample[1]
    (root / "src/deep").mkdir(parents=True)
    (root / "src/app.py").write_text("print('app')\n")
    (root / "src/deep/hidden_by_depth.py").write_text("pass\n")
    (root / ".venv").mkdir()
    (root / ".venv/secret.py").write_text("pass\n")
    result = call(sample, "list_directory", depth=2, limit=200)
    paths = {entry["path"] for entry in result["entries"]}
    assert "src" in paths and "src/app.py" in paths and "src/deep" in paths
    assert "src/deep/hidden_by_depth.py" not in paths
    assert not any(path.startswith((".git", ".venv")) for path in paths)
    assert result["filtered_entries"] >= 2 and result["security_blocked"]
    limited = call(sample, "list_directory", limit=2)
    assert limited["returned"] == 2 and limited["truncated"]


def test_list_directory_nested_path(sample):
    root = sample[1]
    (root / "src").mkdir()
    (root / "src/app.py").write_text("pass\n")
    result = call(sample, "list_directory", path="src", depth=1)
    assert [entry["path"] for entry in result["entries"]] == ["app.py"]


@pytest.mark.parametrize("path", ["../", "D:\\"])
def test_file_tools_reject_path_escape(sample, path):
    for tool, extra in (("list_directory", {}), ("read_file", {}), ("search_text", {"query": "x"})):
        assert call(sample, tool, path=path, **extra)["security_blocked"]


def test_nonexistent_absolute_path(sample):
    missing = sample[1] / "missing"
    assert sample[0].call("list_directory", {"path": str(missing)})["status"] == "not_found"
    assert sample[0].call("read_file", {"path": str(missing / "file.txt")})["status"] == "not_found"


def test_file_tools_require_absolute_paths(sample):
    for tool, args in (
        ("list_directory", {}),
        ("read_file", {"path": "main.py"}),
        ("search_text", {"query": "print"}),
    ):
        result = sample[0].call(tool, {"path": "missing", **args})
        assert result["security_blocked"]


def test_list_filters_junction_or_symlink(sample):
    outside = sample[1].parent / "outside-list"
    outside.mkdir()
    (outside / "outside.py").write_text("do_not_list\n")
    link = sample[1] / "escape-list"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        result = call(sample, "list_directory")
        assert any("escape-list" in entry["path"] for entry in result["entries"])
        # Dev mode resolves a normal junction and permits cross-project work.
        assert not call(sample, "list_directory", path="escape-list")["security_blocked"]
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_read_file_formats_and_ranges(sample):
    root = sample[1]
    (root / "notes.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (root / "settings.yaml").write_text("enabled: true\n", encoding="utf-8")
    assert "print" in call(sample, "read_file", path="main.py")["content"]
    assert "enabled" in call(sample, "read_file", path="settings.yaml")["content"]
    ranged = call(sample, "read_file", path="notes.md", start_line=2, end_line=3)
    assert ranged["content"] == "two\nthree"
    assert ranged["total_lines"] == 4 and ranged["returned_line_range"] == [2, 3]


def test_read_file_large_and_binary(sample):
    root = sample[1]
    (root / "large.txt").write_text("ordinary line\n" * 5000)
    large = call(sample, "read_file", path="large.txt")
    assert large["truncated"] and large["next_start_line"]
    assert len(large["content"]) <= file_adapter.MAX_SOURCE_TEXT
    (root / "binary.txt").write_bytes(b"abc\x00def")
    assert call(sample, "read_file", path="binary.txt")["status"] == "unsupported_binary"
    (root / "too-large.txt").write_bytes(b"x" * (file_adapter.MAX_SOURCE_FILE + 1))
    too_large = call(sample, "read_file", path="too-large.txt")
    assert too_large["status"] == "file_too_large" and too_large["truncated"]


@pytest.mark.parametrize("path", [".env", ".env.production", "private.key", "credentials.json", "token.txt"])
def test_read_file_allows_project_config_but_redacts_values(sample, path):
    (sample[1] / path).write_text("unlabeled-secret-value\n")
    result = call(sample, "read_file", path=path)
    assert result["status"] == "ok"
    assert "unlabeled-secret-value" in json.dumps(result)


def test_read_file_redacts(sample):
    (sample[1] / "safe.md").write_text("password=never-show-this\nAuthorization: Bearer abc123\n")
    result = call(sample, "read_file", path="safe.md")
    assert "never-show-this" not in result["content"] and "abc123" not in result["content"]


def test_search_project_text_results_limits_and_glob(sample):
    root = sample[1]
    (root / "src").mkdir()
    (root / "src/one.py").write_text("Needle first\nneedle second\n")
    (root / "src/two.md").write_text("needle docs\n")
    result = call(sample, "search_text", query="needle", path="src", file_glob="*.py", max_results=1)
    assert result["returned"] == 1 and result["truncated"]
    assert result["results"][0]["path"] == "one.py" and result["results"][0]["line"] == 1


def test_search_excludes_sensitive_dependencies_and_binary(sample):
    root = sample[1]
    (root / ".env").write_text("needle-sensitive\n")
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "node_modules/pkg/index.js").write_text("needle-dependency\n")
    (root / "binary.txt").write_bytes(b"needle\x00binary")
    (root / "main.py").write_text("needle-safe\n")
    result = call(sample, "search_text", query="needle")
    dump = json.dumps(result)
    assert result["returned"] == 2 and result["results"][0]["path"] == ".env"
    assert "needle-sensitive" in dump and "needle-dependency" not in dump and "binary" not in dump
    assert result["skipped_files"] == 1


def test_search_redacts_and_bounds_scan(sample, monkeypatch):
    root = sample[1]
    (root / "match.txt").write_text("password=needle-secret-value\n")
    result = call(sample, "search_text", query="password")
    assert "needle-secret-value" not in json.dumps(result)
    for index in range(10):
        (root / f"scan-{index}.txt").write_text("no match\n")
    monkeypatch.setattr(file_adapter, "SCAN_LIMIT", 5)
    bounded = call(sample, "search_text", query="absent")
    assert bounded["truncated"] and bounded["scanned_files"] <= 5


def test_new_tool_audit_uses_summaries(sample):
    call(sample, "search_text", query="do-not-log-raw-query", path=".")
    logs = list((sample[0].root / "logs").glob("*.jsonl"))
    row = json.loads(logs[0].read_text().splitlines()[-1])
    serialized = json.dumps(row)
    assert "do-not-log-raw-query" not in serialized
    assert "query_summary" in row["parameters"] and row["parameters"]["path"] == str(sample[1])
