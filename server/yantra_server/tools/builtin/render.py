"""Deliverable + chart/diagram tools (SPEC §9.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from yantra_server.tools.base import Tool, ToolContext, ToolResult


class RenderDocumentArgs(BaseModel):
    type: Literal["docx", "xlsx", "pptx", "pdf", "md"]
    schema_id: str = Field(description="report|equipment_list|work_order_draft|pid_review|…")
    data_json: dict[str, Any] | str = Field(
        description="A JSON object matching the schema (preferred), or a path to a JSON file"
    )
    out_path: str
    template_id: str = "house"


class RenderDocumentTool(Tool):
    name = "render_document"
    description = (
        "Render a validated deliverable JSON into DOCX/XLSX/PPTX/PDF/MD via a house template. "
        "For CSV-to-XLSX use schema_id=csv_table: source_path, numeric_columns (exact headers), "
        "computed_columns [{name,expression}], summary and appendix. Source rows are loaded "
        "directly without retyping, filtering or changing values; no sheets/rows are supplied. "
        "Pass data_json as an object. For XLSX use schema_id=data_table with sheets, summary and appendix. "
        "Each sheet has caption, columns (strings), rows (arrays matching columns). "
        "Numeric inputs must be JSON numbers, not quoted strings. Prefer computed_columns: "
        "[{name:'Total',expression:'price * quantity'}] appended to each sheet. Rows contain input cells only. "
        "Expressions use exact input/prior computed column names, + - * / comparisons, and/or/not, "
        "IF(condition, yes, no), AND, OR, NOT; equality may use == or =. "
        "use col('Unit price') for labels with spaces. No leading '='. Raw Excel formula strings instead "
        "require A1 addresses. Put the decision in summary and citations/assumptions/"
        "unverified_claims in appendix (arrays of strings)."
    )
    Args = RenderDocumentArgs
    side_effects = "render"
    idempotent = False

    @property
    def override_schema(self) -> dict[str, Any]:
        """Constrain inline payloads to the selected deliverable schema.

        File paths/JSON strings remain supported for older callers; their content
        is still validated by RenderService after loading.
        """
        import copy

        from yantra_server.gateway.structured import schema_for
        from yantra_server.render.csv_binding import CSVTable
        from yantra_server.render.schemas import SCHEMA_MODELS

        base = schema_for(self.Args)
        branches = []
        models: dict[str, type[BaseModel]] = {**SCHEMA_MODELS, "csv_table": CSVTable}
        for name, model in models.items():
            branch = copy.deepcopy(base)
            branch["properties"]["schema_id"] = {"const": name}
            branch["properties"]["data_json"] = {
                "anyOf": [schema_for(model), {"type": "string"}],
                "description": "Inline structured data is preferred; alternatively a JSON file path.",
            }
            branch["additionalProperties"] = False
            branches.append(branch)
        return {"oneOf": branches}

    async def run(self, args: RenderDocumentArgs, ctx: ToolContext) -> ToolResult:
        data = self._load_data(args.data_json, ctx)
        if data is None:
            return ToolResult.fail(
                "data_json must be a path to a JSON file or valid JSON text. "
                "Use double quotes for JSON keys and strings; Python dictionaries with single "
                "quotes are invalid. Write valid JSON with write_file, then pass its path."
            )
        service = _render_service(ctx)
        provenance: dict[str, Any] = {
            "run_id": ctx.run_id,
            "task_id": ctx.task_id,
            "audit_head": (head.hash if (head := ctx.state.audit.head()) else None),
        }
        try:
            schema_id = args.schema_id
            if schema_id == "csv_table":
                from yantra_server.render.csv_binding import CSVTable, bind_csv_table

                if args.type != "xlsx":
                    raise ValueError("csv_table requires XLSX output")
                data, source = bind_csv_table(CSVTable.model_validate(data), ctx.resolve_path)
                provenance["source_document_hashes"] = [source]
                schema_id = "data_table"
            if getattr(ctx.state, "db", None) is not None and ctx.run_id and ctx.task_id:
                from sqlalchemy import select

                from yantra_server.db.models import RouterDecisionRow

                with ctx.state.db.session() as db:
                    provenance["model_ids"] = list(
                        db.scalars(
                            select(RouterDecisionRow.chosen)
                            .where(
                                RouterDecisionRow.run_id == ctx.run_id,
                                RouterDecisionRow.task_id == ctx.task_id,
                            )
                            .distinct()
                            .order_by(RouterDecisionRow.chosen)
                            .limit(100)
                        )
                    )
            out_path = ctx.resolve_path(args.out_path)
            obj = service.validate(schema_id, data)
            # Image references are reads too. Resolve them through the same
            # workspace boundary as explicit file tools, including symlinks.
            for section in getattr(obj, "sections", []):
                for figure in section.figures:
                    figure.path = self._image_path(figure.path, ctx)
            for slide in getattr(obj, "slides", []):
                if slide.figure:
                    slide.figure = self._image_path(slide.figure, ctx)
            if getattr(obj, "overlay_figure", None):
                obj.overlay_figure = self._image_path(obj.overlay_figure, ctx)
            service.render(
                doc_type=args.type,
                schema_id=schema_id,
                data=obj.model_dump(),
                out_path=out_path,
                template_id=args.template_id,
                provenance=provenance,
            )
            captured = {}
            store = getattr(ctx.state, "artifacts", None)
            if store is not None:
                # Copy each successful revision; later renders may replace the
                # workspace file but must not erase the previous evidence.
                for key, path in [
                    ("snapshot_artifact_id", out_path),
                    (
                        "snapshot_provenance_id",
                        out_path.with_suffix(out_path.suffix + ".provenance.json"),
                    ),
                ]:
                    captured[key] = store.put_file(
                        path,
                        kind="render_revision",
                        run_id=ctx.run_id,
                        task_id=ctx.task_id,
                        meta={
                            "workspace_path": args.out_path,
                            "step_id": ctx.step_id,
                            "schema_id": args.schema_id,
                            "filename": path.name,
                        },
                        copy=True,
                    )
        except Exception as exc:
            return ToolResult.fail(f"render or evidence capture failed: {exc}")
        return ToolResult(
            summary=f"rendered {args.out_path} ({args.type})",
            data={
                "path": args.out_path,
                "provenance": f"{args.out_path}.provenance.json",
                **captured,
            },
        )

    @staticmethod
    def _image_path(value: str, ctx: ToolContext) -> str:
        path = ctx.resolve_path(value)
        if not path.is_file():
            raise ValueError(f"Figure is missing: {value}")
        return str(path)

    def _load_data(
        self, data_json: dict[str, Any] | str, ctx: ToolContext
    ) -> dict[str, Any] | None:
        if isinstance(data_json, dict):
            return data_json
        stripped = data_json.strip()
        if stripped.startswith("{"):
            try:
                value = json.loads(stripped)
                return value if isinstance(value, dict) else None
            except (json.JSONDecodeError, ValueError):
                return None
        try:
            path = ctx.resolve_path(data_json)
        except Exception:
            return None
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else None
            except (json.JSONDecodeError, OSError, UnicodeError):
                return None
        return None


class ValidateDeliverableArgs(BaseModel):
    path: str
    schema_id: str


class ValidateDeliverableTool(Tool):
    name = "validate_deliverable"
    description = "Validate a deliverable JSON file against its schema before rendering."
    Args = ValidateDeliverableArgs
    side_effects = "read"

    async def run(self, args: ValidateDeliverableArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve_path(args.path)
        if not path.is_file():
            return ToolResult.fail(f"no such file: {args.path}")
        service = _render_service(ctx)
        try:
            service.validate(args.schema_id, json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            return ToolResult.fail(f"invalid: {exc}")
        return ToolResult(summary=f"{args.path} validates against {args.schema_id}")


class RenderChartArgs(BaseModel):
    spec: dict[str, Any] = Field(
        description="{type: line|bar|scatter, title, x_label, y_label, series: [{name, x, y}]}"
    )
    out_path: str


class RenderChartTool(Tool):
    name = "render_chart"
    description = "Render a chart spec (line/bar/scatter) to a PNG via matplotlib (house palette)."
    Args = RenderChartArgs
    side_effects = "render"
    idempotent = False

    async def run(self, args: RenderChartArgs, ctx: ToolContext) -> ToolResult:
        out_path = ctx.resolve_path(args.out_path)
        try:
            _render_chart(args.spec, out_path)
        except Exception as exc:
            return ToolResult.fail(f"chart render failed: {exc}")
        return ToolResult(summary=f"chart → {args.out_path}", data={"path": args.out_path})


class RenderDiagramArgs(BaseModel):
    dot: str = Field(description="Graphviz DOT source")
    out_path: str


class RenderDiagramTool(Tool):
    name = "render_diagram"
    description = (
        "Render a Graphviz DOT diagram to PNG (falls back to a text box if graphviz absent)."
    )
    Args = RenderDiagramArgs
    side_effects = "render"
    idempotent = False

    async def run(self, args: RenderDiagramArgs, ctx: ToolContext) -> ToolResult:
        import shutil

        out_path = ctx.resolve_path(args.out_path)
        dot_bin = shutil.which("dot")
        if dot_bin is None:
            return ToolResult.fail("graphviz 'dot' not installed on this host")
        import subprocess

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [dot_bin, "-Tpng", "-o", str(out_path)],
                input=args.dot.encode(),
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return ToolResult.fail(f"dot failed: {exc}")
        return ToolResult(summary=f"diagram → {args.out_path}", data={"path": args.out_path})


class SpreadsheetReadArgs(BaseModel):
    path: str
    sheet: str | None = None
    max_rows: int = Field(default=200, ge=1, le=5000)


class SpreadsheetReadTool(Tool):
    name = "spreadsheet_read"
    description = "Read rows from an XLSX sheet as text."
    Args = SpreadsheetReadArgs
    side_effects = "read"

    async def run(self, args: SpreadsheetReadArgs, ctx: ToolContext) -> ToolResult:
        import openpyxl

        path = ctx.resolve_path(args.path)
        if not path.is_file():
            return ToolResult.fail(f"no such file: {args.path}")
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        ws = wb[args.sheet] if args.sheet and args.sheet in wb.sheetnames else wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(" | ".join("" if c is None else str(c) for c in row))
            if len(rows) >= args.max_rows:
                break
        wb.close()
        return ToolResult(
            summary=f"{len(rows)} rows from {ws.title}",
            content="\n".join(rows),
            data={"rows": len(rows), "sheet": ws.title},
        )


def _render_service(ctx: ToolContext) -> Any:
    from yantra_server.render.service import RenderService

    config = ctx.state.config
    templates = config.render.templates_dir
    if not templates.is_absolute():
        templates = ctx.state.loaded.assets_dir / templates
    return RenderService(templates, config.render.libreoffice_path)


def _render_chart(spec: dict[str, Any], out_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    palette = ["#1f6feb", "#238636", "#e3b341", "#a371f7", "#db6d28"]
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    chart_type = spec.get("type", "line")
    for i, series in enumerate(spec.get("series", [])):
        color = palette[i % len(palette)]
        x = series.get("x", list(range(len(series.get("y", [])))))
        y = series.get("y", [])
        if chart_type == "bar":
            ax.bar([str(v) for v in x], y, label=series.get("name", ""), color=color)
        elif chart_type == "scatter":
            ax.scatter(x, y, label=series.get("name", ""), color=color)
        else:
            ax.plot(x, y, label=series.get("name", ""), color=color, marker="o")
    ax.set_title(spec.get("title", ""))
    ax.set_xlabel(spec.get("x_label", ""))
    ax.set_ylabel(spec.get("y_label", ""))
    if any(s.get("name") for s in spec.get("series", [])):
        ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)


def register_render_tools(registry: Any) -> None:
    for tool in (
        RenderDocumentTool(),
        ValidateDeliverableTool(),
        RenderChartTool(),
        RenderDiagramTool(),
        SpreadsheetReadTool(),
    ):
        if registry.get(tool.name) is None:
            registry.register(tool)
