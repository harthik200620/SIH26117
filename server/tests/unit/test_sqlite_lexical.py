from pathlib import Path

from yantra_server.knowledge.index.sqlite_lexical import SQLiteLexicalIndex


def test_collection_revision_unicode_and_persistence(tmp_path: Path):
    index = SQLiteLexicalIndex(tmp_path)
    for identifier, collection, latest in [
        ("old", "a", False),
        ("new", "a", True),
        ("other", "b", True),
    ]:
        index.add(
            chunk_id=identifier,
            document_id=identifier,
            collection_id=collection,
            title="Inspection",
            text="P-101A\nनिरीक्षण pending",
            tags=["P-101A"],
            doc_type="text",
            page=2,
            section="Evidence",
            is_latest=latest,
        )
    assert not index.search("pending")  # unfinished batches are not visible
    index.commit()
    reopened = SQLiteLexicalIndex(tmp_path)
    hits = reopened.search("निरीक्षण", collections=["a"])
    assert [h[2]["document_id"] for h in hits] == ["new"]
    assert hits[0][2]["page"] == 2 and "\n" in hits[0][2]["text"]
    assert len(reopened.search("P-101A", collections=["a"], latest_only=False)) == 2
    assert len(reopened.search('pending " OR *', collections=["a"])) == 1
    reopened.delete_document("new")
    assert not reopened.search("pending", collections=["a"])
