from pathlib import Path
from types import SimpleNamespace

from yantra_server.tools.builtin.render import RenderDocumentArgs, RenderDocumentTool


def test_render_decoding_schema_rejects_wrong_rows_and_citations():
    import jsonschema
    import pytest

    from yantra_server.gateway.structured import tighten
    from yantra_server.tools.registry import ToolRegistry

    schema = tighten(ToolRegistry().spec_for(RenderDocumentTool()).parameters)
    valid = {
        "type": "xlsx",
        "schema_id": "data_table",
        "out_path": "offers.xlsx",
        "data_json": {
            "title": "Offers",
            "sheets": [{"columns": ["Basic", "Freight", "Total"], "rows": [[10, 2, "=A2+B2"]]}],
        },
    }
    jsonschema.validate(valid, schema)
    valid["data_json"]["sheets"][0]["rows"] = [{"Basic": 10}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(valid, schema)
    report = {
        "type": "docx",
        "schema_id": "report",
        "out_path": "note.docx",
        "data_json": {"title": "Inspection", "appendix": {"citations": [{"source": "input.pdf"}]}},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, schema)
    report["data_json"]["appendix"]["citations"] = ["input.pdf page 1"]
    jsonschema.validate(report, schema)
    report["data_json"] = "report.json"
    jsonschema.validate(report, schema)


def test_render_accepts_structured_payload_without_double_serialization(tmp_path: Path):
    payload = {
        "title": "Inspection",
        "sections": [{"heading": "Results", "paragraphs": ["Operator's note"]}],
    }
    args = RenderDocumentArgs(
        type="docx", schema_id="report", data_json=payload, out_path="note.docx"
    )
    ctx = SimpleNamespace(resolve_path=lambda value: tmp_path / value)
    assert RenderDocumentTool()._load_data(args.data_json, ctx) == payload
    assert RenderDocumentTool()._load_data('{"title":"Inspection"}', ctx) == {"title": "Inspection"}
    assert RenderDocumentTool()._load_data("{'title':'Inspection'}", ctx) is None


async def test_render_object_still_validates_deliverable_schema(tmp_path, monkeypatch):
    from yantra_server.render.service import RenderService

    monkeypatch.setattr(
        "yantra_server.tools.builtin.render._render_service", lambda ctx: RenderService(tmp_path)
    )
    ctx = SimpleNamespace(
        resolve_path=lambda p: tmp_path / p,
        run_id="run",
        task_id="task",
        state=SimpleNamespace(audit=SimpleNamespace(head=lambda: None)),
    )
    args = RenderDocumentArgs(
        type="docx", schema_id="report", data_json={"sections": []}, out_path="bad.docx"
    )
    result = await RenderDocumentTool().run(args, ctx)
    assert not result.ok and "does not match schema" in result.error
    assert not (tmp_path / "bad.docx").exists()


def test_spreadsheet_keeps_numeric_inputs_formulas_and_identifiers(tmp_path):
    import openpyxl

    from yantra_server.render.service import RenderService

    out = tmp_path / "table.xlsx"
    RenderService(tmp_path).render(
        doc_type="xlsx",
        schema_id="data_table",
        out_path=out,
        data={
            "title": "Offers",
            "sheets": [
                {
                    "columns": ["ID", "Basic", "Freight", "Total"],
                    "rows": [["001", 480000, 12000, "=B2+C2"]],
                }
            ],
        },
    )
    sheet = openpyxl.load_workbook(out).active
    assert sheet["A2"].value == "001"
    assert sheet["B2"].data_type == "n" and sheet["B2"].value == 480000
    assert sheet["D2"].data_type == "f" and sheet["D2"].value == "=B2+C2"


async def test_render_revisions_are_copied_to_scoped_artifact_storage(tmp_path, monkeypatch):
    import json

    from tests.helpers import make_ctx, make_state
    from yantra_server.db.models import RouterDecisionRow
    from yantra_server.render.service import RenderService

    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        monkeypatch.setattr(
            "yantra_server.tools.builtin.render._render_service", lambda _: RenderService(tmp_path)
        )
        with state.db.session() as db:
            db.add(
                RouterDecisionRow(
                    run_id=ctx.run_id, task_id=ctx.task_id, role="executor", chosen="fixture-model"
                )
            )
            db.add(
                RouterDecisionRow(
                    run_id="other-run",
                    task_id=ctx.task_id,
                    role="executor",
                    chosen="unrelated-model",
                )
            )
        tool = RenderDocumentTool()
        args = RenderDocumentArgs(
            type="xlsx",
            schema_id="data_table",
            out_path="result.xlsx",
            data_json={"sheets": [{"columns": ["Amount"], "rows": [[25]]}]},
        )
        first = await tool.run(args, ctx)
        assert first.ok
        snapshot = first.data["snapshot_artifact_id"]
        original = (ctx.workspace / "result.xlsx").read_bytes()
        original_provenance = (ctx.workspace / "result.xlsx.provenance.json").read_bytes()
        assert json.loads(original_provenance)["model_ids"] == ["fixture-model"]
        args.data_json["sheets"][0]["rows"] = [[70]]
        second = await tool.run(args, ctx)
        assert second.ok and second.data["snapshot_artifact_id"] != snapshot
        assert state.artifacts.read_bytes(snapshot) == original
        assert (
            state.artifacts.read_bytes(first.data["snapshot_provenance_id"]) == original_provenance
        )
        assert state.artifacts.read_bytes(second.data["snapshot_artifact_id"]) != original
        assert state.artifacts.get(snapshot).run_id == ctx.run_id
    finally:
        state.db.dispose()
