"""Compile a small row-expression language to Excel, without evaluating Python.

Column names are bound by the renderer, so the model need not count Excel letters.
Only numeric arithmetic, comparisons, Boolean logic and literal column lookup exist.
"""

from __future__ import annotations

import ast
import math

from openpyxl.utils import get_column_letter

from .expression_syntax import comparison_syntax


def compile_row_expression(expression: str, columns: list[str], row: int) -> str:
    if row < 2 or row > 1_048_576:
        raise ValueError("Computed row must be a valid Excel data row")
    if not expression.strip() or len(expression) > 1000:
        raise ValueError("Computed expression must contain 1-1000 characters")
    if len(set(columns)) != len(columns):
        raise ValueError("Computed columns require unique, case-sensitive input column names")
    bindings = {name: f"{get_column_letter(i)}{row}" for i, name in enumerate(columns, 1)}
    try:
        tree = ast.parse(comparison_syntax(expression), mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError(
            "Invalid row expression. Use price * quantity or IF(quantity >= 1, 1, 0); omit a leading '='"
        ) from exc
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("Computed expression exceeds 100 syntax nodes")

    def column(name: str) -> str:
        if name not in bindings:
            raise ValueError(f"Unknown input column {name!r}; available: {', '.join(columns)}")
        return bindings[name]

    def visit(node: ast.AST) -> str:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return column(node.id)
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                return "TRUE" if value else "FALSE"
            if isinstance(value, (float, int)):
                if abs(value) > 1e100 or not math.isfinite(value):
                    raise ValueError("Expression constant is non-finite or too large")
                return str(value)
            if isinstance(value, str):
                if len(value) > 256:
                    raise ValueError("Expression text constant is too long")
                return '"' + value.replace('"', '""') + '"'
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "col"
            and not node.keywords
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return column(node.args[0].value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            function = node.func.id.upper()
            allowed = (
                (function == "IF" and len(node.args) == 3)
                or (function == "NOT" and len(node.args) == 1)
                or (function in {"AND", "OR"} and 1 <= len(node.args) <= 32)
            )
            if allowed:
                return f"{function}({','.join(visit(arg) for arg in node.args)})"
        if isinstance(node, ast.IfExp):
            return f"IF({visit(node.test)},{visit(node.body)},{visit(node.orelse)})"
        if isinstance(node, ast.BinOp):
            operator = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}.get(type(node.op))
            if operator:
                return f"({visit(node.left)}{operator}{visit(node.right)})"
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return f"NOT({visit(node.operand)})"
            operator = {ast.UAdd: "+", ast.USub: "-"}.get(type(node.op))
            if operator:
                return f"({operator}{visit(node.operand)})"
        if isinstance(node, ast.BoolOp):
            function = "AND" if isinstance(node.op, ast.And) else "OR"
            return f"{function}({','.join(visit(v) for v in node.values)})"
        if isinstance(node, ast.Compare):
            parts = []
            left = node.left
            for comparison, right in zip(node.ops, node.comparators, strict=True):
                symbol = {
                    ast.Eq: "=",
                    ast.NotEq: "<>",
                    ast.Lt: "<",
                    ast.LtE: "<=",
                    ast.Gt: ">",
                    ast.GtE: ">=",
                }.get(type(comparison))
                if not symbol:
                    raise ValueError("Unsupported comparison in computed expression")
                parts.append(f"({visit(left)}{symbol}{visit(right)})")
                left = right
            return parts[0] if len(parts) == 1 else f"AND({','.join(parts)})"
        raise ValueError(f"Unsupported expression syntax: {type(node).__name__}")

    return "=" + visit(tree)
