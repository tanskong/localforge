from __future__ import annotations

import json
import os
from pathlib import Path

# Generic defaults: no machine-specific paths or IDs.
# tunnel_client and tunnel_id must be configured for tunnel operations.
# runtime_root defaults to %LOCALAPPDATA%\LocalForge (matches install_release.ps1).
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))

DEFAULTS = {
    "alias": "localforge-web",
    "profile": "local-localforge",
    "tunnel_id": "",
    "tunnel_client": "",
    "runtime_root": str(Path(_LOCALAPPDATA) / "LocalForge"),
    "launcher": str(Path(_LOCALAPPDATA) / "LocalForge" / "bin" / "localforge.cmd"),
}

_ENV_OVERRIDES = {
    "LOCALFORGE_TUNNEL_ID": "tunnel_id",
    "LOCALFORGE_TUNNEL_CLIENT": "tunnel_client",
    "LOCALFORGE_RUNTIME_ROOT": "runtime_root",
    "LOCALFORGE_LAUNCHER": "launcher",
    "LOCALFORGE_ALIAS": "alias",
    "LOCALFORGE_PROFILE": "profile",
}


def config_path() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "LocalForge" / "runtime-ops.json"


def load(path: Path | None = None) -> dict:
    target = path or config_path()
    data = dict(DEFAULTS)
    if target.is_file():
        loaded = json.loads(target.read_text(encoding="utf-8"))
        data.update({key: loaded[key] for key in DEFAULTS if key in loaded})
    for env_key, cfg_key in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            data[cfg_key] = val
    return data


def save(data: dict, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {key: data[key] for key in DEFAULTS if key in data}
    target.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return target
