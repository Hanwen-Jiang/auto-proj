from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from .errors import A6AutoError

_CELL_REFERENCE = re.compile(r"^([A-Z]+)[1-9][0-9]*$")
_RELATIONSHIP_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)


@dataclass(frozen=True)
class XlsxRow:
    source_row: int
    values: tuple[str, ...]


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise A6AutoError("xlsx_catalog_cell_reference_invalid")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return ()
    except ElementTree.ParseError as exc:
        raise A6AutoError("xlsx_catalog_invalid") from exc
    return tuple(
        "".join(node.text or "" for node in item.findall(".//{*}t"))
        for item in root.findall("{*}si")
    )


def _cell_text(cell: ElementTree.Element, shared: tuple[str, ...]) -> str:
    cell_type = cell.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//{*}t"))
    value = cell.find("{*}v")
    text = "" if value is None else str(value.text or "")
    if cell_type == "s":
        try:
            return shared[int(text)]
        except (IndexError, ValueError) as exc:
            raise A6AutoError("xlsx_catalog_invalid") from exc
    return text


def _sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
    except (KeyError, ElementTree.ParseError) as exc:
        raise A6AutoError("xlsx_catalog_invalid") from exc
    sheets = [
        item
        for item in workbook.findall(".//{*}sheet")
        if item.get("name") == sheet_name
    ]
    if len(sheets) != 1:
        raise A6AutoError("xlsx_catalog_sheet_invalid")
    relationship_id = sheets[0].get(_RELATIONSHIP_ID, "")
    targets = [
        item.get("Target", "")
        for item in relationships.findall("{*}Relationship")
        if item.get("Id") == relationship_id
    ]
    if len(targets) != 1 or not targets[0]:
        raise A6AutoError("xlsx_catalog_sheet_invalid")
    target = targets[0]
    path = (
        target.lstrip("/")
        if target.startswith("/")
        else posixpath.normpath(posixpath.join("xl", target))
    )
    if not path.startswith("xl/") or ".." in path.split("/"):
        raise A6AutoError("xlsx_catalog_sheet_invalid")
    return path


def read_xlsx_sheet(content: bytes, sheet_name: str) -> tuple[XlsxRow, ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            shared = _shared_strings(archive)
            root = ElementTree.fromstring(archive.read(_sheet_path(archive, sheet_name)))
    except A6AutoError:
        raise
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise A6AutoError("xlsx_catalog_invalid") from exc

    rows: list[XlsxRow] = []
    for ordinal, row in enumerate(root.findall(".//{*}sheetData/{*}row"), 1):
        values: dict[int, str] = {}
        for cell in row.findall("{*}c"):
            values[_column_index(str(cell.get("r") or ""))] = _cell_text(cell, shared)
        width = max(values, default=-1) + 1
        try:
            source_row = int(row.get("r") or ordinal)
        except ValueError as exc:
            raise A6AutoError("xlsx_catalog_row_invalid") from exc
        rows.append(
            XlsxRow(
                source_row=source_row,
                values=tuple(values.get(index, "").strip() for index in range(width)),
            )
        )
    if not rows:
        raise A6AutoError("xlsx_catalog_rows_empty")
    return tuple(rows)
