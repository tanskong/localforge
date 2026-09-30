import asyncio
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_protocol(sample):
    async def run():
        service, root = sample
        # Ensure .ai/ is ignored before we can test write_task
        gi = root / ".gitignore"
        if not gi.is_file() or ".ai/" not in gi.read_text(encoding="utf-8"):
            existing = gi.read_text(encoding="utf-8") if gi.is_file() else ""
            gi.write_text(existing + (existing and "\n" or "") + ".ai/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "add", ".gitignore"], capture_output=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "commit", "-qm", "ignore .ai"], capture_output=True)
        (root / "unicode.py").write_text("TEXT = '中文'\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "add", "unicode.py"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "commit", "-m", "中文提交"], check=True)

        params = StdioServerParameters(command=sys.executable, args=["-m", "tests.protocol_server", str(service.root)], cwd=str(Path(__file__).resolve().parents[1]))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {t.name for t in listed.tools}
                expected = {"project_context", "list_directory", "read_file", "search_code", "apply_patch", "shell", "process_status", "process_stop", "run_python", "git_status", "git_diff", "git_log", "git_show", "work_status", "work_update", "validate"}
                assert names == expected
                assert next(t for t in listed.tools if t.name == "shell").annotations.open_world_hint is True
                assert next(t for t in listed.tools if t.name == "process_status").annotations.read_only_hint is True
                assert next(t for t in listed.tools if t.name == "process_stop").annotations.destructive_hint is True
                for tool in [t for t in listed.tools if t.name in {"project_context", "list_directory", "read_file", "search_code", "git_status", "git_diff", "git_log", "work_status"}]:
                    args = {"root" if tool.name in ("run_python", "write_task", "work_status") else "path": str(root)}
                    if tool.name == "read_file":
                        args["path"] = str(root / "main.py")
                    elif tool.name == "search_code":
                        args["query"] = "print"
                    result = await session.call_tool(tool.name, args)
                    assert not result.is_error, f"{tool.name} error: {result.content}"
                    assert result.structured_content["status"] not in {"error", "unavailable"}, f"{tool.name}: {result.structured_content}"
                rejected = await session.call_tool("git_diff", {"path": "../outside"})
                assert rejected.structured_content["security_blocked"]
                shown = await session.call_tool("git_show", {"path": str(root), "revision": "HEAD"})
                assert "中文提交" in shown.structured_content["metadata"]["message"]
    asyncio.run(run())
