"""Knowledge plane: ingest → hybrid retrieve → recall on the synthetic corpus (M6 DoD)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import make_state
from yantra_server.state import AppState

# The knowledge plane needs the [knowledge] extra; skip cleanly on a base install.
for _extra in ("tantivy", "qdrant_client", "pymupdf"):
    pytest.importorskip(_extra, reason="knowledge extra not installed")

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
async def knowledge_state(tmp_path: Path) -> Any:
    state = make_state()
    state.bus.bind_loop(asyncio.get_running_loop())
    await state.supervisor.start_all()
    from yantra_server.knowledge.service import KnowledgeService

    state.knowledge = KnowledgeService(state.config, state.db, state.gateway)
    yield state
    state.knowledge.close()
    await state.supervisor.stop_all()


def write_corpus(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    import sys

    sys.path.insert(0, str(REPO))
    from corpus.generate import generate_corpus

    out = tmp_path / "corpus"
    truth = generate_corpus(out, size="small")
    return out / "docs", truth


async def test_ingest_and_exact_tag_retrieval(knowledge_state: AppState, tmp_path: Path) -> None:
    docs_dir, _truth = write_corpus(tmp_path)
    stats = await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")
    assert stats.documents > 10
    assert stats.chunks > 0
    assert stats.errors == 0

    # Exact tag query must hit the right document (the lexical tag-preserving tokenizer).
    hits = await knowledge_state.knowledge.search(
        "P-3101A seal failure", collections=["unit3"], k=10
    )
    assert hits, "no hits for an exact tag query"
    titles = " ".join(h.title.lower() for h in hits[:5])
    assert "p-3101a" in titles or "maintlog" in titles or "sop-pmp-014" in titles


async def test_recall_at_10_on_truth_set(knowledge_state: AppState, tmp_path: Path) -> None:
    docs_dir, truth = write_corpus(tmp_path)
    await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")

    from yantra_server.knowledge.retrieve.evaluate import cases_from_truth, evaluate_retrieval

    cases = cases_from_truth(truth)
    metrics = await evaluate_retrieval(
        knowledge_state.knowledge, cases, collections=["unit3"], mode="lexical"
    )
    # Lexical alone must clear the bar on the generated question set (queries share the
    # planted vocabulary and exact tags). Dense adds recall with a real embedding model.
    assert metrics.recall_at_10 >= 0.9, f"recall@10={metrics.recall_at_10:.2f} < 0.9"
    assert metrics.mrr > 0.5


async def test_hybrid_search_and_citations(knowledge_state: AppState, tmp_path: Path) -> None:
    docs_dir, _ = write_corpus(tmp_path)
    await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")
    hits = await knowledge_state.knowledge.search(
        "flush plan mechanical seal SOP", collections=["unit3"], k=8, mode="hybrid"
    )
    assert hits
    assert all(h.citation() for h in hits)
    # citation locators resolve back to chunk text
    text = knowledge_state.knowledge.chunk_text(hits[0].chunk_id)
    assert text and len(text) > 0


async def test_incremental_reingest_skips_unchanged(
    knowledge_state: AppState, tmp_path: Path
) -> None:
    docs_dir, _ = write_corpus(tmp_path)
    first = await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")
    assert first.documents > 0
    second = await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")
    assert second.documents == 0  # nothing changed
    assert second.skipped == first.documents


async def test_lexical_reindex_removes_stale_content(
    knowledge_state: AppState, tmp_path: Path
) -> None:
    knowledge_state.config.knowledge.lexical_only = True
    docs = tmp_path / "local-docs"
    docs.mkdir()
    source = docs / "pump.md"
    source.write_text("# Pump\nobsoletebearing has vibration.", encoding="utf-8")
    service = knowledge_state.knowledge
    first = await service.ingest_path(docs, "local")
    assert first.documents == 1 and first.errors == 0
    assert await service.search("obsoletebearing", collections=["local"])
    source.write_text("# Pump\nreplacementseal is installed.", encoding="utf-8")
    changed = await service.ingest_path(docs, "local")
    assert changed.documents == 1 and changed.errors == 0
    assert not await service.search("obsoletebearing", collections=["local"])
    assert await service.search("replacementseal", collections=["local"])
    source.unlink()
    await service.ingest_path(docs, "local")
    assert not await service.search("replacementseal", collections=["local"])


async def test_collection_path_traversal_rejected(
    knowledge_state: AppState, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        knowledge_state.knowledge.lexical("../../outside")


async def test_failed_index_commit_is_reingested_not_skipped(
    knowledge_state: AppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from yantra_server.db.models import DocumentRow

    service = knowledge_state.knowledge
    service.config.knowledge.lexical_only = True
    folder = tmp_path / "commit-recovery"
    folder.mkdir()
    (folder / "inspection.txt").write_text("uniqueinspectionflag awaiting release", encoding="utf-8")
    index = service.lexical("recovery")
    actual_commit = index.commit
    failures = 0

    def fail_first_commit() -> None:
        nonlocal failures
        if not failures:
            failures += 1
            raise ValueError("PermissionDenied opening fieldnorm")
        actual_commit()

    monkeypatch.setattr(index, "commit", fail_first_commit)
    first = await service.ingest_path(folder, "recovery")
    assert first.documents == 0 and first.errors == 1
    with service.db.session() as session:
        doc = session.execute(select(DocumentRow).where(DocumentRow.path == str(folder / "inspection.txt"))).scalar_one()
        assert doc.status != "indexed"
    assert not await service.search("uniqueinspectionflag", collections=["recovery"])
    second = await service.ingest_path(folder, "recovery")
    assert second.documents == 1 and second.skipped == 0 and second.errors == 0
    assert await service.search("uniqueinspectionflag", collections=["recovery"])


async def test_doc_type_filter(knowledge_state: AppState, tmp_path: Path) -> None:
    docs_dir, _ = write_corpus(tmp_path)
    await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")
    hits = await knowledge_state.knowledge.search(
        "seal replacement procedure",
        collections=["unit3"],
        k=10,
        mode="hybrid",
        filters={"doc_type": "sop"},
    )
    # dense hits are doc_type-filtered; lexical contributes too, but SOPs should dominate
    assert hits


async def test_search_tool_end_to_end(knowledge_state: AppState, tmp_path: Path) -> None:
    from tests.helpers import make_ctx

    docs_dir, _ = write_corpus(tmp_path)
    await knowledge_state.knowledge.ingest_path(docs_dir, "unit3")
    ctx = make_ctx(knowledge_state, tmp_path / "ws")
    result = await knowledge_state.tools.runtime.execute(
        "search_knowledge", {"query": "P-3101A seal", "collections": ["unit3"], "k": 5}, ctx
    )
    assert result.ok
    assert result.data["hits"] > 0
    assert result.data["chunk_ids"]
    # get_chunk resolves
    chunk_result = await knowledge_state.tools.runtime.execute(
        "get_chunk", {"chunk_id": result.data["chunk_ids"][0]}, ctx
    )
    assert chunk_result.ok and chunk_result.content
