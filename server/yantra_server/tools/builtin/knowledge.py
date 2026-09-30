"""Knowledge tools (SPEC §9.2): search_knowledge, get_chunk, find_documents, cite,
list_collections, read_pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from yantra_server.tools.base import Tool, ToolContext, ToolResult


def _scoped_collections(ctx: ToolContext, requested: list[str]) -> list[str]:
    if ctx.state.config.knowledge.auto_index_workspace:
        from yantra_server.workbench import collection_for

        return [collection_for(ctx.workspace)]
    return requested


def _path_allowed(ctx: ToolContext, path: str | None) -> bool:
    if not ctx.state.config.knowledge.auto_index_workspace:
        return True
    return bool(path and Path(path).resolve().is_relative_to(ctx.workspace.resolve()))


class SearchKnowledgeArgs(BaseModel):
    query: str
    collections: list[str] = Field(
        default_factory=list, description="Empty = all active collections"
    )
    k: int = Field(default=10, ge=1, le=30)
    mode: Literal["hybrid", "lexical", "dense", "visual"] = "hybrid"
    doc_type: str | None = Field(default=None, description="Filter to one document type")


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Search the indexed corpus (hybrid lexical+dense). Returns ranked chunks with ids, "
        "titles, pages and scores. Use exact tags like P-101A directly; they are matched exactly."
    )
    Args = SearchKnowledgeArgs
    side_effects = "read"

    async def run(self, args: SearchKnowledgeArgs, ctx: ToolContext) -> ToolResult:
        knowledge = ctx.state.knowledge
        if knowledge is None:
            return ToolResult.fail("no knowledge plane available (index a collection first)")
        filters = {"doc_type": args.doc_type} if args.doc_type else None
        hits = await knowledge.search(
            args.query,
            collections=_scoped_collections(ctx, args.collections),
            k=args.k,
            mode=args.mode,
            filters=filters,
        )
        if ctx.state.config.knowledge.auto_index_workspace:
            hits = [h for h in hits if _path_allowed(ctx, h.path)]
        if not hits:
            return ToolResult(summary="0 results", content="(no matching chunks)", data={"hits": 0})
        # Injection defence (SPEC §16.1): screen retrieved content; drop exfiltration attempts,
        # annotate instruction-like content. Chunks are data, never instructions.
        from yantra_server.guard.injection import screen_content, wrap_untrusted

        lines = []
        dropped = 0
        annotated = 0
        for hit in hits:
            screen = await screen_content(ctx.state.gateway, hit.text)
            page = f"p.{hit.page}" if hit.page is not None else ""
            header = f"[[c:{hit.chunk_id}]] {hit.title} {page} §{hit.section} ({hit.score:.3f})"
            if screen.verdict == "exfiltration_attempt":
                dropped += 1
                ctx.state.audit.append(
                    "guard",
                    "guard.injection",
                    {
                        "chunk_id": hit.chunk_id,
                        "verdict": screen.verdict,
                        "matched": screen.matched,
                    },
                )
                continue
            if screen.verdict == "instruction_like":
                annotated += 1
                lines.append(
                    header
                    + " [instruction-like content — treated as data]\n"
                    + wrap_untrusted(hit.text[:2400])
                )
            else:
                lines.append(f"{header}\n" + wrap_untrusted(hit.text[:2400]))
        if dropped:
            lines.append(f"[{dropped} chunk(s) dropped: exfiltration attempt in content]")
        top = hits[0]
        summary = f"{len(hits)} chunks · top: {top.title} p.{top.page} ({top.score:.2f})"
        if dropped or annotated:
            summary += f" ({dropped} dropped, {annotated} flagged by injection guard)"
        return ToolResult(
            summary=summary,
            content="\n".join(lines),
            data={
                "hits": len(hits),
                "chunk_ids": [h.chunk_id for h in hits],
                "citations": [h.citation() for h in hits],
                "guard": {"dropped": dropped, "annotated": annotated},
            },
        )


class GetChunkArgs(BaseModel):
    chunk_id: str
    expand: Literal["none", "parent", "page", "section"] = "none"


class GetChunkTool(Tool):
    name = "get_chunk"
    description = "Fetch the full text of a chunk by id, optionally expanded to its parent/page."
    Args = GetChunkArgs
    side_effects = "read"

    async def run(self, args: GetChunkArgs, ctx: ToolContext) -> ToolResult:
        knowledge = ctx.state.knowledge
        if knowledge is None:
            return ToolResult.fail("no knowledge plane available")
        chunk = knowledge.get_chunk(args.chunk_id, expand=args.expand)
        if chunk is None:
            return ToolResult.fail(f"no such chunk: {args.chunk_id}")
        if not _path_allowed(ctx, chunk.get("path")):
            return ToolResult.fail("chunk belongs to another workspace")
        return ToolResult(
            summary=f"{chunk['title']} p.{chunk['page']}",
            content=chunk["text"],
            data={"page": chunk["page"], "section": chunk["section"], "path": chunk["path"]},
        )


class FindDocumentsArgs(BaseModel):
    query: str
    collections: list[str] = Field(default_factory=list)
    doc_type: str | None = None
    k: int = Field(default=10, ge=1, le=50)


class FindDocumentsTool(Tool):
    name = "find_documents"
    description = "Browse documents by relevance (title/type/revision cards), not chunk text."
    Args = FindDocumentsArgs
    side_effects = "read"

    async def run(self, args: FindDocumentsArgs, ctx: ToolContext) -> ToolResult:
        knowledge = ctx.state.knowledge
        if knowledge is None:
            return ToolResult.fail("no knowledge plane available")
        hits = await knowledge.search(
            args.query,
            collections=_scoped_collections(ctx, args.collections),
            k=args.k * 3,
            mode="hybrid",
            filters={"doc_type": args.doc_type} if args.doc_type else None,
        )
        seen: dict[str, Any] = {}
        for hit in hits:
            if not _path_allowed(ctx, hit.path):
                continue
            if hit.document_id not in seen:
                seen[hit.document_id] = hit
            if len(seen) >= args.k:
                break
        lines = [f"- {h.title} [{h.doc_type or '?'}] {h.citation()}" for h in seen.values()]
        return ToolResult(
            summary=f"{len(seen)} document(s)",
            content="\n".join(lines) or "(none)",
            data={"documents": [h.document_id for h in seen.values()]},
        )


class CiteArgs(BaseModel):
    chunk_ids: list[str] = Field(min_length=1)


class CiteTool(Tool):
    name = "cite"
    description = "Resolve chunk ids to canonical citation strings (Title, Rev, p.N)."
    Args = CiteArgs
    side_effects = "read"

    async def run(self, args: CiteArgs, ctx: ToolContext) -> ToolResult:
        knowledge = ctx.state.knowledge
        if knowledge is None:
            return ToolResult.fail("no knowledge plane available")
        citations = []
        for chunk_id in args.chunk_ids:
            chunk = knowledge.get_chunk(chunk_id)
            if chunk and _path_allowed(ctx, chunk.get("path")):
                page = f" p.{chunk['page']}" if chunk["page"] else ""
                citations.append(f"[[c:{chunk_id}]] {chunk['title']}{page}")
        return ToolResult(
            summary=f"{len(citations)} citation(s)",
            content="\n".join(citations),
            data={"citations": citations},
        )


class ListCollectionsTool(Tool):
    name = "list_collections"
    description = "List knowledge collections with document and chunk counts."
    Args = BaseModel  # no args
    side_effects = "read"

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        knowledge = ctx.state.knowledge
        if knowledge is None:
            return ToolResult(summary="no collections", content="(knowledge plane not active)")
        cols = knowledge.list_collections()
        if ctx.state.config.knowledge.auto_index_workspace:
            allowed = _scoped_collections(ctx, [])
            cols = [c for c in cols if c["name"] in allowed]
        lines = [f"- {c['name']}: {c['documents']} docs, {c['chunks']} chunks" for c in cols]
        return ToolResult(
            summary=f"{len(cols)} collection(s)",
            content="\n".join(lines) or "(none)",
            data={"collections": [c["name"] for c in cols]},
        )


class ReadPagesArgs(BaseModel):
    path: str
    pages: str = Field(description="Page range like '1-3' or '5'")


class ReadPagesTool(Tool):
    name = "read_pages"
    description = "Extract text of specific pages from a PDF/DOCX/PPTX by page range."
    Args = ReadPagesArgs
    side_effects = "read"

    async def run(self, args: ReadPagesArgs, ctx: ToolContext) -> ToolResult:
        import asyncio

        from yantra_server.knowledge.ingest.parse import parse_document

        path = ctx.resolve_path(args.path)
        if not path.is_file():
            return ToolResult.fail(f"no such file: {args.path}")
        wanted = _parse_range(args.pages)
        if not wanted or len(wanted) > 500:
            return ToolResult.fail("Select between 1 and 500 positive page numbers")
        doc = await asyncio.to_thread(parse_document, Path(path))
        total = len(doc.pages)
        if any(page < 1 or page > total for page in wanted):
            return ToolResult.fail(f"Requested page outside document range 1-{total}")
        doc.pages = [page for page in doc.pages if page.page_no in wanted]
        missing = [
            page.page_no for page in doc.pages if any(b.meta.get("needs_ocr") for b in page.blocks)
        ]
        if missing:
            from yantra_server.knowledge.ingest.ocr import complete_ocr
            from yantra_server.vision.ocr import OCREngine

            config = ctx.state.config.knowledge
            if not config.local_ocr:
                return ToolResult.fail(f"Pages {missing} need OCR but local OCR is disabled")
            try:
                doc = await asyncio.to_thread(
                    complete_ocr, doc, Path(path), OCREngine(), config.max_ocr_pages
                )
            except Exception as exc:
                return ToolResult.fail(f"OCR coverage incomplete: {exc}")
        parts = []
        empty_pages = []
        for page in doc.pages:
            if page.page_no in wanted:
                text = "\n".join(b.text for b in page.blocks if b.text.strip())
                if text.strip():
                    parts.append(f"--- page {page.page_no} ---\n{text}")
                else:
                    empty_pages.append(page.page_no)
        if empty_pages:
            return ToolResult.fail(
                f"No extracted text on requested pages {empty_pages}; coverage incomplete"
            )
        if not parts:
            return ToolResult.fail(f"no text on pages {args.pages} (of {len(doc.pages)})")
        return ToolResult(
            summary=f"{len(parts)} page(s) from {args.path}",
            content=(
                "OCR transcription: visually verify ambiguous identifiers and readings.\n\n"
                if missing
                else ""
            )
            + "\n\n".join(parts),
            data={"pages": sorted(wanted), "ocr_pages": doc.meta.get("ocr_pages", [])},
        )


def _parse_range(spec: str) -> set[int]:
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit():
                start, end = int(lo), int(hi)
                if start < 1 or end < start or end - start >= 500:
                    return set()
                pages.update(range(start, end + 1))
            else:
                return set()
        elif part.isdigit():
            pages.add(int(part))
        else:
            return set()
        if len(pages) > 500 or 0 in pages:
            return set()
    return pages


def register_knowledge_tools(registry: Any) -> None:
    for tool in (
        SearchKnowledgeTool(),
        GetChunkTool(),
        FindDocumentsTool(),
        CiteTool(),
        ListCollectionsTool(),
        ReadPagesTool(),
    ):
        if registry.get(tool.name) is None:
            registry.register(tool)
