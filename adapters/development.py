"""Direct local-development operations; intentionally not a sandbox."""
from __future__ import annotations
import ast, difflib, os, shutil, stat, subprocess, sys, threading, time, uuid
from pathlib import Path
from typing import Any
from server.security import MAX_RESULT, SecurityError, protected_location, redact, relative_parts, safe_path

_processes: dict[str, subprocess.Popen] = {}
_process_results: dict[str, dict] = {}
_process_locks: dict[str, threading.Lock] = {}
_lock = threading.Lock()
_MAX_PROCESS_RESULTS = 64

def target(root: Path, relative: str) -> Path: return safe_path(root, relative)

def write_file(root: Path, path: str, content: str, append: bool = False) -> dict:
    item = target(root, path); item.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"; existed = item.exists()
    with item.open(mode, encoding="utf-8", newline="") as f: f.write(content)
    return {"status":"ok", "path":path.replace('\\','/'), "operation":"updated" if existed else "created", "bytes":len(content.encode())}

def apply_patch(root: Path, path: str, old: str, new: str, count: int = 1) -> dict:
    item = target(root, path)
    text = item.read_text(encoding="utf-8")
    actual = text.count(old)
    if not old or actual < count: return {"status":"error", "error":"patch_context_not_found", "matches":actual}
    raw = item.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    item.write_bytes(text.replace(old, new, count).replace("\n", newline).encode("utf-8"))
    return {"status":"ok", "path":path.replace('\\','/'), "replacements":count}

def apply_changes(root: Path, changes: list[dict]) -> dict:
    """Apply a small preflighted batch of create/update/delete file changes."""
    if not isinstance(changes, list) or not changes or len(changes) > 50:
        return {"status":"error", "error":"invalid_changes"}
    prepared = []
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str):
            return {"status":"error", "error":"invalid_change"}
        path = change["path"]
        item = target(root, path)
        operation = change.get("operation", "update")
        if operation not in {"create", "update", "delete"}:
            return {"status":"error", "error":"invalid_change_operation", "path":path}
        if operation == "create" and item.exists():
            return {"status":"conflict", "error":"path_exists", "path":path, "retryable":True}
        if operation in {"update", "delete"} and not item.is_file():
            return {"status":"conflict", "error":"path_not_found", "path":path, "retryable":True}
        if operation == "update":
            content = change.get("content")
            if not isinstance(content, str):
                return {"status":"error", "error":"content_required", "path":path}
            expected = change.get("expected")
            if expected is not None and item.read_text(encoding="utf-8") != expected:
                return {"status":"conflict", "error":"content_changed", "path":path, "retryable":True}
        if operation == "create" and not isinstance(change.get("content"), str):
            return {"status":"error", "error":"content_required", "path":path}
        prepared.append((item, path, operation, change.get("content", "")))
    for item, path, operation, content in prepared:
        if operation == "delete": item.unlink()
        else:
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text(content, encoding="utf-8")
    return {"status":"ok", "changed": [{"path":p.replace('\\','/'), "operation":op} for _, p, op, _ in prepared]}

def move_path(root: Path, source: str, destination: str) -> dict:
    src, dst = target(root, source), target(root, destination)
    if not src.exists(): return {"status":"not_found", "path":source}
    dst.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(src), str(dst))
    return {"status":"ok", "source":source.replace('\\','/'), "destination":destination.replace('\\','/')}

def delete_path(root: Path, path: str, recursive: bool = False) -> dict:
    item = target(root, path)
    if not item.exists(): return {"status":"not_found", "path":path}
    if item.is_dir():
        if not recursive and any(item.iterdir()): return {"status":"error", "error":"directory_not_empty"}
        if recursive:
            # Git object files can be readonly on Windows after external tooling.
            def retry(func, value, exc):
                os.chmod(value, stat.S_IWRITE)
                func(value)
            shutil.rmtree(item, onexc=retry)
        else: item.rmdir()
    else: item.unlink()
    return {"status":"ok", "path":path.replace('\\','/'), "deleted":True}

def _output(proc: subprocess.Popen, timeout: int) -> dict:
    start=time.perf_counter()
    try: out, err=proc.communicate(timeout=timeout); status="ok"
    except subprocess.TimeoutExpired:
        proc.kill(); out,err=proc.communicate(); status="timeout"
    if status == "ok" and proc.returncode != 0: status = "failed"
    cut = len(out)+len(err)>MAX_RESULT
    return {"status":status, "exit_code":proc.returncode, "stdout":redact(out[:MAX_RESULT//2]), "stderr":redact(err[:MAX_RESULT//2]), "truncated":cut, "duration_ms":int((time.perf_counter()-start)*1000)}

def _background_output(proc: subprocess.Popen) -> dict:
    start = time.perf_counter(); streams = {"stdout": [], "stderr": []}; sizes = {"stdout": 0, "stderr": 0}; truncated = False
    def collect(name: str, stream) -> None:
        nonlocal truncated
        while chunk := stream.read(8192):
            keep = ""
            if sizes[name] < MAX_RESULT // 2:
                keep = chunk[:MAX_RESULT // 2 - sizes[name]]
                streams[name].append(keep); sizes[name] += len(keep)
            if len(chunk) > len(keep):
                truncated = True
    threads = [threading.Thread(target=collect, args=(name, getattr(proc, name)), daemon=True) for name in streams]
    for thread in threads: thread.start()
    proc.wait()
    for thread in threads: thread.join()
    return {"status": "exited", "exit_code": proc.returncode,
            "stdout": redact("".join(streams["stdout"])), "stderr": redact("".join(streams["stderr"])),
            "truncated": truncated, "duration_ms": int((time.perf_counter() - start) * 1000)}

def _remember_result(process_id: str, result: dict) -> None:
    with _lock:
        _process_results[process_id] = result
        _processes.pop(process_id, None)
        _process_locks.pop(process_id, None)
        while len(_process_results) > _MAX_PROCESS_RESULTS:
            _process_results.pop(next(iter(_process_results)))

def run_process(root: Path, executable: str, args: list[str] | None, timeout_seconds: int, env: dict[str,str] | None, shell: bool=False, background: bool=False, command: str | None=None) -> dict:
    if not executable and not command: raise ValueError("executable or command required")
    work = root
    inherited=os.environ.copy(); inherited.update(env or {})
    invocation = command if shell else [executable, *(args or [])]
    kwargs: dict[str,Any]={"cwd":str(work), "env":inherited, "stdin":subprocess.DEVNULL, "text":True, "shell":shell, "stdout":subprocess.PIPE, "stderr":subprocess.PIPE, "creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
    proc=subprocess.Popen(invocation, **kwargs)
    if background:
        ident=uuid.uuid4().hex[:12]
        with _lock:
            _processes[ident]=proc
            _process_locks[ident]=threading.Lock()
        threading.Thread(target=_watch_process, args=(ident, proc), daemon=True).start()
        return {"status":"started", "process_id":ident, "pid":proc.pid, "cwd":str(root), "command":command if shell else [executable,*(args or [])], "started_at":time.time()}
    return _output(proc, timeout_seconds)

def process_status(process_id: str) -> dict:
    with _lock:
        proc=_processes.get(process_id)
        result=_process_results.get(process_id)
    if result:
        return result.copy()
    if not proc: return {"status":"not_found", "process_id":process_id}
    code=proc.poll(); return {"status":"running" if code is None else "exited", "process_id":process_id,"pid":proc.pid,"exit_code":code}

def stop_process(process_id: str) -> dict:
    with _lock:
        proc=_processes.get(process_id)
        result=_process_results.get(process_id)
        process_lock=_process_locks.get(process_id)
    if result:
        return result.copy()
    if not proc: return {"status":"not_found", "process_id":process_id}
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
    deadline = time.time() + 5
    while time.time() < deadline:
        with _lock: result = _process_results.get(process_id)
        if result is not None: break
        time.sleep(0.01)
    result = result or {"status":"stopped", "exit_code":proc.returncode, "stdout":"", "stderr":"", "truncated":False, "duration_ms":0}
    result = {**result, "status":"stopped", "process_id":process_id, "pid":proc.pid}
    _remember_result(process_id, result)
    return result.copy()

def _watch_process(process_id: str, proc: subprocess.Popen) -> None:
    with _lock: process_lock=_process_locks[process_id]
    with process_lock:
        result=_background_output(proc)
        result.update({"status":"exited", "process_id":process_id, "pid":proc.pid})
        with _lock:
            already_recorded = process_id in _process_results
        if not already_recorded:
            _remember_result(process_id, result)

def run_python_dev(root: Path, code: str, timeout_seconds: int) -> dict:
    env=os.environ.copy(); env["PYTHONPATH"]=str(root)+os.pathsep+env.get("PYTHONPATH","")
    proc=subprocess.Popen([sys.executable,"-c",code],cwd=str(root),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    result=_output(proc,timeout_seconds); result["mode"]="dev"; return result

def symbol_search(root: Path, symbol: str, max_results: int=200) -> dict:
    results=[]
    for file in root.rglob("*"):
        if len(results)>=max_results: break
        if not file.is_file() or any(x in {".git","node_modules",".venv"} for x in file.parts): continue
        rel=file.relative_to(root).as_posix()
        if file.suffix==".py":
            try: tree=ast.parse(file.read_text(encoding="utf-8"))
            except (OSError,SyntaxError,UnicodeDecodeError): continue
            for node in ast.walk(tree):
                kind = "definition" if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name==symbol else "import" if isinstance(node,(ast.Import,ast.ImportFrom)) and symbol in ast.unparse(node) else "reference" if isinstance(node,ast.Name) and node.id==symbol else None
                if kind: results.append({"path":rel,"line":node.lineno,"kind":kind})
        elif file.suffix.lower() in {".js",".ts",".tsx",".jsx",".html",".htm"}:
            try: lines=file.read_text(encoding="utf-8",errors="ignore").splitlines()
            except OSError: continue
            for number,line in enumerate(lines,1):
                if symbol in line: results.append({"path":rel,"line":number,"kind":"definition" if any(x in line for x in ("function "+symbol,"class "+symbol,"id=\""+symbol)) else "reference","text":redact(line[:400])})
    return {"status":"ok","symbol":symbol,"results":results[:max_results],"returned":min(len(results),max_results),"truncated":len(results)>max_results}

def changed_symbols(root: Path, paths: list[str]) -> dict:
    items=[]
    for rel in paths:
        file=root/rel
        if file.suffix==".py" and file.exists():
            try: tree=ast.parse(file.read_text(encoding="utf-8")); names=[n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
            except Exception: names=[]
            items.append({"path":rel,"symbols":names})
        elif file.suffix.lower() in {".html",".htm",".js",".ts",".tsx",".jsx",".css"}: items.append({"path":rel,"kind":"web_asset"})
    return {"status":"ok","files":items}
