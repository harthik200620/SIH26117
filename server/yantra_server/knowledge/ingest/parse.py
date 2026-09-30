"""Parsers: PDF (PyMuPDF), Office (docx/xlsx/pptx), code, EML, plain text (SPEC §10.3 step 3).

OCR of scanned pages is delegated to the vision OCR hook (M7); born-digital and Office
formats are handled here directly. Docling is used when installed for complex layouts.
"""

from __future__ import annotations

import email
import logging
import re
from email import policy
from pathlib import Path
from typing import Any

from ..types import Block, ParsedDocument, ParsedPage

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".rst", ".log", ".csv"}
CODE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".go",
    ".rs",
    ".sh",
    ".sql",
    ".f90",
    ".for",
}
GARBAGE_TEXT_RATIO = 0.35  # below this dictionary-word ratio a page is treated as scanned


def parse_document(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix == ".xlsx":
        return _parse_xlsx(path)
    if suffix == ".pptx":
        return _parse_pptx(path)
    if suffix == ".eml":
        return _parse_eml(path)
    if suffix in CODE_SUFFIXES:
        return _parse_code(path)
    if suffix in TEXT_SUFFIXES or suffix == "":
        return _parse_text(path)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        return _parse_image(path)
    return _parse_text(path)


def _title_from(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip()


def _parse_pdf(path: Path) -> ParsedDocument:
    import pymupdf

    doc = ParsedDocument(title=_title_from(path), doc_type="pdf")
    with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
        meta_title = (pdf.metadata or {}).get("title")
        if meta_title:
            doc.title = meta_title
        for index in range(pdf.page_count):
            page = pdf.load_page(index)
            text = page.get_text("text")
            has_layer = len(text.strip()) > 20
            quality = _text_quality(text) if has_layer else 0.0
            page_kind = "text"
            blocks: list[Block] = []
            if has_layer and quality >= GARBAGE_TEXT_RATIO:
                for block in page.get_text("blocks"):
                    body = str(block[4]).strip()
                    if body:
                        blocks.append(
                            Block(
                                text=body,
                                page=index + 1,
                                bbox=(block[0], block[1], block[2], block[3]),
                            )
                        )
            else:
                page_kind = "drawing" if _looks_like_drawing(page) else "photo"
                blocks.append(
                    Block(
                        text="",
                        kind="figure",
                        page=index + 1,
                        meta={"needs_ocr": True},
                    )
                )
            doc.pages.append(
                ParsedPage(
                    page_no=index + 1,
                    blocks=blocks,
                    kind=page_kind,
                    has_text_layer=has_layer,
                    text_quality=quality,
                )
            )
    return doc


def _looks_like_drawing(page: object) -> bool:
    try:
        drawings = page.get_drawings()  # type: ignore[attr-defined]
        return len(drawings) > 40
    except Exception:
        return False


def _text_quality(text: str) -> float:
    words = re.findall(r"[A-Za-z]{2,}", text)
    if not words:
        return 0.0
    plausible = sum(1 for w in words if 2 <= len(w) <= 20)
    return plausible / len(words)


def _parse_docx(path: Path) -> ParsedDocument:
    import docx

    document = docx.Document(str(path))
    doc = ParsedDocument(title=_title_from(path), doc_type="docx")
    blocks: list[Block] = []
    section = ""
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style else ""
        if "heading" in style:
            section = text
            blocks.append(Block(text=text, kind="heading", section_path=section))
        else:
            blocks.append(Block(text=text, section_path=section))
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        if rows:
            blocks.append(Block(text="\n".join(rows), kind="table", section_path=section))
    doc.pages.append(ParsedPage(page_no=1, blocks=blocks))
    return doc


def _parse_xlsx(path: Path) -> ParsedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    doc = ParsedDocument(title=_title_from(path), doc_type="xlsx")
    blocks: list[Block] = []
    for sheet in wb.worksheets:
        rows: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                rows.append(" | ".join(cells))
            if len(rows) >= 5000:
                break
        if rows:
            blocks.append(
                Block(
                    text="\n".join(rows),
                    kind="table",
                    section_path=sheet.title,
                    meta={"sheet": sheet.title},
                )
            )
    wb.close()
    doc.pages.append(ParsedPage(page_no=1, blocks=blocks))
    return doc


def _parse_pptx(path: Path) -> ParsedDocument:
    import pptx

    presentation = pptx.Presentation(str(path))
    doc = ParsedDocument(title=_title_from(path), doc_type="pptx")
    for index, slide in enumerate(presentation.slides, 1):
        blocks: list[Block] = []

        def visit(
            shapes: Any, page: int, target: list[Block], ancestors: tuple[str, ...] = ()
        ) -> None:
            # Grouped tables/text remain evidence on the enclosing slide.
            for shape in shapes:
                location = (*ancestors, shape.name)
                if hasattr(shape, "shapes"):
                    visit(shape.shapes, page, target, location)
                    continue
                section = f"Slide {page} / " + " / ".join(location)
                if shape.has_table:
                    table = shape.table
                    rows = [
                        ["" if cell.is_spanned else cell.text for cell in row.cells]
                        for row in table.rows
                    ]
                    text = "\n".join(
                        " | ".join(re.sub(r"\s+", " ", value).strip() for value in row)
                        for row in rows
                    )
                    if any(value.strip() for row in rows for value in row):
                        target.append(
                            Block(
                                text=text,
                                kind="table",
                                page=page,
                                section_path=section,
                                meta={
                                    "slide": page,
                                    "shape_id": shape.shape_id,
                                    "rows": rows,
                                    "header_row": bool(table.first_row),
                                    "merged_cells": [
                                        {"row": r + 1, "column": c + 1,
                                         "row_span": cell.span_height,
                                         "column_span": cell.span_width}
                                        for r, row in enumerate(table.rows)
                                        for c, cell in enumerate(row.cells)
                                        if cell.is_merge_origin
                                    ],
                                },
                            )
                        )
                elif shape.has_text_frame:
                    text = "\n".join(
                        para.text.strip() for para in shape.text_frame.paragraphs
                        if para.text.strip()
                    )
                    if text:
                        target.append(Block(text=text, page=page, section_path=section))

        visit(slide.shapes, index, blocks)
        doc.pages.append(
            ParsedPage(page_no=index, blocks=blocks)
        )
    return doc


def _parse_eml(path: Path) -> ParsedDocument:
    message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    subject = str(message.get("subject", _title_from(path)))
    doc = ParsedDocument(title=subject, doc_type="email")
    header = "\n".join(
        f"{key}: {message.get(key)}"
        for key in ("From", "To", "Date", "Subject")
        if message.get(key)
    )
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                body += part.get_content()
    else:
        body = message.get_content()
    doc.pages.append(
        ParsedPage(page_no=1, blocks=[Block(text=header, kind="heading"), Block(text=str(body))])
    )
    return doc


def _parse_code(path: Path) -> ParsedDocument:
    text = path.read_text(encoding="utf-8", errors="replace")
    doc = ParsedDocument(title=_title_from(path), doc_type="code")
    doc.pages.append(ParsedPage(page_no=1, blocks=[Block(text=text, kind="code")]))
    return doc


def _parse_text(path: Path) -> ParsedDocument:
    text = path.read_text(encoding="utf-8", errors="replace")
    doc = ParsedDocument(title=_title_from(path), doc_type="text")
    doc.pages.append(ParsedPage(page_no=1, blocks=[Block(text=text)]))
    return doc


def _parse_image(path: Path) -> ParsedDocument:
    doc = ParsedDocument(title=_title_from(path), doc_type="image")
    doc.pages.append(
        ParsedPage(
            page_no=1,
            blocks=[
                Block(text="", kind="figure", meta={"needs_ocr": True, "image_path": str(path)})
            ],
            kind="drawing",
            has_text_layer=False,
            text_quality=0.0,
        )
    )
    return doc
