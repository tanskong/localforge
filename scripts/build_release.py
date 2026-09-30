"""Build and verify one immutable LocalForge wheel release artifact."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from zipfile import ZipFile


def run(root: Path, *args: str) -> str:
    return subprocess.run(args, cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    dirty = run(root, "git", "status", "--porcelain")
    if dirty:
        raise SystemExit("working tree is dirty; commit before release build")
    output = (root / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "--wheel-dir", str(output)], cwd=root, check=True)
    wheels = sorted(output.glob(f"localforge-{version}-*.whl"), key=lambda item: item.stat().st_mtime)
    if not wheels:
        raise SystemExit(f"wheel not found for version {version}")
    wheel = wheels[-1]
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    required = {"server/", "adapters/", "capabilities/"}
    if not all(any(name.startswith(prefix) for name in names) for prefix in required):
        raise SystemExit("wheel is missing a required package")
    forbidden = ("tests/", ".ai/", "logs/", "tmp/", "projects/", "sandbox/", ".egg-info/")
    if any(name.startswith(prefix) or prefix in name for name in names for prefix in forbidden):
        raise SystemExit("wheel contains forbidden development content")
    sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {
        "product": "localforge",
        "version": version,
        "filename": wheel.name,
        "sha256": sha256,
        "git_commit": run(root, "git", "rev-parse", "HEAD"),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_requires": project["requires-python"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text(f"{sha256}  {wheel.name}\n", encoding="ascii")
    print(json.dumps({"status": "ok", "version": version, "wheel": str(wheel), "sha256": sha256}, indent=2))


if __name__ == "__main__":
    main()
