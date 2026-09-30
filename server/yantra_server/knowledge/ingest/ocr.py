"""Fill missing page text with provisioned local OCR; never read truth sidecars."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..types import Block, ParsedDocument


def complete_ocr(doc: ParsedDocument, path: Path, engine: Any, max_pages: int) -> ParsedDocument:
    missing = [page for page in doc.pages if any(b.meta.get("needs_ocr") for b in page.blocks)]
    if not missing:
        return doc
    if len(missing) > max_pages:
        raise ValueError(
            f"{len(missing)} pages need OCR; configured per-file limit is {max_pages}."
        )
    import pymupdf

    # PyMuPDF's optional API has incomplete annotations across supported builds.
    pdf_api: Any = pymupdf
    source = pdf_api.open(path) if path.suffix.lower() == ".pdf" else None
    try:
        with tempfile.TemporaryDirectory(prefix="yantra-ocr-") as directory:
            for page in missing:
                scale_x = scale_y = 1.0
                image_path = path
                if source is not None:
                    original = source[page.page_no - 1]
                    # PyMuPDF text coordinates use the unrotated page frame. Render
                    # there too; PDF display rotation otherwise turns upright scans sideways.
                    original.set_rotation(0)
                    if original.rect.width * original.rect.height * 2.5**2 > 25_000_000:
                        raise ValueError(f"Page {page.page_no} exceeds the OCR pixel budget.")
                    pixmap = original.get_pixmap(dpi=180, alpha=False)
                    image_path = Path(directory) / f"page-{page.page_no}.png"
                    pixmap.save(image_path)
                    scale_x = original.rect.width / pixmap.width
                    scale_y = original.rect.height / pixmap.height
                boxes = engine.ocr_image(image_path)
                if not boxes:
                    raise ValueError(
                        f"Page {page.page_no}: local OCR unavailable or no text recognised; coverage incomplete."
                    )
                ordered = sorted(boxes, key=lambda b: (b.bbox[1], b.bbox[0]))
                # Group boxes sharing a baseline so numeric columns stay on their row.
                lines: list[list[Any]] = []
                for box in ordered:
                    center = (box.bbox[1] + box.bbox[3]) / 2
                    if lines:
                        first = lines[-1][0]
                        previous = (first.bbox[1] + first.bbox[3]) / 2
                        tolerance = max(
                            2.0, min(box.bbox[3] - box.bbox[1], first.bbox[3] - first.bbox[1]) * 0.5
                        )
                    else:
                        previous, tolerance = float("inf"), 0.0
                    if abs(center - previous) <= tolerance:
                        lines[-1].append(box)
                    else:
                        lines.append([box])
                blocks = []
                for number, line in enumerate(lines, 1):
                    line.sort(key=lambda b: b.bbox[0])
                    blocks.append(
                        Block(
                            text=" | ".join(box.text for box in line),
                            page=page.page_no,
                            section_path=f"Page {page.page_no} / OCR row {number}",
                            bbox=(
                                min(b.bbox[0] for b in line) * scale_x,
                                min(b.bbox[1] for b in line) * scale_y,
                                max(b.bbox[2] for b in line) * scale_x,
                                max(b.bbox[3] for b in line) * scale_y,
                            ),
                            meta={
                                "ocr": True,
                                "ocr_engine": "local_rapidocr",
                                "confidence": min(b.confidence for b in line),
                            },
                        )
                    )
                page.blocks = [b for b in page.blocks if not b.meta.get("needs_ocr")] + blocks
                page.kind = "ocr"
                page.text_quality = min(b.confidence for b in boxes)
    finally:
        if source is not None:
            source.close()
    doc.meta["ocr_pages"] = [p.page_no for p in missing]
    return doc
