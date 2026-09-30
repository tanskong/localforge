from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time

from .config import load
from .credentials import CredentialStore

def default_process_provider() -> list[dict]:
    command = "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not result.stdout.strip(): return []
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else [data]

def running_version_from_tree(root: str, managed_pid: int | None, provider=default_process_provider) -> str:
    if not managed_pid: return "unknown"
    rows = provider(); by_parent = {}
    for row in rows:
        by_parent.setdefault(int(row.get("ParentProcessId") or 0), []).append(row)
    queue = [int(managed_pid)]; seen = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen: continue
        seen.add(pid)
        for row in by_parent.get(pid, []):
            command = str(row.get("CommandLine") or "")
            match = re.search(r"versions[\\/]+([^\\/\" ]+)[\\/]+venv[\\/]Scripts[\\/]localforge(?:\.exe)?", command, re.I)
            if match: return match.group(1)
            queue.append(int(row.get("ProcessId") or 0))
    return "unknown"

class RuntimeController:
    def __init__(self, config=None, credentials=None, runner=subprocess.run, popen=subprocess.Popen, process_provider=default_process_provider):
        self.config = config or load()
        self.credentials = credentials or CredentialStore()
        self.runner = runner
        self.popen = popen
        self.process_provider = process_provider
        self.child = None

    def status(self) -> dict:
        c = self.config
        result = self.runner([c["tunnel_client"], "runtimes", "status", c["alias"], "--json"],
                             capture_output=True, text=True, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            data = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            data = {"runtime_state": "unknown", "ready": False, "healthy": False}
        current = Path(c["runtime_root"]) / "current.txt"
        data["installed_version"] = current.read_text(encoding="ascii").strip() if current.is_file() else "unknown"
        managed_pid = ((data.get("process") or {}).get("pid") if isinstance(data.get("process"), dict) else None)
        data["running_version"] = running_version_from_tree(str(c["runtime_root"]), managed_pid, self.process_provider)
        data["version_mismatch"] = data["running_version"] not in {"unknown", data["installed_version"]}
        data["alias"] = c["alias"]
        return data

    def start(self, timeout=30) -> dict:
        state = self.status()
        if state.get("ready") or state.get("runtime_state") in {"ready", "connected"}:
            return {"status": "ok", "action": "already_running", "state": state}
        key = self.credentials.get()
        env = os.environ.copy(); env["CONTROL_PLANE_API_KEY"] = key
        c = self.config
        self.child = self.popen([c["tunnel_client"], "runtimes", "connect", "--alias", c["alias"],
                                 "--profile", c["profile"], "--tunnel-id", c["tunnel_id"],
                                 "--mcp-command", f"cmd.exe /d /s /c {c['launcher']}"],
                                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.status()
            if state.get("ready") or state.get("runtime_state") in {"ready", "connected"}:
                return {"status": "ok", "action": "started", "state": state}
            time.sleep(.5)
        return {"status": "failed", "reason": "readiness_timeout", "state": self.status()}

    def stop(self) -> dict:
        c = self.config
        result = self.runner([c["tunnel_client"], "runtimes", "stop", c["alias"]], capture_output=True, text=True,
                             check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"status": "ok" if result.returncode == 0 else "failed", "action": "stopped" if result.returncode == 0 else "stop_failed"}

    def restart(self, timeout=30) -> dict:
        stopped = self.stop()
        return {"status": "failed", "stop": stopped} if stopped["status"] != "ok" else self.start(timeout)
