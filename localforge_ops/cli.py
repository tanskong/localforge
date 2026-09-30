from __future__ import annotations

import argparse
import os
from .autostart import default_target, disable, enable, status as autostart_status
from .config import load
from .credentials import CredentialStore
from .runtime import RuntimeController

def format_status(state, credential, autostart):
    running = state.get("running_version", "unknown")
    installed = state.get("installed_version", "unknown")
    return "\n".join([
        f"overall_status={'ready' if state.get('ready') else state.get('runtime_state', 'unknown')}",
        f"alias={state.get('alias', 'localforge-web')}",
        f"runtime_state={state.get('runtime_state', 'unknown')}",
        f"ready={bool(state.get('ready'))}",
        f"healthy={bool(state.get('healthy', state.get('ready')))}",
        f"installed_version={installed}",
        f"running_version={running}",
        f"version_mismatch={bool(state.get('version_mismatch'))}",
        f"ui_url={state.get('ui_url', '')}",
        f"credential_status={credential.get('status', 'unknown')}",
        f"autostart_status={autostart.get('status', 'unknown')}",
    ])

def main(argv=None):
    parser = argparse.ArgumentParser(prog="localforge-ops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    cred = sub.add_parser("credential").add_subparsers(dest="action", required=True)
    cred.add_parser("status"); cred.add_parser("import-env")
    auto = sub.add_parser("autostart").add_subparsers(dest="action", required=True)
    auto.add_parser("status"); auto.add_parser("enable"); auto.add_parser("disable")
    for name in ("start", "stop", "restart"): sub.add_parser(name)
    args = parser.parse_args(argv); cfg = load(); store = CredentialStore(); controller = RuntimeController(cfg, store)
    if args.command == "status":
        result = controller.status(); print(format_status(result, store.status(), autostart_status())); return 0
    elif args.command == "credential":
        if args.action == "status": result = store.status()
        elif args.action == "import-env":
            value = os.environ.get("CONTROL_PLANE_API_KEY")
            if not value: parser.error("CONTROL_PLANE_API_KEY is not set")
            result = store.set(value)
    elif args.command == "autostart":
        result = autostart_status() if args.action == "status" else enable(default_target(cfg["runtime_root"])) if args.action == "enable" else disable()
    else: result = getattr(controller, args.command)()
    print(result.get("status", "ok"), result.get("target", result.get("action", "")))
    return 0 if result.get("status") not in {"failed", "missing"} else 1
