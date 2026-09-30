from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("tantivy")
from yantra_server.knowledge.index.lexical import LexicalIndex


def add(index: LexicalIndex, identifier: str, text: str) -> None:
    index.add(chunk_id=identifier, document_id=identifier, collection_id="test", title="Test",
              text=text, tags=[], doc_type="text", page=1, section="evidence", is_latest=True)


def test_multiline_evidence_and_multiple_commits_survive_reopen(tmp_path: Path) -> None:
    index = LexicalIndex(tmp_path / "space in index path")
    for n in range(12):
        add(index, str(n), f"Vendor | Price\nSYN-{n} | {480000+n}\nSiC material evidence")
        index.commit()
    reopened = LexicalIndex(index.path)
    hits = reopened.search("SiC material", limit=20)
    assert len(hits) == 12
    assert all("\nSYN-" in h[2]["text"] and "\nSiC" in h[2]["text"] for h in hits)
    reopened.delete_document("0")
    assert len(reopened.search("SiC material", limit=20)) == 11


def test_commit_failure_discards_writer_and_is_not_reported_as_success(tmp_path: Path) -> None:
    index = LexicalIndex(tmp_path / "index")
    failed = Mock()
    failed.commit.side_effect = ValueError("PermissionDenied opening fieldnorm")
    index._writer = failed
    with pytest.raises(ValueError, match="PermissionDenied"):
        index.commit()
    failed.rollback.assert_called_once()
    assert index._writer is None
    add(index, "retry", "replacementseal")
    index.commit()
    assert index.search("replacementseal")[0][2]["document_id"] == "retry"
