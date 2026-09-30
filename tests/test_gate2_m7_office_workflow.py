import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def test_office_create_modify_readback_over_stdio(tmp_path):
    workbook = tmp_path / "workflow.xlsx"
    code = f"""
from capabilities.runtime import discover, execute
root = {str(tmp_path)!r}
path = 'workflow.xlsx'
assert discover() == ['office']
execute('office', 'create_workbook', root=root, path=path, value='draft')
assert execute('office', 'read_cell', root=root, path=path)['value'] == 'draft'
execute('office', 'write_cell', root=root, path=path, cell='A1', value='published')
assert execute('office', 'read_cell', root=root, path=path, cell='A1')['value'] == 'published'
"""

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tests.protocol_server", str(ROOT)],
            cwd=str(ROOT),
        )
        async with stdio_client(params) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                result = await client.call_tool("run_python", {
                    "root": str(ROOT),
                    "code": code,
                    "mode": "dev",
                    "purpose": "Gate 2 M7 Office artifact workflow",
                    "timeout_seconds": 30,
                })
                assert result.structured_content["status"] == "ok"
                assert result.structured_content["exit_code"] == 0
                assert len((await client.list_tools()).tools) == 16

    asyncio.run(run())
