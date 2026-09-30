from pathlib import Path
import subprocess

import pytest
from server.service import Service


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.autocrlf=false", *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


@pytest.fixture
def sample():
    # Windows system Temp is intentionally not registrable; fixtures live in service/tmp.
    import tempfile
    base = Path(__file__).resolve().parents[1] / "tmp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as location:
        base = Path(location)
        service_root = base / "service"
        service_root.mkdir(parents=True)
        root = base / "business"
        root.mkdir()
        (root / "README.md").write_text("# Example\nSmall sample project\n", encoding="utf-8")
        (root / "PROJECT_STATE.md").write_text("# Project State\n## Known issues\nNone\n", encoding="utf-8")
        (root / "main.py").write_text("print('first')\n", encoding="utf-8")
        (root / "logs").mkdir()
        (root / "logs/app.log").write_text("INFO ready\nERROR sample failure\n", encoding="utf-8")
        (root / "junit.xml").write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>', encoding="utf-8")
        git(root, "init", "-q")
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial sample")
        yield Service(service_root), root
