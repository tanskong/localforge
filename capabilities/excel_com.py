"""Small Excel COM adapter with explicit ownership semantics."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from server.security import SecurityError


def _target(root: str, path: str) -> Path:
    from capabilities.office import _target as office_target
    return office_target(root, path)


@contextmanager
def _application(mode: str):
    if mode not in {"active", "isolated"}:
        raise ValueError("mode must be active or isolated")
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise RuntimeError("excel_com_unavailable") from error
    pythoncom.CoInitialize()
    app = None
    owned = mode == "isolated"
    try:
        if mode == "active":
            try:
                app = win32com.client.GetActiveObject("Excel.Application")
            except Exception as error:
                raise RuntimeError("excel_active_unavailable") from error
        else:
            app = win32com.client.DispatchEx("Excel.Application")
            app.Visible = False
        yield app
    except Exception as error:
        if isinstance(error, (RuntimeError, ValueError, SecurityError)):
            raise
        raise RuntimeError(f"excel_com_error: {type(error).__name__}") from error
    finally:
        if owned and app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        app = None
        pythoncom.CoUninitialize()


def _sheet(app, mode: str, root: str, path: str | None, sheet: str | None):
    if mode == "active":
        workbook = app.ActiveWorkbook
        if workbook is None:
            raise RuntimeError("excel_active_workbook_unavailable")
        return workbook, workbook.Worksheets(sheet) if sheet else app.ActiveSheet, False
    if not path:
        raise ValueError("isolated mode requires path")
    target = _target(root, path)
    if target.exists():
        workbook = app.Workbooks.Open(str(target))
    else:
        workbook = app.Workbooks.Add()
        workbook.SaveAs(str(target), FileFormat=51)
    return workbook, workbook.Worksheets(sheet) if sheet else workbook.ActiveSheet, True


def excel_read_cell(mode: str, root: str, path: str | None = None, cell: str = "A1", sheet: str | None = None) -> dict:
    with _application(mode) as app:
        workbook, worksheet, close_workbook = _sheet(app, mode, root, path, sheet)
        try:
            return {"status": "ok", "mode": mode, "cell": cell, "value": worksheet.Range(cell).Value}
        finally:
            if close_workbook:
                workbook.Close(SaveChanges=False)
            worksheet = None
            workbook = None


def excel_write_cell(mode: str, root: str, value: str, path: str | None = None, cell: str = "A1",
                     sheet: str | None = None, save: bool = False) -> dict:
    with _application(mode) as app:
        workbook, worksheet, close_workbook = _sheet(app, mode, root, path, sheet)
        try:
            worksheet.Range(cell).Value = value
            if save:
                workbook.Save()
            return {"status": "ok", "mode": mode, "cell": cell, "value": value, "saved": save}
        finally:
            if close_workbook:
                workbook.Close(SaveChanges=False)
            worksheet = None
            workbook = None


OPERATIONS = {
    "excel_read_cell": excel_read_cell,
    "excel_write_cell": excel_write_cell,
}
