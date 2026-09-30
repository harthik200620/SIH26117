import hashlib

import pytest

from yantra_server.render.csv_binding import CSVTable, bind_csv_table


def test_explicit_numeric_typing_preserves_identifiers_and_source_order(tmp_path):
    raw = b'code,freight,price,note\r\n001,2,10,"two, parts"\r\n002,0,25,=1+1\r\n'
    path = tmp_path / "input.csv"
    path.write_bytes(raw)
    payload, evidence = bind_csv_table(
        CSVTable(source_path="input.csv", numeric_columns=["price", "freight"]),
        lambda p: tmp_path / p,
    )
    sheet = payload["sheets"][0]
    assert sheet["columns"] == ["code", "freight", "price", "note"]
    assert sheet["rows"] == [["001", 2, 10, "two, parts"], ["002", 0, 25, "=1+1"]]
    assert sheet["literal_inputs"] is True
    assert evidence == {"path": "input.csv", "sha256": hashlib.sha256(raw).hexdigest()}
    assert "records 2-3" in payload["appendix"]["citations"][0]
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "source,numeric",
    [
        ("x,x\n1,2", []),
        ("x,\n1,2", []),
        ("x,y\n1", []),
        ("x\n1,2", []),
        ("x\n", []),
        ("x\n1", ["missing"]),
        ("x\n1", ["x", "x"]),
        ("x\nNaN", ["x"]),
        ("x\n1e309", ["x"]),
        ("x\n1e-101", ["x"]),
        ("x\n1234567890123456", ["x"]),
        ("x\n10000000000000000000000000001", ["x"]),
        ("x\n0.10000000000000000000000000001", ["x"]),
        ('x\n"1,200"', ["x"]),
        ("x\n25 kg", ["x"]),
        ("x\n\x00", []),
    ],
)
def test_ambiguous_missing_and_lossy_data_fail_before_render(source, numeric, tmp_path):
    (tmp_path / "input.csv").write_text(source, encoding="utf-8")
    with pytest.raises(ValueError):
        bind_csv_table(
            CSVTable(source_path="input.csv", numeric_columns=numeric), lambda p: tmp_path / p
        )


def test_csv_size_is_bounded(tmp_path):
    (tmp_path / "input.csv").write_bytes(b"x\n" + b"a" * 2_000_001)
    with pytest.raises(ValueError, match="2 MB"):
        bind_csv_table(CSVTable(source_path="input.csv"), lambda p: tmp_path / p)


async def test_csv_render_preserves_source_text_formulas_and_hash(tmp_path, monkeypatch):
    import json

    import openpyxl

    from tests.helpers import make_ctx, make_state
    from yantra_server.render.service import RenderService
    from yantra_server.tools.builtin.render import RenderDocumentArgs, RenderDocumentTool

    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        source = ctx.workspace / "input.csv"
        source.write_text(
            "ID,price,freight,note\n001,10,2,=1+1\n002,25,0,plain\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "yantra_server.tools.builtin.render._render_service", lambda _: RenderService(tmp_path)
        )
        args = RenderDocumentArgs(
            type="xlsx",
            schema_id="csv_table",
            out_path="result.xlsx",
            data_json={
                "source_path": "input.csv",
                "numeric_columns": ["price", "freight"],
                "computed_columns": [{"name": "Total", "expression": "price + freight"}],
                "summary": "Synthetic example; review required.",
            },
        )
        result = await RenderDocumentTool().run(args, ctx)
        assert result.ok, result.error
        book = openpyxl.load_workbook(ctx.workspace / "result.xlsx")
        try:
            sheet = book.worksheets[0]
            assert sheet["A2"].value == "001"
            assert sheet["B2"].value == 10 and sheet["B2"].data_type == "n"
            assert sheet["D2"].value == "=1+1" and sheet["D2"].data_type == "s"
            assert sheet["E2"].value == "=(B2+C2)" and sheet["E2"].data_type == "f"
            assert "input.csv" in str(list(book.worksheets[1].values))
        finally:
            book.close()
        provenance = json.loads((ctx.workspace / "result.xlsx.provenance.json").read_text())
        assert provenance["source_document_hashes"] == [
            {"path": "input.csv", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        ]
        assert state.artifacts.read_bytes(result.data["snapshot_artifact_id"])
        for path in ("../secret.csv", "input.csv"):
            args.data_json["source_path"] = path
            if path == "input.csv":
                args.out_path = "input.csv"
            failed = await RenderDocumentTool().run(args, ctx)
            assert not failed.ok
        assert source.read_text().startswith("ID,price,")
    finally:
        state.db.dispose()
