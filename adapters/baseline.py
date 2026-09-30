"""Git baseline snapshots with per-stage diagnostics."""
from __future__ import annotations
import hashlib, json, os, subprocess, time, uuid
from pathlib import Path
from adapters.git import GitReader

def _env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_PREFIX"):
        env.pop(key, None)
    env.update({"GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":os.devnull, "GIT_OPTIONAL_LOCKS":"0", "GIT_TERMINAL_PROMPT":"0", "GIT_PAGER":""})
    return env

def _git(root: Path, executable: str, *args: str) -> str:
    return subprocess.run([executable, "-C", str(root), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=10, env=_env(), creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).stdout

def _status(root: Path, executable: str) -> list[dict]:
    chunks=[x for x in _git(root, executable, "status", "--porcelain=v1", "-z").split("\0") if x]; rows=[]; index=0
    while index < len(chunks):
        item=chunks[index]; index += 1; row={"code":item[:2],"path":item[3:]}
        if ("R" in row["code"] or "C" in row["code"]) and index < len(chunks): row["old_path"] = chunks[index]; index += 1
        rows.append(row)
    return rows

def _snapshot(root: Path, executable: str) -> list[dict]:
    rows = _status(root, executable)
    for row in rows:
        path = root / row["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        row["fingerprint"] = f"{row['code']}:{digest}"
    return rows

def capture(state_root: Path, root: Path) -> dict:
    started=time.perf_counter(); timings={"enter_capture":0}; stage="enter_capture"
    try:
        stage="resolve_git"; reader=GitReader(root); executable=reader.executable
        if not executable: raise RuntimeError("git_not_installed")
        timings[stage]=round((time.perf_counter()-started)*1000)
        stage="rev_parse"; head=_git(root, executable, "rev-parse", "HEAD").strip(); timings[stage]=round((time.perf_counter()-started)*1000)
        stage="git_status"; dirty=_snapshot(root, executable); timings[stage]=round((time.perf_counter()-started)*1000)
        stage="snapshot_write"; ident=uuid.uuid4().hex; directory=state_root/"baselines"; directory.mkdir(parents=True,exist_ok=True)
        (directory/(ident+".json")).write_text(json.dumps({"id":ident,"root":str(root),"head":head,"dirty":dirty}),encoding="utf-8")
        timings[stage]=round((time.perf_counter()-started)*1000)
        return {"status":"ok","snapshot_id":ident,"head":head,"pre_existing_dirty":dirty,"timings_ms":{**timings,"total":round((time.perf_counter()-started)*1000)}}
    except Exception as exc:
        return {"status":"error","error":"baseline_failed","stage":stage,"message":f"{type(exc).__name__}: {exc}","timings_ms":{**timings,"total":round((time.perf_counter()-started)*1000)}}

def compare(state_root: Path, root: Path, snapshot_id: str) -> dict:
    file=state_root/"baselines"/(snapshot_id+".json")
    if not file.is_file(): return {"status":"not_found","snapshot_id":snapshot_id}
    old=json.loads(file.read_text(encoding="utf-8")); executable=GitReader(root).executable
    now=_snapshot(root, executable); head=_git(root, executable, "rev-parse", "HEAD").strip(); prior={x["path"]:x for x in old["dirty"]}; current={x["path"]:x for x in now}
    changed=[x for path, x in current.items() if path not in prior or x["fingerprint"] != prior[path].get("fingerprint")]
    removed=[x for path, x in prior.items() if path not in current]
    return {"status":"ok","snapshot_id":snapshot_id,"head_before":old["head"],"head_now":head,"head_changed":head!=old["head"],"pre_existing_dirty":old["dirty"],"new_dirty":changed,"removed_pre_existing_dirty":removed,"status_changes":[{"path":p,"before":prior[p]["code"],"now":current[p]["code"]} for p in prior.keys()&current.keys() if prior[p]["code"]!=current[p]["code"]],"baseline_preserved":not changed and not removed}
