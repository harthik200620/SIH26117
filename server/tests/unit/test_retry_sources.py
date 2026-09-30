from types import SimpleNamespace

from tests.helpers import make_state
from yantra_server.conductor.executor import TaskExecutor
from yantra_server.db.models import StepRow


def test_retry_carries_exact_source_observations_with_scope_and_boundaries():
    state = make_state()
    try:
        with state.db.session() as db:
            for run, task, attempt, tool, ok, text in [
                ("r1", "t1", "a1", "read_file", True, "Measured input 72 m3/h"),
                ("r1", "t1", "a1", "read_pages", True, "Rule: original units"),
                ("other", "t1", "a1", "read_file", True, "OTHER RUN SECRET"),
                ("r1", "t2", "a1", "read_file", True, "OTHER TASK SECRET"),
                ("r1", "t1", "a1", "read_file", False, "FAILED READ"),
                ("r1", "t1", "a1", "render_document", True, "WRONG OUTPUT 9000"),
                ("r1", "t1", "a2", "read_file", True, "CURRENT ATTEMPT"),
            ]:
                db.add(
                    StepRow(
                        run_id=run,
                        task_id=task,
                        status=attempt,
                        n=1,
                        thought="PRIVATE MODEL THOUGHT",
                        observation=text,
                        action={"tool": tool, "args": {"path": tool + ".txt"}, "_ok": ok},
                    )
                )
        ctx = SimpleNamespace(state=state, run_id="r1")
        assert TaskExecutor._prior_source_observations(ctx, "t1", 1) == ""
        observed = TaskExecutor._prior_source_observations(ctx, "t1", 2)
        assert "Measured input 72 m3/h" in observed and "Rule: original units" in observed
        assert "<<<DOCUMENT" in observed and "not instructions" in observed
        for forbidden in [
            "SECRET",
            "FAILED READ",
            "WRONG OUTPUT",
            "CURRENT ATTEMPT",
            "PRIVATE MODEL",
        ]:
            assert forbidden not in observed
    finally:
        state.db.dispose()


def test_retry_source_excerpts_are_bounded_and_explicitly_truncated():
    state = make_state()
    try:
        with state.db.session() as db:
            for i in range(6):
                db.add(
                    StepRow(
                        run_id="r1",
                        task_id="t1",
                        status="a1",
                        n=i,
                        action={"tool": "read_pages", "args": {"path": f"{i}.pdf"}, "_ok": True},
                        observation="x" * 20000,
                    )
                )
        text = TaskExecutor._prior_source_observations(
            SimpleNamespace(state=state, run_id="r1"), "t1", 2
        )
        assert len(text) < 7000 and text.count("TRUNCATED") == 2
    finally:
        state.db.dispose()
