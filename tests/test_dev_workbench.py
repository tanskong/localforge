from pathlib import Path
import subprocess
import sys
import time
from adapters.baseline import capture

from tests.conftest import git

def call(service, root, tool, **args):
    key = "root" if tool in {"write_file","apply_patch","move_path","delete_path","run_process","capture_git_baseline","compare_git_baseline","verify_change_scope","symbol_search","read_current_task","read_current_result","task_status","run_python"} else "path"
    return service.call(tool, {key: str(root), **args})

def test_file_lifecycle_process_python_and_symbols(sample):
    service, root = sample
    assert call(service, root, "write_file", path="a.py", content="def start_course():\n return 1\n")["status"] == "ok"
    assert call(service, root, "apply_patch", path="a.py", old="return 1", new="return 2")["status"] == "ok"
    assert call(service, root, "move_path", source="a.py", destination="b.py")["status"] == "ok"
    assert call(service, root, "symbol_search", symbol="start_course")["results"][0]["kind"] == "definition"
    py = call(service, root, "run_python", code="import b\nprint(b.start_course())", mode="dev")
    assert py["status"] == "ok" and "2" in py["stdout"]
    cmd = call(service, root, "run_process", executable=sys.executable, args=["-c", "import os; print(os.getcwd())"], env={"WORKBENCH_TEST":"yes"})
    assert cmd["status"] == "ok" and str(root) in cmd["stdout"]
    assert call(service, root, "delete_path", path="b.py")["deleted"]

def test_background_baseline_git_and_scope(sample):
    service, root = sample
    baseline = call(service, root, "capture_git_baseline")["snapshot_id"]
    call(service, root, "write_file", path="new.py", content="def added(): pass\n")
    bg = call(service, root, "run_process", executable=sys.executable, args=["-c", "import time; time.sleep(30)"], background=True)
    assert service.call("process_status", {"process_id":bg["process_id"]})["status"] == "running"
    assert service.call("stop_process", {"process_id":bg["process_id"]})["status"] == "stopped"
    compared = call(service, root, "compare_git_baseline", snapshot_id=baseline)
    assert any(x["path"] == "new.py" for x in compared["new_dirty"])
    scope = call(service, root, "verify_change_scope", snapshot_id=baseline, allowed_patterns=["src/**"], denied_patterns=[])
    assert "new.py" in scope["out_of_scope_changes"]
    git(root, "add", "new.py"); git(root, "commit", "-qm", "add function")
    shown = service.call("git_show", {"path":str(root),"revision":"HEAD"})
    assert "added" in shown["patch"]
    comparison = service.call("git_compare", {"path":str(root),"base":"HEAD~1","head":"HEAD"})
    assert comparison["changed_files"] == ["new.py"]
    assert service.call("git_blame", {"path":str(root),"file":"new.py"})["lines"]
    assert service.call("changed_symbols", {"path":str(root),"base":"HEAD~1","head":"HEAD"})["files"][0]["symbols"] == ["added"]

def test_project_env_and_redaction_are_precise(sample):
    service, root = sample
    call(service, root, "write_file", path=".env", content='API_KEY="abc123"\n')
    assert "abc123" not in service.call("read_file", {"path":str(root / ".env")})["content"]
    call(service, root, "write_file", path="tokenizer.py", content="def test_login_failure(): pass\nclass CredentialSchema: pass\n")
    text=service.call("read_file", {"path":str(root / "tokenizer.py")})["content"]
    assert "test_login_failure" in text and "CredentialSchema" in text

def test_two_repo_real_smoke(sample):
    service, repo_a = sample
    repo_b = repo_a.parent / "repo-b"; repo_b.mkdir(); git(repo_b, "init", "-q")
    call(service, repo_b, "write_file", path="shared.py", content="def shared(): return 'B'\n")
    git(repo_b, "add", "."); git(repo_b, "commit", "-qm", "initial B")
    assert service.call("search_text", {"path":str(repo_b),"query":"shared"})["returned"]
    baseline=call(service, repo_a, "capture_git_baseline")["snapshot_id"]
    call(service, repo_a, "write_file", path="consumer.py", content="def use(): return 'B'\n")
    failed=call(service, repo_a, "run_process", executable=sys.executable, args=["-c","raise SystemExit(3)"])
    assert failed["exit_code"] == 3
    passed=call(service, repo_a, "run_process", executable=sys.executable, args=["-c","print('pytest-style success')"])
    assert passed["exit_code"] == 0
    assert call(service, repo_a, "compare_git_baseline", snapshot_id=baseline)["new_dirty"]

def test_baseline_tracks_rename_and_delete(sample):
    service, root = sample
    call(service, root, "write_file", path="old.py", content="x=1\n"); git(root, "add", "old.py"); git(root, "commit", "-qm", "old")
    baseline=call(service, root, "capture_git_baseline")["snapshot_id"]
    call(service, root, "move_path", source="old.py", destination="new.py"); git(root, "add", "-A")
    row=call(service, root, "compare_git_baseline", snapshot_id=baseline)["new_dirty"][0]
    assert row["path"] == "new.py" and row["old_path"] == "old.py"

def test_sample_path_is_not_a_sam_credential_store(sample):
    service, root = sample
    call(service, root, "write_file", path="src/sample.py", content="print('ok')\n")
    git(root, "add", "src/sample.py"); git(root, "commit", "-qm", "sample")
    assert service.call("read_file", {"path":str(root / "src/sample.py")})["status"] == "ok"
    assert service.call("git_blame", {"path":str(root),"file":"src/sample.py"})["status"] == "ok"

def test_direct_baseline_benchmark_under_two_seconds(sample):
    service, root = sample
    started=time.perf_counter(); result=capture(service.root, root)
    assert result["status"] == "ok", result
    assert result["timings_ms"]["total"] < 2000
    assert (time.perf_counter()-started) < 2

def test_git_show_and_compare_decode_utf8_commit_and_patch(sample):
    service, root = sample
    call(service, root, "write_file", path="中文.py", content="VALUE = '中文内容'\n")
    git(root, "add", "."); git(root, "commit", "-m", "中文提交")
    shown = service.call("git_show", {"path":str(root),"revision":"HEAD"})
    compared = service.call("git_compare", {"path":str(root),"base":"HEAD~1","head":"HEAD"})
    assert shown["status"] == "ok" and "中文提交" in shown["metadata"]["message"]
    assert compared["status"] == "ok" and "中文内容" in compared["patch"]
