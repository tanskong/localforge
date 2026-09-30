from __future__ import annotations

import subprocess
import os
from pathlib import Path

TASK_NAME = "LocalForge Tray"

def default_target(runtime_root: Path) -> Path:
    return runtime_root / "bin" / "localforge-tray.vbs"

def _run(args, runner=subprocess.run):
    return runner(args, capture_output=True, text=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

def enable(target: Path, runner=subprocess.run) -> dict:
    wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    command = f'"{wscript}" //B //Nologo "{target}"'
    result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/TR", command, "/F"], runner)
    return {"status": "ok" if result.returncode == 0 else "failed", "task": TASK_NAME}

def disable(runner=subprocess.run) -> dict:
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], runner)
    return {"status": "ok" if result.returncode == 0 else "failed", "task": TASK_NAME}

def status(runner=subprocess.run) -> dict:
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/NH"], runner)
    return {"status": "enabled" if result.returncode == 0 else "disabled", "task": TASK_NAME}
