import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def params():
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.protocol_server", str(Path(__file__).resolve().parents[1])],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def test_office_capability_black_box_over_stdio(tmp_path):
    workbook = tmp_path / "proof.xlsx"
    code = f"""
from capabilities.runtime import discover, execute
root = {str(tmp_path)!r}
path = 'proof.xlsx'
assert discover() == ['office']
created = execute('office', 'create_workbook', root=root, path=path, value='LocalForge')
assert created['status'] == 'ok'
assert execute('office', 'read_cell', root=root, path=path)['value'] == 'LocalForge'
try:
    execute('office', 'not_registered', root=root, path=path)
except ValueError as error:
    assert 'unknown operation' in str(error)
else:
    raise AssertionError('unregistered operation was accepted')
"""

    async def run():
        async with stdio_client(params()) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                result = await client.call_tool("run_python", {
                    "root": str(Path(__file__).resolve().parents[1]),
                    "code": code,
                    "mode": "dev",
                    "purpose": "Gate 2 capability proof",
                    "timeout_seconds": 30,
                })
                assert result.structured_content["status"] == "ok"
                assert result.structured_content["exit_code"] == 0
                tools = await client.list_tools()
                assert len(tools.tools) == 16

    asyncio.run(run())
