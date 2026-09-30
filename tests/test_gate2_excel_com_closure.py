import asyncio
import sys
from pathlib import Path

import pythoncom
import win32com.client
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def test_excel_active_attach_and_isolated_lifecycle_over_stdio(tmp_path):
    active_path = tmp_path / "active.xlsx"
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("Excel.Application")
    app.Visible = False
    workbook = app.Workbooks.Add()
    workbook.SaveAs(str(active_path), FileFormat=51)
    workbook.Worksheets(1).Range("A1").Value = "before"
    workbook.Save()

    active_code = """
from capabilities.runtime import execute
assert execute('office', 'excel_read_cell', root='.', mode='active', cell='A1')['value'] == 'before'
execute('office', 'excel_write_cell', root='.', mode='active', cell='A1', value='after', save=True)
assert execute('office', 'excel_read_cell', root='.', mode='active', cell='A1')['value'] == 'after'
"""
    isolated_code = f"""
from capabilities.runtime import execute
root = {str(tmp_path)!r}
path = 'isolated.xlsx'
execute('office', 'excel_write_cell', root=root, mode='isolated', path=path, value='isolated', save=True)
assert execute('office', 'excel_read_cell', root=root, mode='isolated', path=path)['value'] == 'isolated'
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
                active = await client.call_tool("run_python", {
                    "root": str(ROOT), "code": active_code, "mode": "dev",
                    "purpose": "Gate 2 active Excel lifecycle", "timeout_seconds": 30,
                })
                assert active.structured_content["status"] == "ok"
                assert active.structured_content["exit_code"] == 0
                isolated = await client.call_tool("run_python", {
                    "root": str(ROOT), "code": isolated_code, "mode": "dev",
                    "purpose": "Gate 2 isolated Excel lifecycle", "timeout_seconds": 30,
                })
                assert isolated.structured_content["status"] == "ok"
                assert isolated.structured_content["exit_code"] == 0
                assert len((await client.list_tools()).tools) == 16

    try:
        asyncio.run(run())
        assert workbook.Worksheets(1).Range("A1").Value == "after"
        assert win32com.client.GetActiveObject("Excel.Application").Hwnd == app.Hwnd
        assert active_path.exists()
        assert (tmp_path / "isolated.xlsx").exists()
    finally:
        workbook.Close(SaveChanges=False)
        app.Quit()


def test_excel_active_without_instance_is_controlled(tmp_path):
    code = """
from capabilities.runtime import execute
import win32com.client
win32com.client.GetActiveObject = lambda _: (_ for _ in ()).throw(RuntimeError('no active Excel'))
try:
    execute('office', 'excel_read_cell', root='.', mode='active', cell='A1')
except RuntimeError as error:
    assert 'excel_' in str(error)
else:
    raise AssertionError('active mode silently fell back to a new instance')
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
                    "root": str(ROOT), "code": code, "mode": "dev",
                    "purpose": "Gate 2 active Excel error contract", "timeout_seconds": 30,
                })
                assert result.structured_content["exit_code"] == 0

    asyncio.run(run())
