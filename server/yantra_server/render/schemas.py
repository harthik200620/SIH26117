"""Deliverable schemas (SPEC §12): Pydantic models → JSON Schema in templates/schemas/."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Citation(BaseModel):
    marker: str = ""  # e.g. c:<chunk_id> or artifact_id:locator
    source: str = ""  # resolved "Title, Rev, p.N"


class TableBase[CellT](BaseModel):
    caption: str = ""
    columns: list[str] = Field(min_length=1, max_length=128)
    rows: list[list[CellT]]

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if len(self.rows) > 10000:
            raise ValueError("A table is limited to 10000 rows; split larger datasets")
        for number, row in enumerate(self.rows, 1):
            if len(row) != len(self.columns):
                raise ValueError(f"Row {number} has {len(row)} cells; expected {len(self.columns)}")
        return self


class TableSpec(TableBase[str]):
    pass


class FigureSpec(BaseModel):
    caption: str = ""
    path: str  # workspace-relative image path


class Section(BaseModel):
    heading: str
    paragraphs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)
    figures: list[FigureSpec] = Field(default_factory=list)


class Finding(BaseModel):
    id: str = ""
    severity: Literal["high", "medium", "low", "info"] = "info"
    statement: str
    evidence: str = ""


class Recommendation(BaseModel):
    id: str = ""
    text: str
    priority: Literal["high", "medium", "low"] = "medium"


class Appendix(BaseModel):
    citations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)


class Report(BaseModel):
    title: str
    metadata: dict[str, str] = Field(default_factory=dict)
    executive_summary: str = ""
    sections: list[Section] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    appendix: Appendix = Field(default_factory=Appendix)


class EquipmentList(BaseModel):
    title: str = "Equipment List"
    equipment: list[dict[str, str]] = Field(default_factory=list)
    instruments: list[dict[str, str]] = Field(default_factory=list)
    lines: list[dict[str, str]] = Field(default_factory=list)
    loops: list[dict[str, str]] = Field(default_factory=list)
    findings: list[dict[str, str]] = Field(default_factory=list)


class WorkOrderDraft(BaseModel):
    title: str
    equipment: str
    priority: Literal["emergency", "high", "medium", "low"] = "medium"
    description: str
    steps: list[str] = Field(default_factory=list)
    parts: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)


class EmailDraft(BaseModel):
    to: str = ""
    subject: str
    body: str


class ComputedColumn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expression: str = Field(min_length=1, max_length=1000)


class SpreadsheetTable(TableBase[str | int | float | bool | None]):
    model_config = ConfigDict(allow_inf_nan=False)
    literal_inputs: bool = False
    computed_columns: list[ComputedColumn] = Field(default_factory=list, max_length=127)

    @model_validator(mode="after")
    def validate_computed_columns(self) -> Self:
        if len(self.columns) + len(self.computed_columns) > 128:
            raise ValueError("Input and computed columns together must not exceed 128")
        if not self.computed_columns:
            return self
        from .column_formulas import compile_row_expression

        available = list(self.columns)
        for computed in self.computed_columns:
            if not computed.name.strip():
                raise ValueError("Computed column names must be nonblank")
            if computed.name in available:
                raise ValueError(
                    f"Computed column {computed.name!r} is already declared. Input columns and rows "
                    "must omit calculated outputs; computed_columns appends them automatically. "
                    "Do not supply a precomputed value for the same output."
                )
            try:
                compile_row_expression(computed.expression, available, 2)
            except ValueError as exc:
                raise ValueError(
                    f"Computed column {computed.name!r}: {exc}. Expression: {computed.expression!r}"
                ) from exc
            available.append(computed.name)
        return self


class DataTable(BaseModel):
    title: str = "Data"
    sheets: list[SpreadsheetTable] = Field(min_length=1, max_length=64)
    summary: str = ""
    appendix: Appendix = Field(default_factory=Appendix)


class CodeChangeSummary(BaseModel):
    title: str = "Code change summary"
    files: list[str] = Field(default_factory=list)
    rationale: str = ""
    tests: str = ""
    risks: list[str] = Field(default_factory=list)


class Slide(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list)
    notes: str = ""
    figure: str | None = None


class Presentation(BaseModel):
    title: str
    slides: list[Slide] = Field(default_factory=list)


class PIDReview(BaseModel):
    title: str
    drawing_number: str = ""
    revision: str = ""
    equipment_count: int = 0
    instrument_count: int = 0
    coverage: float = 1.0
    findings: list[Finding] = Field(default_factory=list)
    overlay_figure: str | None = None
    appendix: Appendix = Field(default_factory=Appendix)


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "report": Report,
    "equipment_list": EquipmentList,
    "instrument_index": EquipmentList,
    "line_list": EquipmentList,
    "inspection_summary": Report,
    "work_order_draft": WorkOrderDraft,
    "email_draft": EmailDraft,
    "meeting_minutes": Report,
    "presentation": Presentation,
    "data_table": DataTable,
    "code_change_summary": CodeChangeSummary,
    "pid_review": PIDReview,
}
