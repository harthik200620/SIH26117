from pathlib import Path
from types import SimpleNamespace

import pytest

from yantra_server.knowledge.ingest.ocr import complete_ocr
from yantra_server.knowledge.types import Block, ParsedDocument, ParsedPage
from yantra_server.tools.builtin.knowledge import ReadPagesArgs, ReadPagesTool, _parse_range
from yantra_server.vision.ocr import TextBox


def scan():
    return ParsedDocument(
        title="Scan",
        doc_type="image",
        pages=[ParsedPage(page_no=1, blocks=[Block(text="", meta={"needs_ocr": True})])],
    )


def test_ocr_preserves_table_rows_and_coordinates(tmp_path: Path):
    engine = SimpleNamespace(
        ocr_image=lambda p: [
            TextBox("10.12", (100, 20, 150, 30), 0.94),
            TextBox("10", (10, 20, 30, 30), 0.97),
            TextBox("reference", (10, 0, 80, 10), 0.99),
        ]
    )
    doc = complete_ocr(scan(), tmp_path / "scan.png", engine, 1)
    assert [b.text for b in doc.pages[0].blocks] == ["reference", "10 | 10.12"]
    assert doc.pages[0].blocks[1].bbox == (10, 20, 150, 30)
    assert doc.pages[0].text_quality == 0.94
    assert doc.meta["ocr_pages"] == [1]


def test_unavailable_ocr_and_page_limit_fail_closed(tmp_path: Path):
    engine = SimpleNamespace(ocr_image=lambda p: [])
    with pytest.raises(ValueError, match="coverage incomplete"):
        complete_ocr(scan(), tmp_path / "scan.png", engine, 1)
    with pytest.raises(ValueError, match="per-file limit"):
        complete_ocr(scan(), tmp_path / "scan.png", engine, 0)


def test_rotated_pdf_uses_unrotated_coordinates_without_changing_source(tmp_path: Path):
    import pymupdf
    from PIL import Image

    path = tmp_path / "rotated.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=200, height=300)
    page.set_rotation(90)
    pdf.save(path)
    pdf.close()
    original = path.read_bytes()

    def recognise(image_path):
        with Image.open(image_path) as image:
            assert image.size == (500, 750)
        return [TextBox("reading", (25, 50, 125, 100))]

    doc = complete_ocr(scan(), path, SimpleNamespace(ocr_image=recognise), 1)
    assert doc.pages[0].blocks[0].bbox == (10, 20, 50, 40)
    assert path.read_bytes() == original


@pytest.mark.parametrize("value", ["0", "-1", "1-99999999999", "3-1", "1,bad", "1,2-x", ""])
def test_page_ranges_reject_invalid_or_unbounded_requests(value):
    assert not _parse_range(value)


async def test_read_pages_applies_ocr_only_to_requested_pages(tmp_path, monkeypatch):
    import yantra_server.knowledge.ingest.parse as parsing
    import yantra_server.vision.ocr as vision

    doc = scan()
    doc.pages.append(ParsedPage(page_no=2, blocks=[Block(text="born digital")]))
    monkeypatch.setattr(parsing, "parse_document", lambda p: doc)
    monkeypatch.setattr(
        vision.OCREngine, "ocr_image", lambda s, p: [TextBox("scan evidence", (0, 0, 50, 10))]
    )
    path = tmp_path / "scan.png"
    path.touch()
    ctx = SimpleNamespace(
        resolve_path=lambda p: path,
        state=SimpleNamespace(
            config=SimpleNamespace(knowledge=SimpleNamespace(local_ocr=True, max_ocr_pages=1))
        ),
    )
    result = await ReadPagesTool().run(ReadPagesArgs(path="scan.png", pages="1"), ctx)
    assert result.ok and "scan evidence" in result.content
    assert "born digital" not in result.content
    assert result.data == {"pages": [1], "ocr_pages": [1]}


async def test_read_pages_does_not_report_empty_scan_as_success(tmp_path, monkeypatch):
    import yantra_server.knowledge.ingest.parse as parsing
    import yantra_server.vision.ocr as vision

    monkeypatch.setattr(parsing, "parse_document", lambda p: scan())
    monkeypatch.setattr(vision.OCREngine, "ocr_image", lambda s, p: [])
    path = tmp_path / "scan.png"
    path.touch()
    ctx = SimpleNamespace(
        resolve_path=lambda p: path,
        state=SimpleNamespace(
            config=SimpleNamespace(knowledge=SimpleNamespace(local_ocr=True, max_ocr_pages=1))
        ),
    )
    result = await ReadPagesTool().run(ReadPagesArgs(path="scan.png", pages="1"), ctx)
    assert not result.ok and "coverage incomplete" in result.error
