import asyncio
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=M1", "-c", "user.email=m1@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_m1_black_box_coding_loop(tmp_path):
    project = tmp_path / "project"
    state = tmp_path / "localforge-state"
    state.mkdir()
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "calc.py").write_text("def add_one(value):\n    return value - 1\n", encoding="utf-8")
    (project / "tests" / "test_calc.py").write_text(
        "from src.calc import add_one\n\ndef test_add_one():\n    assert add_one(1) == 2\n", encoding="utf-8"
    )
    (project / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "config", "core.autocrlf", "false")
    _git(project, "add", ".")
    _git(project, "commit", "-qm", "initial buggy project")

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tests.protocol_server", str(state)],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=30) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "LocalForge"
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert len(names) == 16

                context = await session.call_tool("project_context", {"path": str(project)})
                assert not context.is_error and context.structured_content["git"]["dirty"] is False

                search = await session.call_tool("search_code", {"path": str(project), "query": "add_one"})
                assert not search.is_error and search.structured_content["returned"] >= 1
                source = await session.call_tool("read_file", {"path": str(project / "src" / "calc.py")})
                assert "value - 1" in source.structured_content["content"]

                before = await session.call_tool("shell", {
                    "root": str(project), "executable": sys.executable,
                    "args": ["-m", "pytest", "-q", "tests/test_calc.py"], "timeout_seconds": 30,
                })
                assert not before.is_error
                assert before.structured_content["exit_code"] != 0

                patched = await session.call_tool("apply_patch", {
                    "root": str(project),
                    "changes": [{
                        "path": "src/calc.py", "operation": "update",
                        "expected": "def add_one(value):\n    return value - 1\n",
                        "content": "def add_one(value):\n    return value + 1\n",
                    }],
                })
                assert not patched.is_error and patched.structured_content["status"] == "ok"

                after = await session.call_tool("shell", {
                    "root": str(project), "executable": sys.executable,
                    "args": ["-m", "pytest", "-q", "tests/test_calc.py"], "timeout_seconds": 30,
                })
                assert not after.is_error and after.structured_content["exit_code"] == 0

                status = await session.call_tool("git_status", {"path": str(project)})
                assert not status.is_error
                assert any(item["path"] == "src/calc.py" for item in status.structured_content["files"])
                diff = await session.call_tool("git_diff", {"path": str(project)})
                assert not diff.is_error and any(item["path"] == "src/calc.py" for item in diff.structured_content["unstaged"])

                validated = await session.call_tool("validate", {
                    "root": str(project),
                    "commands": [f'"{sys.executable}" -m pytest -q tests/test_calc.py'],
                    "allowed_patterns": ["src/**", "tests/**"],
                    "timeout_seconds": 30,
                })
                assert not validated.is_error
                result = validated.structured_content
                assert result["status"] == "ok"
                assert result["scope"]["passed"] is True
                assert result["scope"]["current"]["out_of_scope_changes"] == []

    asyncio.run(run())
