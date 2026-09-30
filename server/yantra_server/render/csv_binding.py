"""Load bounded CSV inputs without letting a model rewrite their values."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Appendix, ComputedColumn

MAX_SOURCE_BYTES = 2_000_000
MAX_ROWS = 10_000
MAX_CELLS = 100_000


class CSVTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = "Source comparison"
    source_path: str = Field(min_length=1, max_length=1000)
    numeric_columns: list[str] = Field(default_factory=list, max_length=128)
    computed_columns: list[ComputedColumn] = Field(default_factory=list, max_length=127)
    summary: str = ""
    appendix: Appendix = Field(default_factory=Appendix)


def bind_csv_table(
    spec: CSVTable, resolve_path: Callable[[str], Path]
) -> tuple[dict[str, Any], dict[str, str]]:
    path = resolve_path(spec.source_path)
    if path.suffix.lower() != ".csv" or not path.is_file():
        raise ValueError("source_path must name an existing workspace CSV file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("CSV source exceeds the 2 MB limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV source must be UTF-8; convert its encoding explicitly first") from exc
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    headers = next(reader, [])
    if not 1 <= len(headers) <= 128 or len(headers) != len(set(headers)):
        raise ValueError("CSV requires 1-128 unique column names")
    if any(not h.strip() or len(h) > 128 for h in headers):
        raise ValueError("CSV column names must be nonblank and at most 128 characters")
    if len(headers) + len(spec.computed_columns) > 128:
        raise ValueError("Input and computed columns together must not exceed 128")
    numeric = set(spec.numeric_columns)
    if len(numeric) != len(spec.numeric_columns) or not numeric.issubset(headers):
        raise ValueError(f"numeric_columns must be unique source column names: {headers}")
    rows: list[list[str | int | float]] = []
    for record, values in enumerate(reader, 2):
        if (
            len(rows) >= MAX_ROWS
            or (len(rows) + 1) * (len(headers) + len(spec.computed_columns)) > MAX_CELLS
        ):
            raise ValueError("CSV exceeds 10000 rows or 100000 input cells")
        if len(values) != len(headers):
            raise ValueError(
                f"CSV record {record} has {len(values)} cells; expected {len(headers)}"
            )
        row: list[str | int | float] = []
        for name, value in zip(headers, values, strict=True):
            if len(value) > 32767 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
                raise ValueError(f"CSV record {record}, {name}: invalid Excel text")
            if name not in numeric:
                row.append(value)
                continue
            # Explicit typing keeps identifiers such as 001 intact. Locale-specific
            # separators, units and missing values require an explicit upstream fix.
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip()):
                raise ValueError(f"CSV record {record}, {name}: expected a plain finite number")
            try:
                number = Decimal(value.strip())
                if not number.is_finite() or abs(number) > Decimal("1e100"):
                    raise ValueError("non-finite or oversized value")
                # normalize() uses the active Decimal context and can round long
                # coefficients before this check. Inspect the original digits.
                digits = number.as_tuple().digits
                significant = len(digits)
                while significant > 1 and digits[significant - 1] == 0:
                    significant -= 1
                if significant > 15:
                    raise ValueError("more than Excel's 15 significant digits")
                if number and abs(number) < Decimal("1e-100"):
                    raise ValueError("value is too small for this calculation profile")
                row.append(int(number) if number == number.to_integral_value() else float(number))
            except (InvalidOperation, ValueError, OverflowError) as exc:
                raise ValueError(f"CSV record {record}, {name}: {exc}") from exc
        rows.append(row)
    if not rows:
        raise ValueError("CSV source must contain at least one data record")
    from .expression_types import expression_types

    types = {name: frozenset({"number" if name in numeric else "text"}) for name in headers}
    for computed in spec.computed_columns:
        if computed.name in types:
            raise ValueError(f"Computed column {computed.name!r} is already declared")
        try:
            types[computed.name] = expression_types(computed.expression, types)
        except ValueError as exc:
            raise ValueError(f"Computed column {computed.name!r}: {exc}") from exc
    evidence = {"path": spec.source_path, "sha256": hashlib.sha256(raw).hexdigest()}
    appendix = spec.appendix.model_dump()
    appendix["citations"] = list(
        dict.fromkeys(
            [
                *appendix["citations"],
                f"{spec.source_path}, CSV records 2-{len(rows) + 1} (header is record 1); SHA-256 {evidence['sha256']}",
            ]
        )
    )
    return {
        "title": spec.title,
        "sheets": [
            {
                "caption": "Source data and calculations",
                "columns": headers,
                "rows": rows,
                "literal_inputs": True,
                "computed_columns": [c.model_dump() for c in spec.computed_columns],
            }
        ],
        "summary": spec.summary,
        "appendix": appendix,
    }, evidence
