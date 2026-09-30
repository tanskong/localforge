import asyncio
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=State", "-c", "user.email=state@example.invalid", *args], check=True, capture_output=True, text=True)


def repo(path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (path / ".gitignore").write_text(".ai/\n__pycache__/\n", encoding="utf-8")
    git(path, "init", "-q"); git(path, "config", "core.autocrlf", "false"); git(path, "add", "."); git(path, "commit", "-qm", "initial")
    return path


def params(state: Path):
    return StdioServerParameters(command=sys.executable, args=["-m", "tests.protocol_server", str(state)], cwd=str(Path(__file__).resolve().parents[1]))


def test_m4_checkpoint_resume_across_stdio_sessions(tmp_path):
    project = repo(tmp_path / "project", {"src/value.py": "VALUE = 1\n", "tests/test_value.py": "from src.value import VALUE\ndef test_value():\n    assert VALUE == 2\n"})
    state = tmp_path / "state"; state.mkdir()

    async def session_a():
        async with stdio_client(params(state)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                task = await client.call_tool("work_update", {"root": str(project), "target": "task", "content": "Fix VALUE to 2.\n"})
                assert task.structured_content["status"] == "ok"
                patch = await client.call_tool("apply_patch", {"root": str(project), "changes": [{"path": "src/value.py", "operation": "update", "expected": "VALUE = 1\n", "content": "VALUE = 2\n"}]})
                assert patch.structured_content["status"] == "ok"
                checkpoint = await client.call_tool("work_update", {"root": str(project), "target": "checkpoint", "content": "Completed source edit.\nNext: run tests, validate scope, and deliver.\n"})
                assert checkpoint.structured_content["status"] == "ok"

    async def session_b():
        async with stdio_client(params(state)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                status = await client.call_tool("work_status", {"root": str(project)})
                data = status.structured_content
                assert data["task"]["text"].startswith("Fix VALUE")
                assert "Next: run tests" in data["checkpoint"]["text"]
                tested = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-m", "pytest", "-q", "tests/test_value.py"], "timeout_seconds": 30})
                assert tested.structured_content["exit_code"] == 0
                validated = await client.call_tool("validate", {"root": str(project), "commands": [f'"{sys.executable}" -m pytest -q tests/test_value.py'], "allowed_patterns": ["src/**", "tests/**"], "timeout_seconds": 30})
                assert validated.structured_content["status"] == "ok" and validated.structured_content["scope"]["passed"]

    async def run():
        await session_a()
        await session_b()
    asyncio.run(run())


def test_m5_multi_root_and_navigation_black_box(tmp_path):
    project_a = repo(tmp_path / "a", {"src/core.py": "def value():\n    return 1\n", "src/service.py": "from src.core import value\ndef serve():\n    return value()\n", "src/api.py": "from src.service import serve\ndef answer():\n    return serve()\n", "tests/test_api.py": "from src.api import answer\ndef test_answer():\n    assert answer() == 2\n"})
    project_b = repo(tmp_path / "b", {"main.py": "NAME = 'B'\n"})
    state = tmp_path / "state"; state.mkdir()

    async def run():
        async with stdio_client(params(state)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                context_a = await client.call_tool("project_context", {"path": str(project_a)})
                context_b = await client.call_tool("project_context", {"path": str(project_b)})
                assert context_a.structured_content["root"] == str(project_a)
                assert context_b.structured_content["root"] == str(project_b)
                symbol = await client.call_tool("search_code", {"path": str(project_a), "query": "serve", "mode": "symbol"})
                text = await client.call_tool("search_code", {"path": str(project_a), "query": "from src.core import value", "mode": "text"})
                source = await client.call_tool("read_file", {"path": str(project_a / "src" / "core.py")})
                assert symbol.structured_content["returned"] >= 2
                assert text.structured_content["returned"] >= 1
                assert "return 1" in source.structured_content["content"]
                patch = await client.call_tool("apply_patch", {"root": str(project_a), "changes": [{
                    "path": "src/core.py",
                    "operation": "update",
                    "expected": "def value():\n    return 1\n",
                    "content": "def value():\n    return 2\n",
                }]})
                assert patch.structured_content["status"] == "ok"
                tested = await client.call_tool("shell", {
                    "root": str(project_a),
                    "executable": sys.executable,
                    "args": ["-m", "pytest", "-q", "tests/test_api.py"],
                    "timeout_seconds": 30,
                })
                assert tested.structured_content["exit_code"] == 0
                status_a = await client.call_tool("git_status", {"path": str(project_a)})
                assert any(item["path"] == "src/core.py" for item in status_a.structured_content["files"])
                diff_a = await client.call_tool("git_diff", {"path": str(project_a)})
                assert any(item["path"] == "src/core.py" and "return 2" in item["diff"] for item in diff_a.structured_content["unstaged"])
                validated = await client.call_tool("validate", {
                    "root": str(project_a),
                    "commands": [f'"{sys.executable}" -m pytest -q tests/test_api.py'],
                    "allowed_patterns": ["src/**", "tests/**"],
                    "timeout_seconds": 30,
                })
                assert validated.structured_content["status"] == "ok"
                assert validated.structured_content["scope"]["passed"] is True
                status_b = await client.call_tool("git_status", {"path": str(project_b)})
                assert status_b.structured_content["dirty"] is False
    asyncio.run(run())
