"""KnowledgeService (SPEC §10): the door the tools and conductor call for ingest + retrieve."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from yantra_server.db.base import Database, new_id
from yantra_server.db.models import ChunkRow, CollectionRow, DocumentRow, IngestErrorRow, PageRow
from yantra_server.observe.tracing import span

from .index.lexical import LexicalIndex
from .index.vector import VectorIndex
from .ingest.chunk import chunk_document
from .ingest.classify import classify_document
from .ingest.enrich import contextual_prefix, summarize
from .ingest.parse import parse_document
from .ingest.tags import all_tags, load_patterns
from .retrieve.fuse import weighted_rrf
from .types import RetrievedChunk

if TYPE_CHECKING:
    from yantra_server.config import YantraConfig
    from yantra_server.gateway.service import Gateway

log = logging.getLogger(__name__)

EMBED_INSTRUCTION = "Represent this industrial document passage for retrieval"
QUERY_INSTRUCTION = "Represent this engineer's question for retrieving passages"
DOC_DIM = 256  # document tier (MRL-truncated)
IGNORE_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".yantra"}
SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".txt",
    ".md",
    ".csv",
    ".eml",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".sql",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
}


@dataclass
class IngestStats:
    documents: int = 0
    chunks: int = 0
    errors: int = 0
    skipped: int = 0


class KnowledgeService:
    def __init__(self, config: YantraConfig, db: Database, gateway: Gateway) -> None:
        self.config = config
        self.db = db
        self.gateway = gateway
        self.root = config.paths.data_dir / "knowledge"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lexical: dict[str, LexicalIndex] = {}
        self._vector: VectorIndex | None = None
        self._ingest_lock = asyncio.Lock()
        self._ocr: Any = None
        self.tag_patterns = load_patterns(
            config.paths.assets_dir / config.knowledge.tag_patterns_file
            if not config.knowledge.tag_patterns_file.is_absolute()
            else config.knowledge.tag_patterns_file
        )

    # ------------------------------------------------------------- lazy backends

    def lexical(self, collection: str) -> LexicalIndex:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", collection):
            raise ValueError("Invalid collection name")
        if collection not in self._lexical:
            self._lexical[collection] = LexicalIndex(self.root / "lexical" / collection)
        return self._lexical[collection]

    def vector(self) -> VectorIndex:
        if self._vector is None:
            location = self.config.knowledge.qdrant_location
            if not location:
                if self.config.profile == "refinery":
                    location = "server:127.0.0.1:6333"
                else:
                    location = str(self.root / "qdrant")
            self._vector = VectorIndex(location, quantize=self.config.profile != "lite")
        return self._vector

    def embed_dim(self) -> int:
        # The active embed model's dimension; mock/tiny embeddings are 32-d.
        for manifest in self.gateway.router.registry.all():
            if "embed" in manifest.roles:
                dim = manifest.model_dump().get("embedding_dim")
                if dim:
                    return int(dim)
        return 32  # mock engine default

    # ------------------------------------------------------------- collections

    def ensure_collection(self, name: str, source_roots: list[str] | None = None) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("Collection names use 1-80 letters, numbers, underscores or hyphens")
        with self.db.session() as s:
            existing = s.execute(
                select(CollectionRow).where(CollectionRow.name == name)
            ).scalar_one_or_none()
            if existing:
                if source_roots:
                    existing.source_roots = list({*existing.source_roots, *source_roots})
                return existing.id
            row = CollectionRow(
                name=name,
                source_roots=source_roots or [],
                embedding_model=self._embed_model_id(),
            )
            s.add(row)
            s.flush()
            return row.id

    def _embed_model_id(self) -> str:
        if self.config.knowledge.lexical_only:
            return "lexical"
        from yantra_server.gateway.router import RouteNeed

        return self.gateway.router.route(RouteNeed(role="embed")).model

    def list_collections(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.execute(select(CollectionRow)).scalars().all()
            out = []
            for row in rows:
                docs = int(
                    s.execute(
                        select(func.count())
                        .select_from(DocumentRow)
                        .where(DocumentRow.collection_id == row.id)
                    ).scalar_one()
                )
                chunks = int(
                    s.execute(
                        select(func.count())
                        .select_from(ChunkRow)
                        .where(ChunkRow.collection_id == row.id)
                    ).scalar_one()
                )
                errors = int(
                    s.execute(
                        select(func.count())
                        .select_from(IngestErrorRow)
                        .where(IngestErrorRow.collection_id == row.id)
                    ).scalar_one()
                )
                out.append(
                    {
                        "name": row.name,
                        "documents": docs,
                        "chunks": chunks,
                        "pending": 0,
                        "errors": errors,
                    }
                )
            return out

    def dashboard_status(self) -> dict[str, Any]:
        return {"collections": self.list_collections()}

    def _collection_id(self, name: str) -> str | None:
        with self.db.session() as s:
            row = s.execute(
                select(CollectionRow).where(CollectionRow.name == name)
            ).scalar_one_or_none()
            return row.id if row else None

    # ------------------------------------------------------------- ingest

    async def ingest_path(
        self, source: Path, collection: str, *, on_progress: Any = None
    ) -> IngestStats:
        async with self._ingest_lock:
            return await self._ingest_path(source, collection, on_progress=on_progress)

    async def _ingest_path(
        self, source: Path, collection: str, *, on_progress: Any = None
    ) -> IngestStats:
        """Discover, parse, chunk, enrich, embed and store every supported file under `source`."""
        collection_id = self.ensure_collection(collection, [str(source)])
        stats = IngestStats()
        is_file = await __import__("anyio").to_thread.run_sync(source.is_file)
        files = [source] if is_file else await asyncio.to_thread(self._discover, source)
        if not is_file:
            with self.db.session() as db:
                indexed = [
                    (row.id, row.path)
                    for row in db.execute(
                        select(DocumentRow).where(DocumentRow.collection_id == collection_id)
                    ).scalars()
                ]
            stale = await asyncio.to_thread(
                lambda: [
                    (identifier, path) for identifier, path in indexed if not Path(path).is_file()
                ]
            )
            for document_id, _ in stale:
                self._purge_document(document_id, collection_id)
                self.lexical(collection).delete_document(document_id)
                with self.db.session() as db:
                    row = db.get(DocumentRow, document_id)
                    if row:
                        row.status = "deleted"
        for path in files:
            try:
                changed = await self._ingest_file(path, collection, collection_id)
                if changed:
                    stats.documents += 1
                else:
                    stats.skipped += 1
            except Exception as exc:
                stats.errors += 1
                log.exception("ingest failed for %s", path)
                self._record_error(collection_id, None, "ingest", f"{path}: {exc}")
            if on_progress is not None:
                on_progress(stats)
        self.lexical(collection).commit()
        with self.db.session() as s:
            stats.chunks = int(
                s.execute(
                    select(func.count())
                    .select_from(ChunkRow)
                    .where(ChunkRow.collection_id == collection_id)
                ).scalar_one()
            )
        return stats

    def _discover(self, root: Path) -> list[Path]:
        ignore = self._load_ignore(root)
        found: list[Path] = []
        root = root.resolve()
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith("."))
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix.lower() not in SUPPORTED_SUFFIXES or name.startswith("."):
                    continue
                if not path.resolve().is_relative_to(root) or path.stat().st_size > 25 * 1024**2:
                    continue
                rel = path.relative_to(root).as_posix()
                if any(rel.startswith(pat) or pat in rel for pat in ignore):
                    continue
                found.append(path)
                if len(found) >= 2000:
                    raise ValueError(
                        "Workspace exceeds 2,000 eligible files; select a smaller folder"
                    )
        return found

    def _load_ignore(self, root: Path) -> list[str]:
        ignore_file = root / ".yantraignore"
        if ignore_file.is_file():
            return [
                line.strip()
                for line in ignore_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        return []

    async def _ingest_file(self, path: Path, collection: str, collection_id: str) -> bool:
        # Parser changes require re-ingestion even when the file bytes are unchanged.
        fingerprint = (
            (await asyncio.to_thread(_fingerprint, path))
            + ":" + self._embed_model_id() + f":parser-4:ocr-{self.config.knowledge.local_ocr}"
        )
        with self.db.session() as s:
            existing = s.execute(
                select(DocumentRow).where(
                    DocumentRow.collection_id == collection_id,
                    DocumentRow.path == str(path),
                )
            ).scalar_one_or_none()
            if existing and existing.fingerprint == fingerprint and existing.status == "indexed":
                return False

        with span("ingest.document", kind="ingest.job", path=str(path)):
            doc = await asyncio.to_thread(parse_document, path)
            missing_pages = [p.page_no for p in doc.pages if any(b.meta.get("needs_ocr") for b in p.blocks)]
            if missing_pages:
                if not self.config.knowledge.local_ocr:
                    raise ValueError(f"OCR required on pages {missing_pages}; local OCR is disabled.")
                from yantra_server.knowledge.ingest.ocr import complete_ocr
                from yantra_server.vision.ocr import OCREngine

                if self._ocr is None:
                    self._ocr = OCREngine()
                doc = await asyncio.to_thread(
                    complete_ocr, doc, path, self._ocr, self.config.knowledge.max_ocr_pages
                )
            if not doc.full_text().strip():
                raise ValueError(
                    "No extractable text. This document needs an installed local OCR/vision model."
                )
            doc = classify_document(doc, path)
            chunks = chunk_document(
                doc,
                self.config.knowledge.chunk_tokens_child,
                self.config.knowledge.chunk_tokens_parent,
                self.config.knowledge.chunk_overlap_ratio,
            )
            enrichment = await summarize(
                None if self.config.knowledge.lexical_only else self.gateway, doc
            )

            document_id = self._upsert_document(path, doc, collection_id, fingerprint)
            self._store_pages(document_id, doc)

            child_chunks = [c for c in chunks if c.kind != "parent"]
            texts_for_embedding: list[str] = []
            for chunk in child_chunks:
                chunk.context_prefix = contextual_prefix(chunk, doc, enrichment)
                texts_for_embedding.append(f"{chunk.context_prefix}\n{chunk.text}")

            vectors = (
                []
                if self.config.knowledge.lexical_only
                else await self.gateway.embed(
                    texts_for_embedding, role="embed", instruction=EMBED_INSTRUCTION
                )
            )
            self._store_chunks(document_id, collection, collection_id, doc, child_chunks, vectors)
            # document tier
            if not self.config.knowledge.lexical_only:
                doc_vector = await self._document_vector(doc, enrichment)
                self._store_document_tier(collection, collection_id, document_id, doc, doc_vector)
            self.lexical(collection).commit()
            self._mark_indexed(document_id)
        return True

    async def _document_vector(self, doc: Any, enrichment: Any) -> list[float]:
        text = f"{doc.title}\n{enrichment.document_summary}"
        vectors = await self.gateway.embed([text], role="embed", instruction=EMBED_INSTRUCTION)
        vec = vectors[0]
        return vec[:DOC_DIM] if len(vec) > DOC_DIM else vec

    def _upsert_document(self, path: Path, doc: Any, collection_id: str, fingerprint: str) -> str:
        with self.db.session() as s:
            existing = s.execute(
                select(DocumentRow).where(
                    DocumentRow.collection_id == collection_id, DocumentRow.path == str(path)
                )
            ).scalar_one_or_none()
            if existing:
                self._purge_document(existing.id, collection_id)
                document_id = existing.id
                row = existing
            else:
                document_id = new_id()
                row = DocumentRow(id=document_id, collection_id=collection_id, path=str(path))
                s.add(row)
            row.title = doc.title
            row.doc_type = doc.doc_type
            row.language = doc.language
            row.fingerprint = fingerprint
            row.sha256 = fingerprint.split(":", 1)[0]
            row.size = path.stat().st_size
            row.mtime = path.stat().st_mtime
            row.page_count = len(doc.pages)
            row.revision = doc.revision
            row.doc_number = doc.doc_number
            row.is_latest = True
            row.status = "parsing"
            s.flush()
            self._link_version(s, row)
            return document_id

    def _link_version(self, s: Any, row: DocumentRow) -> None:
        from yantra_server.db.models import DocumentVersionRow

        group_key = (row.doc_number or row.title or "").strip().lower()
        if not group_key:
            return
        siblings = (
            s.execute(select(DocumentVersionRow).where(DocumentVersionRow.group_key == group_key))
            .scalars()
            .all()
        )
        for sib in siblings:
            sib.is_latest = False
            other = s.get(DocumentRow, sib.document_id)
            if other and other.id != row.id:
                other.is_latest = False
        s.add(
            DocumentVersionRow(
                group_key=group_key,
                document_id=row.id,
                revision=row.revision,
                ordinal=len(siblings),
                is_latest=True,
            )
        )

    def _store_pages(self, document_id: str, doc: Any) -> None:
        with self.db.session() as s:
            for page in doc.pages:
                s.merge(
                    PageRow(
                        id=new_id(),
                        document_id=document_id,
                        page_no=page.page_no,
                        kind=page.kind,
                        has_text_layer=page.has_text_layer,
                        text_quality=page.text_quality,
                    )
                )

    def _store_chunks(
        self,
        document_id: str,
        collection: str,
        collection_id: str,
        doc: Any,
        chunks: list[Any],
        vectors: list[list[float]],
    ) -> None:
        lex = self.lexical(collection)
        vec_index = self.vector() if vectors else None
        chunk_collection = f"chunks_{collection}"
        dim = len(vectors[0]) if vectors else self.embed_dim()
        if vec_index is not None:
            vec_index.ensure_collection(chunk_collection, dim)

        ids: list[str] = []
        payloads: list[dict[str, Any]] = []
        with self.db.session() as s:
            for chunk in chunks:
                chunk_id = new_id()
                tags = all_tags(chunk.text, self.tag_patterns)
                s.add(
                    ChunkRow(
                        id=chunk_id,
                        document_id=document_id,
                        collection_id=collection_id,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        section_path=chunk.section_path,
                        text=chunk.text,
                        context_prefix=chunk.context_prefix,
                        token_count=chunk.token_count,
                        kind=chunk.kind,
                        meta={"tags": tags},
                    )
                )
                lex.add(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    collection_id=collection_id,
                    title=doc.title,
                    text=chunk.text,
                    tags=tags,
                    doc_type=doc.doc_type,
                    page=chunk.page_start,
                    section=chunk.section_path,
                    is_latest=True,
                )
                ids.append(chunk_id)
                payloads.append(
                    {
                        "document_id": document_id,
                        "title": doc.title,
                        "page": chunk.page_start,
                        "section": chunk.section_path,
                        "doc_type": doc.doc_type,
                        "is_latest": True,
                        "text": chunk.text[:2000],
                    }
                )
        if ids and vec_index is not None:
            vec_index.upsert(chunk_collection, ids, vectors, payloads)

    def _store_document_tier(
        self, collection: str, collection_id: str, document_id: str, doc: Any, vector: list[float]
    ) -> None:
        vec_index = self.vector()
        docs_collection = f"docs_{collection}"
        vec_index.ensure_collection(docs_collection, len(vector))
        vec_index.upsert(
            docs_collection,
            [document_id],
            [vector],
            [{"document_id": document_id, "title": doc.title, "doc_type": doc.doc_type}],
        )

    def _mark_indexed(self, document_id: str) -> None:
        with self.db.session() as s:
            row = s.get(DocumentRow, document_id)
            if row:
                row.status = "indexed"

    def _purge_document(self, document_id: str, collection_id: str) -> None:
        with self.db.session() as db:
            collection = db.get(CollectionRow, collection_id)
            name = collection.name if collection else None
        if name:
            self.lexical(name).delete_document(document_id)
        with self.db.session() as s:
            from sqlalchemy import delete

            s.execute(delete(ChunkRow).where(ChunkRow.document_id == document_id))
            s.execute(delete(PageRow).where(PageRow.document_id == document_id))

    def _record_error(
        self, collection_id: str, document_id: str | None, stage: str, error: str
    ) -> None:
        with self.db.session() as s:
            s.add(
                IngestErrorRow(
                    collection_id=collection_id,
                    document_id=document_id,
                    stage=stage,
                    error=error[:2000],
                )
            )

    # ------------------------------------------------------------- retrieve

    async def search(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        k: int = 10,
        mode: str = "hybrid",
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        with span("retrieval", kind="retrieval", query=query[:200], mode=mode) as sp:
            if self.config.knowledge.lexical_only:
                mode = "lexical"
            collection_names = collections or [c["name"] for c in self.list_collections()]
            if not collection_names:
                return []
            ranked: dict[str, list[tuple[str, float, dict[str, Any]]]] = {}

            if mode in ("hybrid", "lexical"):
                lexical_hits: list[tuple[str, float, dict[str, Any]]] = []
                for name in collection_names:
                    lexical_hits.extend(
                        self.lexical(name).search(query, collections=None, limit=100)
                    )
                ranked["lexical"] = lexical_hits[:100]
                sp.set("lexical_candidates", len(lexical_hits))

            if mode in ("hybrid", "dense"):
                query_vecs = await self.gateway.embed(
                    [query], role="embed", instruction=QUERY_INSTRUCTION
                )
                dense_hits: list[tuple[str, float, dict[str, Any]]] = []
                for name in collection_names:
                    dense_hits.extend(
                        self.vector().search(
                            f"chunks_{name}", query_vecs[0], limit=100, filters=filters
                        )
                    )
                ranked["dense"] = dense_hits[:100]
                sp.set("dense_candidates", len(dense_hits))

            weights = self.config.knowledge.fusion_weights
            fused = weighted_rrf(
                ranked, weights, doc_cap_ratio=0.4, limit=self.config.knowledge.rerank_depth
            )
            sp.set("fused_candidates", len(fused))

            reranked = (
                fused if self.config.knowledge.lexical_only else await self._rerank(query, fused)
            )
            final = [c for c in reranked if self.get_chunk(c.chunk_id) is not None][:k]
            sp.set("returned", len(final))
            return [self._to_retrieved(c) for c in final]

    async def _rerank(self, query: str, candidates: list[Any]) -> list[Any]:
        if not candidates:
            return []
        docs = [str(c.payload.get("text", "")) for c in candidates]
        try:
            scores = await self.gateway.rerank(query, docs, role="rerank")
        except Exception:
            return candidates
        for cand, score in zip(candidates, scores, strict=False):
            cand.fused = score
        return sorted(candidates, key=lambda c: -c.fused)

    def _to_retrieved(self, candidate: Any) -> RetrievedChunk:
        payload = candidate.payload
        current = self.get_chunk(candidate.chunk_id) or {}
        text = str(current.get("text", payload.get("text", "")))
        return RetrievedChunk(
            chunk_id=candidate.chunk_id,
            document_id=str(payload.get("document_id", "")),
            title=str(payload.get("title", "")),
            path=str(current.get("path", "")),
            page=payload.get("page"),
            section=str(payload.get("section", "")),
            text=text,
            score=round(candidate.fused, 4),
            doc_type=payload.get("doc_type"),
            source="+".join(candidate.scores.keys()),
            snippet=text[:160],
        )

    def chunk_text(self, chunk_id: str) -> str | None:
        with self.db.session() as s:
            row = s.get(ChunkRow, chunk_id)
            return row.text if row else None

    def get_chunk(self, chunk_id: str, expand: str = "none") -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(ChunkRow, chunk_id)
            if row is None:
                return None
            text = row.text
            if expand == "parent" and row.parent_chunk_id:
                parent = s.get(ChunkRow, row.parent_chunk_id)
                if parent:
                    text = parent.text
            doc = s.get(DocumentRow, row.document_id)
            if doc is None or doc.status != "indexed":
                return None
            return {
                "chunk_id": chunk_id,
                "text": text,
                "title": doc.title if doc else "",
                "page": row.page_start,
                "section": row.section_path,
                "path": doc.path if doc else "",
            }

    def close(self) -> None:
        if self._vector is not None:
            self._vector.close()


def _fingerprint(path: Path) -> str:
    """Hash all bytes so changes after the first megabyte cannot escape reindexing."""
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()
