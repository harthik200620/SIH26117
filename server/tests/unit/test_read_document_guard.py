import pytest

from tests.helpers import make_ctx, make_state
from yantra_server.tools.builtin.fs import ReadFileArgs, ReadFileTool


@pytest.mark.parametrize("suffix", [".pdf", ".PDF", ".docx", ".pptx"])
async def test_structured_document_bytes_are_not_presented_as_text_lines(tmp_path, suffix):
    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        name = "source" + suffix
        # Some valid PDF encodings contain no NUL byte. Binary sniffing alone fails.
        (ctx.workspace / name).write_bytes(b"%PDF-1.4\n1 0 obj\ncompressed data\n")
        result = await ReadFileTool().run(ReadFileArgs(path=name), ctx)
        assert not result.ok and "read_pages" in result.error
        assert "compressed data" not in result.content
    finally:
        state.db.dispose()


async def test_ordinary_source_text_still_has_line_numbers(tmp_path):
    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        (ctx.workspace / "data.csv").write_text("id,value\n001,42\n")
        result = await ReadFileTool().run(ReadFileArgs(path="data.csv"), ctx)
        assert result.ok and "001,42" in result.content and result.data["total_lines"] == 2
    finally:
        state.db.dispose()
