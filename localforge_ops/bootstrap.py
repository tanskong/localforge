"""Stable, no-console logon bootstrap generated beside the installed runtime."""
from __future__ import annotations

import os
from pathlib import Path

def write_bootstrap(runtime_root: Path) -> Path:
    target = runtime_root / "bin" / "localforge-tray.vbs"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = ('Set shell = CreateObject("WScript.Shell")\n'
            f'root = "{runtime_root}"\n'
            'shell.Run """" & root & "\\versions\\" & ReadCurrent(root) & "\\venv\\Scripts\\pythonw.exe""" -m localforge_ops.tray", 0, False\n'
            'Function ReadCurrent(root)\n'
            '  Set fso = CreateObject("Scripting.FileSystemObject")\n'
            '  Set f = fso.OpenTextFile(root & "\\current.txt", 1)\n'
            '  ReadCurrent = Trim(f.ReadLine)\n'
            '  f.Close\n'
            'End Function\n')
    target.write_text(text, encoding="ascii")
    return target
