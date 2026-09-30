from types import SimpleNamespace

from tests.helpers import make_ctx, make_state
from yantra_server.conductor.types import EvidenceReview, FinishArgs, PlanTask, ReviewedCriterion
from yantra_server.conductor.verifier import Verifier


async def test_compact_review_uses_original_request_not_planner_paraphrase(tmp_path, monkeypatch):
    state = make_state()
    try:
        state.config.execution.compact_planning = True
        state.config.knowledge.auto_index_workspace = False
        ctx = make_ctx(state, tmp_path / "workspace")
        seen = []

        async def chat(request):
            seen.append(request)
            return SimpleNamespace(
                parsed=EvidenceReview(
                    criteria=[
                        ReviewedCriterion(
                            requirement="Recommend an option",
                            status="missing",
                            evidence="No choice named",
                        )
                    ],
                    all_requirements_covered=True,
                )
            )

        monkeypatch.setattr(state.gateway, "chat", chat)
        task = PlanTask(
            id="t1",
            title="INVENTED TITLE REQUIREMENT",
            role="analyst",
            acceptance=[{"kind": "file_exists", "path": "result.xlsx"}],
            intent="INVENTED PLANNER REQUIREMENT\nOriginal objective: Recommend the preferred eligible option and explain why.",
        )
        verifier = Verifier(state, state.extras["roster"], ctx.workspace, "r1", ctx.sandbox)
        result = await verifier._review(task, FinishArgs(summary="file exists"), [])
        prompt = seen[0].messages[1].text()
        assert "Recommend the preferred eligible option and explain why." in prompt
        assert "INVENTED" not in prompt
        assert result.verdict == "fail"
    finally:
        state.db.dispose()
