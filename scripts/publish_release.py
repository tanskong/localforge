"""Publish a verified release to the dedicated LocalForge server directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import uuid


def call(args: list[str], **kwargs) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dist = (root / args.dist).resolve()
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    wheel = dist / manifest["filename"]
    for item in (wheel, dist / "manifest.json", dist / "SHA256SUMS"):
        if not item.is_file():
            raise SystemExit(f"missing release file: {item}")
    ssh = ["ssh", "-i", args.key, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", args.host]
    scp = ["scp", "-i", args.key, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes"]
    print(call(ssh + ["hostname; whoami"]))
    remote = "/opt/localforge"
    version = manifest["version"]
    token = uuid.uuid4().hex
    incoming = f"{remote}/.incoming/{token}"
    prepare = (
        f"if test -d {shlex.quote(remote)} && test -w {shlex.quote(remote)}; then "
        f"mkdir -p {shlex.quote(incoming)}; "
        f"else sudo -n mkdir -p {shlex.quote(remote)}/releases {shlex.quote(remote)}/.incoming "
        f"&& sudo -n chown -R \"$(id -un):$(id -gn)\" {shlex.quote(remote)} "
        f"&& mkdir -p {shlex.quote(incoming)}; fi"
    )
    call(ssh + [prepare])
    for item in (wheel, dist / "manifest.json", dist / "SHA256SUMS"):
        call(scp + [str(item), f"{args.host}:{incoming}/"])
    finalize = (
        f"set -eu; root={shlex.quote(remote)}; incoming={shlex.quote(incoming)}; "
        f"version={shlex.quote(version)}; mkdir -p \"$root/releases\"; "
        f"test \"$(sha256sum \"$incoming/{wheel.name}\" | awk '{{print $1}}')\" = \"$(sed -n 's/^\\([^ ]*\\).*$/\\1/p' \"$incoming/SHA256SUMS\")\"; "
        f"if test -e \"$root/releases/$version\"; then test \"$(sha256sum \"$root/releases/$version/{wheel.name}\" | awk '{{print $1}}')\" = \"$(sed -n 's/^\\([^ ]*\\).*$/\\1/p' \"$incoming/SHA256SUMS\")\" || exit 42; rm -rf \"$incoming\"; exit 0; fi; "
        f"mv \"$incoming\" \"$root/releases/$version\"; printf '%s\\n' \"$(cat \"$root/releases/$version/manifest.json\")\" > \"$root/releases/latest.json.tmp\"; mv \"$root/releases/latest.json.tmp\" \"$root/releases/latest.json\""
    )
    call(ssh + [finalize])
    print(json.dumps({"status": "ok", "version": version, "remote": f"{remote}/releases/{version}"}))


if __name__ == "__main__":
    main()
