"""Exercise the actual STDIO MCP transport and emit content-free acceptance evidence."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(path: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=["-m", "server.main"], cwd=str(root))
    target = str(Path(path).resolve())
    evidence = {"transport": "stdio", "path": target, "calls": {}}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=30) as session:
            initialized = await session.initialize()
            evidence["server"] = initialized.serverInfo.name
            tools = await session.list_tools()
            evidence["tools"] = [t.name for t in tools.tools]
            calls = [("project_context", {"path": target}), ("list_directory", {"path": target}),
                     ("read_file", {"path": str(Path(target) / "README.md")}),
                     ("search_code", {"path": target, "query": "Project"}),
                     ("git_status", {"path": target}), ("work_status", {"root": target})]
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                data = result.structured_content
                assert not result.is_error and data and data["status"] not in {"error", "unavailable"}, name
                evidence["calls"][name] = {"status": data["status"], "truncated": data.get("truncated", False), "security_blocked": data.get("security_blocked", False)}
                if name == "project_context":
                    evidence["health"] = {k: data[k] for k in ("git", "warnings")}
    return evidence


async def smoke_write_task() -> dict:
    """Write-task smoke — isolated temp dir only, never touches real projects."""
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=["-m", "server.main"], cwd=str(root))
    with tempfile.TemporaryDirectory(dir=str(root / "tmp")) as loc:
        tmp = Path(loc)
        # Build a tiny git repo with .gitignore containing .ai/
        subprocess.run(["git", "-C", str(tmp), "init", "-q"], check=True, capture_output=True)
        (tmp / ".gitignore").write_text(".ai/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "add", ".gitignore"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "commit", "-qm", "init"], capture_output=True)
        evidence: dict = {"transport": "stdio", "test_root": str(tmp), "steps": {}}
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=30) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "work_update" in [t.name for t in tools.tools], "work_update not advertised"
                # Create
                r1 = await session.call_tool("work_update", {"root": str(tmp), "content": "# Smoke\nhello\n", "target": "task"})
                assert not r1.is_error
                d1 = r1.structured_content
                assert d1["status"] == "ok" and d1["operation"] == "created"
                evidence["steps"]["create"] = {"status": d1["status"], "operation": d1["operation"], "bytes": d1["bytes"]}
                # Overwrite
                r2 = await session.call_tool("work_update", {"root": str(tmp), "content": "# Smoke\nworld\n", "target": "task"})
                assert not r2.is_error
                d2 = r2.structured_content
                assert d2["status"] == "ok" and d2["operation"] == "replaced"
                evidence["steps"]["overwrite"] = {"status": d2["status"], "operation": d2["operation"], "bytes": d2["bytes"]}
                # Verify file on disk
                f = tmp / ".ai" / "TASK_CURRENT.md"
                assert f.read_text(encoding="utf-8") == "# Smoke\nworld\n"
                evidence["steps"]["disk_content"] = "ok"
                # Check wrong-ignore fails
                tmp2 = tmp.parent / (tmp.name + "_badignore")
                tmp2.mkdir()
                subprocess.run(["git", "-C", str(tmp2), "init", "-q"], check=True, capture_output=True)
                (tmp2 / ".gitignore").write_text("*.log\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(tmp2), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "add", ".gitignore"], capture_output=True)
                subprocess.run(["git", "-C", str(tmp2), "-c", "user.name=T", "-c", "user.email=t@i.invalid", "commit", "-qm", "init"], capture_output=True)
                r3 = await session.call_tool("work_update", {"root": str(tmp2), "content": "x", "target": "task"})
                d3 = r3.structured_content
                assert d3["status"] == "error" and d3["error"] == "security_blocked"
                evidence["steps"]["missing_ignore"] = {"status": d3["status"], "error": d3["error"]}
                # Clean up tmp2 manually (it's outside the with-TemporaryDirectory of tmp)
                import shutil
                shutil.rmtree(tmp2, ignore_errors=True)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=".")
    parser.add_argument("--write-task", action="store_true", help="Run write_task smoke only (isolated temp dir)")
    args = parser.parse_args()
    if args.write_task:
        print(json.dumps(asyncio.run(smoke_write_task()), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(asyncio.run(smoke(args.path)), ensure_ascii=False, indent=2))
