"""Bounded arithmetic interpreter: no eval, imports, attributes, I/O or executable code."""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable

from pydantic import BaseModel, Field

from yantra_server.tools.base import Tool, ToolContext, ToolResult

OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "abs": abs,
    "round": lambda value, digits=0: round(value, int(digits)),
}


def calculate(expression: str, variables: dict[str, float]) -> float:
    if len(expression) > 1000 or len(variables) > 30:
        raise ValueError("Calculation exceeds limits")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 120:
        raise ValueError("Expression is too complex")

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            value = walk(node.body)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ):
            value = float(node.value)
        elif isinstance(node, ast.Name) and node.id in {"pi": math.pi, "e": math.e, **variables}:
            value = {"pi": math.pi, "e": math.e, **variables}[node.id]
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = -walk(node.operand) if isinstance(node.op, ast.USub) else walk(node.operand)
        elif isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent exceeds 100")
            value = OPS[type(node.op)](left, right)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FUNCTIONS
            and not node.keywords
            and len(node.args) <= 2
        ):
            value = FUNCTIONS[node.func.id](*[walk(arg) for arg in node.args])
        else:
            raise ValueError(
                "Only arithmetic, named variables and approved math functions are allowed"
            )
        if isinstance(value, complex) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Non-finite or excessive result")
        return float(value)

    return float(walk(tree))


class CalculateArgs(BaseModel):
    expression: str = Field(max_length=1000)
    variables: dict[str, float] = Field(default_factory=dict)
    units: str = Field(
        default="", max_length=80, description="Output units; convert inputs consistently first"
    )


class CalculateTool(Tool):
    name = "calculate"
    description = "Compute arithmetic precisely. Supports + - * / ** sqrt log sin cos pi. No Python execution."
    Args = CalculateArgs

    async def run(self, args: CalculateArgs, ctx: ToolContext) -> ToolResult:
        try:
            value = calculate(args.expression, args.variables)
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            return ToolResult.fail(str(exc))
        content = f"{args.expression} = {value:.12g} {args.units}\nInputs: {args.variables}\nUnits are user-specified; dimensional correctness requires review."
        return ToolResult(
            summary=f"{value:.12g} {args.units}",
            content=content,
            data={
                "value": value,
                "expression": args.expression,
                "variables": args.variables,
                "units": args.units,
            },
        )
