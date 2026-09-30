"""Reject implicit text/number coercion in CSV-backed spreadsheet calculations."""

from __future__ import annotations

import ast
from itertools import pairwise

from .column_formulas import compile_row_expression
from .expression_syntax import comparison_syntax


def expression_types(expression: str, columns: dict[str, frozenset[str]]) -> frozenset[str]:
    # Reuse the compiler's syntax/size/name allowlist before walking the tree.
    compile_row_expression(expression, list(columns), 2)
    tree = ast.parse(comparison_syntax(expression), mode="eval")

    def require(actual: frozenset[str], allowed: set[str], node: ast.AST) -> None:
        if not actual.issubset(allowed):
            raise ValueError(
                f"Type mismatch at {ast.unparse(node)!r}: {sorted(actual)} cannot be used "
                f"as {sorted(allowed)}. Declare numeric source columns in numeric_columns; "
                "keep identifiers as text and do not use quoted text as numbers."
            )

    def visit(node: ast.AST) -> frozenset[str]:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return columns[node.id]
        if isinstance(node, ast.Constant):
            return frozenset(
                {
                    "boolean"
                    if isinstance(node.value, bool)
                    else "text"
                    if isinstance(node.value, str)
                    else "number"
                }
            )
        if isinstance(node, ast.BinOp):
            for part in (node.left, node.right):
                require(visit(part), {"number"}, part)
            return frozenset({"number"})
        if isinstance(node, ast.UnaryOp):
            boolean = isinstance(node.op, ast.Not)
            require(
                visit(node.operand), {"number", "boolean"} if boolean else {"number"}, node.operand
            )
            return frozenset({"boolean" if boolean else "number"})
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            for left, right in pairwise(parts):
                left_types, right_types = visit(left), visit(right)
                if left_types != right_types or len(left_types) != 1:
                    raise ValueError(
                        f"Mixed comparison types at {ast.unparse(node)!r}. "
                        "Declare numeric source columns in numeric_columns; compare like types."
                    )
            return frozenset({"boolean"})
        if isinstance(node, ast.BoolOp):
            for part in node.values:
                require(visit(part), {"boolean", "number"}, part)
            return frozenset({"boolean"})
        if isinstance(node, ast.IfExp):
            require(visit(node.test), {"boolean", "number"}, node.test)
            return visit(node.body) | visit(node.orelse)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "col":
                name = node.args[0]
                assert isinstance(name, ast.Constant) and isinstance(name.value, str)
                return columns[name.value]
            if node.func.id.upper() == "IF":
                require(visit(node.args[0]), {"boolean", "number"}, node.args[0])
                return visit(node.args[1]) | visit(node.args[2])
            for part in node.args:
                require(visit(part), {"boolean", "number"}, part)
            return frozenset({"boolean"})
        raise ValueError("Unsupported expression type")

    return visit(tree)
