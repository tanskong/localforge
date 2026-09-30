import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from capabilities import office


ROOT = Path(__file__).resolve().parents[1]


def test_capability_contract_black_box_over_stdio(tmp_path):
    outside = tmp_path.parent / "escape.xlsx"
    code = f"""
from capabilities.runtime import execute
root = {str(tmp_path)!r}
path = 'safe.xlsx'
value = 'A&B <ready> 中文'
execute('office', 'create_workbook', root=root, path=path, value=value)
assert execute('office', 'read_cell', root=root, path=path)['value'] == value
updated = '& <done> 中文'
execute('office', 'write_cell', root=root, path=path, cell='A1', value=updated)
assert execute('office', 'read_cell', root=root, path=path)['value'] == updated
for escaped in ('../escape.xlsx', {str(outside)!r}):
    try:
        execute('office', 'create_workbook', root=root, path=escaped, value='blocked')
    except ValueError:
        pass
    else:
        raise AssertionError('workspace escape was accepted')
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
                    "purpose": "Gate 2 M8 capability hardening",
                    "timeout_seconds": 30,
                })
                assert result.structured_content["status"] == "ok"
                assert result.structured_content["exit_code"] == 0
                assert len((await client.list_tools()).tools) == 16

    asyncio.run(run())


def test_write_cell_replace_failure_preserves_original(tmp_path, monkeypatch):
    office.create_workbook(str(tmp_path), "atomic.xlsx", "original")

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        office.write_cell(str(tmp_path), "atomic.xlsx", "A1", "updated")
    except OSError:
        pass
    else:
        raise AssertionError("replace failure was not injected")
    assert office.read_cell(str(tmp_path), "atomic.xlsx")["value"] == "original"
    assert not list(tmp_path.glob(".atomic.xlsx.*.tmp"))
