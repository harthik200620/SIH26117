"""Tantivy BM25 index with a tag-preserving tokenizer (SPEC §10.3 step 7, §10.4).

A custom tokenizer keeps `P-101A`-style tokens intact and also emits split variants, so a
query for the exact tag hits and a semantic query still matches the words around it.
"""

from __future__ import annotations

import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import tantivy

__all__ = ["LexicalIndex", "TantivyLexicalIndex", "tokenize"]

TAG_RE = re.compile(r"[A-Za-z]{1,4}-?\d{2,5}[A-Za-z]?|\d+\"|[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercased tokens that keep tag-like runs whole and also split around hyphens."""
    tokens: list[str] = []
    for match in TAG_RE.findall(text):
        low = match.lower()
        tokens.append(low)
        if "-" in low:
            tokens.extend(part for part in low.split("-") if part)
    return tokens


class TantivyLexicalIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("chunk_id", stored=True)
        builder.add_text_field("document_id", stored=True)
        builder.add_text_field("collection_id", stored=True, tokenizer_name="raw")
        builder.add_text_field("title", stored=True)
        builder.add_text_field("text", stored=True)
        builder.add_text_field("tags", stored=True)  # space-joined, tokenizer keeps tags
        builder.add_text_field("doc_type", stored=True, tokenizer_name="raw")
        builder.add_integer_field("page", stored=True, indexed=True)
        builder.add_text_field("section", stored=True)
        builder.add_integer_field("is_latest", stored=True, indexed=True)
        self.schema = builder.build()
        self.index = tantivy.Index(self.schema, path=str(path))
        self._writer: Any = None

    def writer(self) -> Any:
        if self._writer is None:
            # Bound Windows file-writer concurrency; bulk ingestion is already
            # serialized by KnowledgeService and shares memory with local inference.
            self._writer = self.index.writer(
                heap_size=64_000_000, num_threads=1 if sys.platform == "win32" else 0
            )
        return self._writer

    def add(
        self,
        *,
        chunk_id: str,
        document_id: str,
        collection_id: str,
        title: str,
        text: str,
        tags: list[str],
        doc_type: str,
        page: int,
        section: str,
        is_latest: bool,
    ) -> None:
        # Index the tag-preserving token stream alongside the natural text.
        expanded = " ".join(tokenize(text))
        self.writer().add_document(
            tantivy.Document(
                chunk_id=chunk_id,
                document_id=document_id,
                collection_id=collection_id,
                title=title,
                text=f"{text}\n{expanded}",
                tags=" ".join(tags),
                doc_type=doc_type,
                page=page,
                section=section,
                is_latest=1 if is_latest else 0,
            )
        )

    def commit(self) -> None:
        if self._writer is not None:
            writer = self._writer
            try:
                writer.commit()
                writer.wait_merging_threads()
            except Exception:
                # A failed batch must not be silently committed by a later document.
                # Do not retry an uncertain native commit; re-ingestion replaces the
                # document by ID after the caller records its failed status.
                with suppress(Exception):
                    writer.rollback()
                raise
            finally:
                self._writer = None
        self.index.reload()

    def search(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        limit: int = 100,
        tag_boost: float = 3.0,
        latest_only: bool = True,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        self.index.reload()
        searcher = self.index.searcher()
        tokens = tokenize(query)
        if not tokens:
            return []
        clauses: list[tuple[Any, Any]] = []
        for token in tokens:
            term = tantivy.Query.term_query(self.schema, "text", token)
            clauses.append((tantivy.Occur.Should, term))
            # exact tag-ish tokens get a boosted phrase match on the tags field
            if re.search(r"\d", token):
                tag_q = tantivy.Query.term_query(self.schema, "tags", token)
                clauses.append((tantivy.Occur.Should, tantivy.Query.boost_query(tag_q, tag_boost)))
        query_obj: Any = tantivy.Query.boolean_query(clauses)
        if collections:
            col_clauses = [
                (tantivy.Occur.Should, tantivy.Query.term_query(self.schema, "collection_id", c))
                for c in collections
            ]
            col_query = tantivy.Query.boolean_query(col_clauses)
            query_obj = tantivy.Query.boolean_query(
                [(tantivy.Occur.Must, query_obj), (tantivy.Occur.Must, col_query)]
            )
        if latest_only:
            latest_q = tantivy.Query.term_query(self.schema, "is_latest", 1)
            query_obj = tantivy.Query.boolean_query(
                [(tantivy.Occur.Must, query_obj), (tantivy.Occur.Must, latest_q)]
            )
        results: list[tuple[str, float, dict[str, Any]]] = []
        for score, address in searcher.search(query_obj, limit).hits:
            doc = searcher.doc(address)
            payload = {
                "chunk_id": doc["chunk_id"][0],
                "document_id": doc["document_id"][0],
                "title": doc["title"][0],
                "text": doc["text"][0].rsplit("\n", 1)[0][:2000],
                "page": doc["page"][0] if doc["page"] else None,
                "section": doc["section"][0] if doc["section"] else "",
                "doc_type": doc["doc_type"][0] if doc["doc_type"] else None,
            }
            results.append((str(payload["chunk_id"]), float(score), payload))
        return results

    def delete_document(self, document_id: str) -> None:
        writer = self.writer()
        writer.delete_documents("document_id", document_id)
        self.commit()


# Native Tantivy segment writes intermittently fail on this Windows deployment.
# Choose a persistent transactional backend rather than returning an empty index.
if sys.platform == "win32":
    from .sqlite_lexical import SQLiteLexicalIndex as LexicalIndex
else:
    LexicalIndex = TantivyLexicalIndex
