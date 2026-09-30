"""Persistent local BM25 search for hosts without reliable native index writers."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class SQLiteLexicalIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self._database = path / "lexical.sqlite"
        self._writer: sqlite3.Connection | None = None
        with closing(self._connect()) as connection:
            connection.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                chunk_id UNINDEXED, document_id UNINDEXED, collection_id UNINDEXED,
                title, body, tags, doc_type UNINDEXED, page UNINDEXED,
                section UNINDEXED, is_latest UNINDEXED, expanded,
                tokenize='unicode61')""")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database, timeout=30, check_same_thread=False)

    def writer(self) -> sqlite3.Connection:
        if self._writer is None:
            self._writer = self._connect()
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
        from .lexical import tokenize

        self.writer().execute(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                chunk_id,
                document_id,
                collection_id,
                title,
                text,
                " ".join(tags),
                doc_type,
                page,
                section,
                int(is_latest),
                " ".join(tokenize(text)),
            ),
        )

    def commit(self) -> None:
        if self._writer is None:
            return
        writer = self._writer
        try:
            writer.commit()
        except Exception:
            writer.rollback()
            raise
        finally:
            writer.close()
            self._writer = None

    def search(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        limit: int = 100,
        tag_boost: float = 3.0,
        latest_only: bool = True,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        # Quote every token: document text cannot introduce FTS operators.
        tokens = list(dict.fromkeys(re.findall(r"[^\W_]+(?:-[^\W_]+)*", query.lower())))[:256]
        if not tokens or limit <= 0:
            return []
        expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        where = ["chunks MATCH ?"]
        values: list[Any] = [expression]
        if collections:
            where.append("collection_id IN (" + ",".join("?" for _ in collections) + ")")
            values.extend(collections)
        if latest_only:
            where.append("is_latest=1")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT chunk_id,document_id,title,body,page,section,doc_type,"
                "bm25(chunks,0,0,0,1,1,?,0,0,0,0,1) AS score FROM chunks WHERE "
                + " AND ".join(where)
                + " ORDER BY score LIMIT ?",
                [max(0.0, float(tag_boost)), *values, min(limit, 10000)],
            ).fetchall()
        return [
            (
                str(row[0]),
                -float(row[7]),
                {
                    "chunk_id": row[0],
                    "document_id": row[1],
                    "title": row[2],
                    "text": row[3][:2000],
                    "page": row[4],
                    "section": row[5],
                    "doc_type": row[6],
                },
            )
            for row in rows
        ]

    def delete_document(self, document_id: str) -> None:
        self.writer().execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
        self.commit()
