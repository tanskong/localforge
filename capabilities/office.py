"""Minimal OOXML workbook proof using only the Python standard library."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree

from server.security import SecurityError, safe_path, validate_root
from capabilities.excel_com import OPERATIONS as EXCEL_OPERATIONS

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SHEET = "xl/worksheets/sheet1.xml"


def _target(root: str, path: str) -> Path:
    workspace = validate_root(root)
    target = safe_path(workspace, path)
    try:
        target.resolve(strict=False).relative_to(workspace)
    except ValueError as error:
        raise SecurityError("Capability path escapes workspace") from error
    return target


def create_workbook(root: str, path: str, value: str) -> dict:
    target = _target(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.register_namespace("", _NS)
    sheet_root = ElementTree.Element(f"{{{_NS}}}worksheet")
    sheet_data = ElementTree.SubElement(sheet_root, f"{{{_NS}}}sheetData")
    row = ElementTree.SubElement(sheet_data, f"{{{_NS}}}row", {"r": "1"})
    cell = ElementTree.SubElement(row, f"{{{_NS}}}c", {"r": "A1", "t": "inlineStr"})
    inline = ElementTree.SubElement(cell, f"{{{_NS}}}is")
    ElementTree.SubElement(inline, f"{{{_NS}}}t").text = value
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/{_SHEET}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''')
        archive.writestr("_rels/.rels", f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        archive.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{_NS}" xmlns:r="{_REL_NS}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>''')
        archive.writestr(_SHEET, ElementTree.tostring(sheet_root, encoding="utf-8", xml_declaration=True))
    return {"status": "ok", "path": str(target), "cell": "A1", "value": value}


def read_cell(root: str, path: str, cell: str = "A1") -> dict:
    target = _target(root, path)
    with ZipFile(target) as archive:
        root = ElementTree.fromstring(archive.read(_SHEET))
    for item in root.iter(f"{{{_NS}}}c"):
        if item.get("r") == cell:
            text = item.find(f".//{{{_NS}}}t")
            return {"status": "ok", "cell": cell, "value": "" if text is None else text.text}
    raise ValueError(f"cell not found: {cell}")


def write_cell(root: str, path: str, cell: str, value: str) -> dict:
    target = _target(root, path)
    with ZipFile(target) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    root = ElementTree.fromstring(entries[_SHEET])
    item = next((node for node in root.iter(f"{{{_NS}}}c") if node.get("r") == cell), None)
    if item is None:
        row = root.find(f".//{{{_NS}}}row")
        if row is None:
            sheet_data = root.find(f"{{{_NS}}}sheetData")
            row = ElementTree.SubElement(sheet_data, f"{{{_NS}}}row", {"r": "1"})
        item = ElementTree.SubElement(row, f"{{{_NS}}}c", {"r": cell, "t": "inlineStr"})
    item.set("t", "inlineStr")
    inline = item.find(f"{{{_NS}}}is")
    if inline is None:
        inline = ElementTree.SubElement(item, f"{{{_NS}}}is")
    text = inline.find(f"{{{_NS}}}t")
    if text is None:
        text = ElementTree.SubElement(inline, f"{{{_NS}}}t")
    text.text = value
    entries[_SHEET] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with ZipFile(temporary_path, "w", ZIP_DEFLATED) as output:
            for name, content in entries.items():
                output.writestr(name, content)
        os.replace(temporary_path, target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return {"status": "ok", "path": str(target), "cell": cell, "value": value}


OPERATIONS = {
    "create_workbook": create_workbook,
    "read_cell": read_cell,
    "write_cell": write_cell,
    **EXCEL_OPERATIONS,
}
