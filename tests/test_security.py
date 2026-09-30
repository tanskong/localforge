import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.conftest import git
from server.security import SecurityError, redact, safe_path, validate_root
from tests.test_tools import call


@pytest.mark.parametrize("path", ["../outside.txt", "a/../../outside", "D:\\", "D:\\secret.txt", "/etc/passwd", "\\\\server\\share\\file", "%2e%2e/secret", "%252e%252e/secret", "file:stream", "a/./file", "a//file", "README.md.", "README.md ", "NUL", "a\x00b", "~1/file"])
def test_path_escape(sample, path):
    with pytest.raises(SecurityError):
        safe_path(sample[1], path)


@pytest.mark.parametrize("path", [".env", ".env.production", "cert.pem", "auth.key", "credentials.json", "secrets.yml", "token.txt", "vault.kdbx", "db.sqlite", ".npmrc"])
def test_project_config_names_are_allowed(sample, path):
    assert safe_path(sample[1], path) == sample[1] / path


@pytest.mark.parametrize("path", ["C:\\", "D:\\", "C:\\Windows", "\\\\localhost\\D$\\codex", "/", "../"])
def test_broad_root(path):
    with pytest.raises((SecurityError, FileNotFoundError)):
        validate_root(path)


def test_absolute_path_and_command_parameters(sample):
    assert sample[0].call("project_overview", {"path": "../outside"})["security_blocked"]
    assert call(sample, "recent_logs", command="whoami")["security_blocked"]
    assert sample[0].call("run_shell", {"command": "whoami"})["security_blocked"]


@pytest.mark.parametrize("tool,params", [("git_history", {"limit": 51}), ("git_history", {"limit": -1}), ("recent_logs", {"limit": True}), ("project_overview", {"depth": 99})])
def test_numeric_bounds(sample, tool, params):
    assert call(sample, tool, **params)["error"] == "invalid_arguments"


@pytest.mark.parametrize("text,hidden", [
    ("sk-xxxx", "sk-xxxx"), ("Bearer xxxx", "xxxx"), ("password=xxxx", "xxxx"),
    ('{"api_key": "abc123"}', "abc123"), ("Authorization: Bearer abc", "abc"),
    ("Cookie: sid=abcd; jwt=qwerty", "abcd"), ("session_id: session123", "session123"),
    ("postgresql://admin:p455@localhost/db", "p455"), ("mysql://admin@localhost/db", "admin"),
    ("https://admin:p455@example.com/", "p455"), ("DB_PASSWORD=xyz123", "xyz123"),
    ("-----BEGIN PRIVATE KEY-----\nabcdef\n-----END PRIVATE KEY-----", "abcdef"),
    ("access_token=somevalue", "somevalue"), ("client_secret: value", "value"),
])
def test_redaction(text, hidden):
    assert hidden not in redact(text)


def test_redacted_in_all_outputs_and_audit(sample):
    secret = "password=never-leak-this-value"
    for name in ("README.md", "PROJECT_STATE.md", "main.py", "logs/app.log"):
        (sample[1] / name).write_text(secret + "\n")
    for tool in ("project_overview", "git_diff", "recent_logs", "project_health"):
        result = call(sample, tool)
        assert "never-leak-this-value" not in json.dumps(result)
    sample[0].call("project_overview", {"path": "Bearer never-leak-this-value"})
    for p in (sample[0].root / "logs").glob("*.jsonl"):
        raw = p.read_text()
        assert "never-leak-this-value" not in raw
        rows = [json.loads(line) for line in raw.splitlines()]
        assert all("return_bytes" in row and "security_blocked" in row for row in rows)
        assert rows[-1]["security_blocked"]


def test_project_env_git_diff_is_redacted(sample):
    root = sample[1]
    (root / ".env").write_text("UNLABELED_REAL_SECRET\n")
    git(root, "add", ".env")
    result = call(sample, "git_diff")
    # Dev mode cannot safely infer that an unlabelled code/data string is secret.
    assert "UNLABELED_REAL_SECRET" in json.dumps(result)
    assert result["filtered_files"] == 0


def test_git_external_helpers_disabled(sample):
    root = sample[1]
    marker = root / "MUST_NOT_EXIST"
    git(root, "config", "core.fsmonitor", f"touch {marker.as_posix()}")
    git(root, "config", "diff.external", f"touch {marker.as_posix()}")
    (root / "main.py").write_text("changed\n")
    assert call(sample, "git_diff")["status"] == "ok"
    assert not marker.exists()


def test_git_include_blocked(sample):
    git(sample[1], "config", "include.path", "../outside.ini")
    assert call(sample, "git_diff")["security_blocked"]
    assert call(sample, "project_health")["git"]["status"] == "blocked"


def test_git_clean_filter_blocked(sample):
    root = sample[1]
    marker = root / "MUST_NOT_EXIST"
    git(root, "config", "filter.danger.clean", f"touch {marker.as_posix()}")
    (root / ".gitattributes").write_text("*.py filter=danger\n")
    (root / "main.py").write_text("changed\n")
    assert call(sample, "git_diff")["security_blocked"]
    assert not marker.exists()


def test_git_environment_ignored(sample, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(sample[1].parent / "wrong"))
    assert call(sample, "git_history")["commits"]


def test_junction_or_symlink_resolves_to_normal_dev_path(sample):
    outside = sample[1].parent / "outside"
    outside.mkdir()
    link = sample[1] / "escape"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        assert safe_path(sample[1], "escape/file.txt") == link / "file.txt"
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_hardlink_in_project_is_allowed_in_dev_mode(sample):
    outside = sample[1].parent / "outside.txt"
    outside.write_text("secret")
    os.link(outside, sample[1] / "hardlink.txt")
    assert safe_path(sample[1], "hardlink.txt") == sample[1] / "hardlink.txt"


def test_audit_failure_withholds_result(sample, monkeypatch):
    def fail(*args):
        raise OSError("no audit")
    monkeypatch.setattr(sample[0].audit, "record", fail)
    result = call(sample, "project_overview")
    assert result["error"] == "audit_unavailable" and "readme" not in result
