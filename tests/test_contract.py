import sys

from adapters.development import apply_changes
from server.main import _validate


def test_apply_changes_create_update_delete_and_conflict(sample):
    service, root = sample
    created = apply_changes(root, [{"path": "new.py", "operation": "create", "content": "x = 1\n"}])
    assert created["status"] == "ok"
    updated = apply_changes(root, [{"path": "new.py", "operation": "update", "content": "x = 2\n", "expected": "x = 1\n"}])
    assert updated["status"] == "ok" and (root / "new.py").read_text() == "x = 2\n"
    conflict = apply_changes(root, [{"path": "new.py", "operation": "update", "content": "x = 3\n", "expected": "old\n"}])
    assert conflict["status"] == "conflict" and conflict["retryable"]
    deleted = apply_changes(root, [{"path": "new.py", "operation": "delete"}])
    assert deleted["status"] == "ok" and not (root / "new.py").exists()


def test_shell_process_and_work_update_contract(sample):
    service, root = sample
    result = service.call("run_process", {"root": str(root), "executable": sys.executable,
                                           "args": ["-c", "print('ok')"], "timeout_seconds": 5})
    assert result["status"] == "ok" and result["exit_code"] == 0
    (root / ".gitignore").write_text(".ai/\n")
    work = service.call("work_update", {"root": str(root), "content": "# Task\n", "target": "task"})
    assert work["status"] == "ok" and (root / ".ai" / "TASK_CURRENT.md").exists()


def test_git_status_and_public_aliases(sample):
    service, root = sample
    status = service.call("git_status", {"path": str(root)})
    assert status["status"] == "ok" and status["branch"]


def test_validate_captures_and_passes_scope(sample):
    service, root = sample
    result = _validate(service, str(root), [f'"{sys.executable}" -c "print(\'ok\')"'], ["**"], [], 5)
    assert result["status"] == "ok"
    assert result["commands"]["passed"] and result["scope"]["passed"]


def test_validate_propagates_missing_and_out_of_scope(sample):
    service, root = sample
    out = _validate(service, str(root), [f'"{sys.executable}" -c "open(\'unexpected.txt\', \'w\').write(\'x\')"'], ["src/**"], [], 5)
    assert out["status"] != "ok" and out["scope"]["reason"] == "out_of_scope_changes"


def test_validate_checks_preexisting_working_tree_scope(sample):
    service, root = sample
    (root / "unexpected.txt").write_text("already dirty\n")
    result = _validate(service, str(root), [f'"{sys.executable}" -c "print(123)"'], ["src/**"], [], 5)
    assert result["status"] == "ok"
    assert result["scope"]["passed"] is True
    assert "unexpected.txt" in [x["path"] for x in result["scope"]["pre_existing_dirty"]]


def test_validate_propagates_command_failure(sample):
    service, root = sample
    result = _validate(service, str(root), [f'"{sys.executable}" -c "raise SystemExit(3)"'], ["**"], [], 5)
    assert result["status"] != "ok" and not result["commands"]["passed"]

def test_shell_nonzero_is_failed(sample):
    service, root = sample
    result = service.call("run_process", {"root": str(root), "executable": sys.executable,
                                           "args": ["-c", "raise SystemExit(7)"], "timeout_seconds": 5})
    assert result["status"] == "failed" and result["exit_code"] == 7

def test_validate_detects_preexisting_file_modified_again(sample):
    service, root = sample
    (root / "unexpected.txt").write_text("before\n")
    result = _validate(service, str(root), [f'"{sys.executable}" -c "open(\'unexpected.txt\', \'a\').write(\'after\\n\')"'], ["src/**"], [], 5)
    assert result["status"] != "ok" and "unexpected.txt" in result["scope"]["out_of_scope_changes"]

def test_apply_patch_preserves_newlines_and_git_is_blocked(sample):
    service, root = sample
    lf = root / "lf.txt"; lf.write_bytes(b"one\ntwo\n")
    assert service.call("apply_patch", {"root": str(root), "path": "lf.txt", "old": "one", "new": "ONE"})["status"] == "ok"
    assert b"\r\n" not in lf.read_bytes()
    crlf = root / "crlf.txt"; crlf.write_bytes(b"one\r\ntwo\r\n")
    assert service.call("apply_patch", {"root": str(root), "path": "crlf.txt", "old": "one", "new": "ONE"})["status"] == "ok"
    assert crlf.read_bytes() == b"ONE\r\ntwo\r\n"
    assert service.call("read_file", {"path": str(root / ".git" / "config")})["security_blocked"] is True
    assert service.call("apply_changes", {"root": str(root), "changes": [{"path": ".git/x", "operation": "create", "content": "x"}]})["security_blocked"] is True

def test_stop_process_returns_watched_output(sample):
    service, root = sample
    bg = service.call("run_process", {"root": str(root), "executable": sys.executable,
        "args": ["-c", "import sys,time; print('flushed', flush=True); time.sleep(30)"], "background": True})
    import time; time.sleep(1)
    stopped = service.call("stop_process", {"process_id": bg["process_id"]})
    assert stopped["status"] == "stopped" and "flushed" in stopped["stdout"]
