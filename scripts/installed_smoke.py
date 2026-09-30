"""Installed-package MCP smoke; run from outside the source repository."""
from __future__ import annotations

import argparse
import asyncio
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(version: str) -> None:
    import capabilities
    import server.main
    source = Path(__file__).resolve().parents[1]
    assert metadata.version("localforge") == version
    assert source not in Path(server.main.__file__).resolve().parents
    assert source not in Path(capabilities.__file__).resolve().parents
    with tempfile.TemporaryDirectory() as location:
        project = Path(location)
        (project / "README.md").write_text("installed smoke\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=Smoke", "-c", "user.email=smoke@example.invalid", "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=Smoke", "-c", "user.email=smoke@example.invalid", "commit", "-qm", "init"], check=True)
        params = StdioServerParameters(command=sys.executable, args=["-m", "server.main"], cwd=str(project))
        async with stdio_client(params) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) == 16
                assert (await client.call_tool("project_context", {"path": str(project)})).structured_content["status"] == "ok"
                assert "installed smoke" in (await client.call_tool("read_file", {"path": str(project / "README.md")})).structured_content["content"]
                assert (await client.call_tool("search_code", {"path": str(project), "query": "installed"})).structured_content["returned"] >= 1
                shell = await client.call_tool("shell", {"root": str(project), "executable": sys.executable, "args": ["-c", "print('SMOKE_OK')"]})
                assert shell.structured_content["exit_code"] == 0
                git_status = await client.call_tool("git_status", {"path": str(project)})
                assert git_status.structured_content["status"] == "ok"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    asyncio.run(smoke(args.version))
    print("INSTALLED_SMOKE_OK")
