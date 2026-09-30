import asyncio
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Loop", "-c", "user.email=loop@example.invalid", *args], check=True, capture_output=True, text=True)


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    project, state = tmp_path / "project", tmp_path / "state"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    state.mkdir()
    (project / "src" / "calc.py").write_text("def transform(value):\n    return value - 1\n", encoding="utf-8")
    (project / "tests" / "test_calc.py").write_text(
        "from src.calc import transform\n\ndef test_transform():\n    assert transform(1) == 2\n    assert transform(2) == 4\n", encoding="utf-8"
    )
    (project / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(project, "init", "-q")
    git(project, "config", "core.autocrlf", "false")
    git(project, "add", ".")
    git(project, "commit", "-qm", "initial repair project")
    return project, state


async def session_for(project: Path, state: Path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "tests.protocol_server", str(state)], cwd=str(Path(__file__).resolve().parents[1]))
    return stdio_client(params)


def test_m2_black_box_repair_loop(tmp_path):
    project, state = make_project(tmp_path)

    async def run():
        async with await session_for(project, state) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                context = await client.call_tool("project_context", {"path": str(project)})
                assert context.structured_content["git"]["dirty"] is False
                search = await client.call_tool("search_code", {"path": str(project), "query": "transform"})
                assert search.structured_content["returned"] >= 1
                source = await client.call_tool("read_file", {"path": str(project / "src" / "calc.py")})
                assert "value - 1" in source.structured_content["content"]

                first = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-m", "pytest", "-q", "tests/test_calc.py"], "timeout_seconds": 30})
                assert first.structured_content["exit_code"] != 0 and "assert" in first.structured_content["stdout"]
                patch1 = await client.call_tool("apply_patch", {"root": str(project), "changes": [{
                    "path": "src/calc.py", "operation": "update", "expected": "def transform(value):\n    return value - 1\n", "content": "def transform(value):\n    return value + 1\n"
                }]})
                assert patch1.structured_content["status"] == "ok"
                second = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-m", "pytest", "-q", "tests/test_calc.py"], "timeout_seconds": 30})
                assert second.structured_content["exit_code"] != 0 and "1 failed" in second.structured_content["stdout"]
                source2 = await client.call_tool("read_file", {"path": str(project / "src" / "calc.py")})
                assert "value + 1" in source2.structured_content["content"]
                patch2 = await client.call_tool("apply_patch", {"root": str(project), "changes": [{
                    "path": "src/calc.py", "operation": "update", "expected": "def transform(value):\n    return value + 1\n", "content": "def transform(value):\n    return value * 2\n"
                }]})
                assert patch2.structured_content["status"] == "ok"
                final = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-m", "pytest", "-q", "tests/test_calc.py"], "timeout_seconds": 30})
                assert final.structured_content["exit_code"] == 0
                status = await client.call_tool("git_status", {"path": str(project)})
                diff = await client.call_tool("git_diff", {"path": str(project)})
                assert any(item["path"] == "src/calc.py" for item in status.structured_content["files"])
                assert any(item["path"] == "src/calc.py" for item in diff.structured_content["unstaged"])
                validated = await client.call_tool("validate", {"root": str(project), "commands": [f'"{sys.executable}" -m pytest -q tests/test_calc.py'], "allowed_patterns": ["src/**", "tests/**"], "timeout_seconds": 30})
                assert validated.structured_content["status"] == "ok" and validated.structured_content["scope"]["passed"]
    asyncio.run(run())


def test_m3_background_stop_and_timeout_loop(tmp_path):
    project, state = make_project(tmp_path)

    async def run():
        async with await session_for(project, state) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                short = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-c", "import time; print('done', flush=True); time.sleep(.2)"], "background": True, "timeout_seconds": 5})
                pid = short.structured_content["process_id"]
                assert short.structured_content["status"] == "started"
                running = await client.call_tool("process_status", {"process_id": pid})
                assert running.structured_content["status"] == "running"
                await asyncio.sleep(.5)
                exited = await client.call_tool("process_status", {"process_id": pid})
                assert exited.structured_content["status"] in {"exited", "completed"} and exited.structured_content["exit_code"] == 0
                assert "done" in exited.structured_content.get("stdout", "")

                long = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-c", "import time; time.sleep(30)"], "background": True, "timeout_seconds": 5})
                long_pid = long.structured_content["process_id"]
                assert (await client.call_tool("process_status", {"process_id": long_pid})).structured_content["status"] == "running"
                stopped = await client.call_tool("process_stop", {"process_id": long_pid})
                assert stopped.structured_content["status"] in {"stopped", "terminated"}
                after_stop = await client.call_tool("process_status", {"process_id": long_pid})
                assert after_stop.structured_content["status"] in {"stopped", "terminated", "exited"}

                timeout = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-c", "import time; time.sleep(3)"], "timeout_seconds": 1})
                assert timeout.structured_content["status"] == "timeout"
                assert (await client.call_tool("git_status", {"path": str(project)})).structured_content["status"] == "ok"
    asyncio.run(run())


def test_m3_registry_retention_is_bounded(monkeypatch):
    import adapters.development as development

    development._processes.clear()
    development._process_locks.clear()
    development._process_results.clear()
    monkeypatch.setattr(development, "_MAX_PROCESS_RESULTS", 2)
    for index in range(3):
        development._remember_result(f"p{index}", {"status": "exited", "exit_code": 0})
    assert len(development._process_results) == 2
    assert development.process_status("p0")["status"] == "not_found"
    assert development.process_status("p2")["status"] == "exited"
