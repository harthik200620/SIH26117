"""Small, explicit SI quantity algebra for engineering calculations; no executable code."""

from __future__ import annotations

import ast
import math
from typing import Literal

from pydantic import BaseModel, Field

from yantra_server.tools.base import Tool, ToolContext, ToolResult

# Exponents are mass, length, time. Temperature requires affine conversions and is
# deliberately excluded. Unsupported units fail rather than being guessed.
Dimension = tuple[int, int, int]
Unit = Literal[
    "1",
    "%",
    "kg",
    "g",
    "m",
    "mm",
    "s",
    "min",
    "h",
    "m/s",
    "m/s^2",
    "m^3/s",
    "m^3/h",
    "L/s",
    "L/min",
    "kg/m^3",
    "N",
    "Pa",
    "kPa",
    "bar",
    "J",
    "kJ",
    "W",
    "kW",
]
UNITS: dict[str, tuple[float, Dimension]] = {
    "1": (1, (0, 0, 0)),
    "%": (0.01, (0, 0, 0)),
    "kg": (1, (1, 0, 0)),
    "g": (0.001, (1, 0, 0)),
    "m": (1, (0, 1, 0)),
    "mm": (0.001, (0, 1, 0)),
    "s": (1, (0, 0, 1)),
    "min": (60, (0, 0, 1)),
    "h": (3600, (0, 0, 1)),
    "m/s": (1, (0, 1, -1)),
    "m/s^2": (1, (0, 1, -2)),
    "m^3/s": (1, (0, 3, -1)),
    "m^3/h": (1 / 3600, (0, 3, -1)),
    "L/s": (0.001, (0, 3, -1)),
    "L/min": (0.001 / 60, (0, 3, -1)),
    "kg/m^3": (1, (1, -3, 0)),
    "N": (1, (1, 1, -2)),
    "Pa": (1, (1, -1, -2)),
    "kPa": (1000, (1, -1, -2)),
    "bar": (100000, (1, -1, -2)),
    "J": (1, (1, 2, -2)),
    "kJ": (1000, (1, 2, -2)),
    "W": (1, (1, 2, -3)),
    "kW": (1000, (1, 2, -3)),
}


class Quantity(BaseModel):
    value: float = Field(allow_inf_nan=False)
    unit: Unit


def evaluate_quantity(expression: str, variables: dict[str, Quantity], output_unit: str) -> float:
    if len(expression) > 1000 or len(variables) > 30:
        raise ValueError("Calculation exceeds limits")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 120:
        raise ValueError("Expression is too complex")

    def walk(node: ast.AST) -> tuple[float, Dimension]:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ):
            value, dimension = float(node.value), (0, 0, 0)
        elif isinstance(node, ast.Name) and node.id in variables:
            quantity = variables[node.id]
            factor, dimension = UNITS[quantity.unit]
            value = quantity.value * factor
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value, dimension = walk(node.operand)
            value *= -1 if isinstance(node.op, ast.USub) else 1
        elif isinstance(node, ast.BinOp):
            left, ld = walk(node.left)
            right, rd = walk(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if ld != rd:
                    raise ValueError("Cannot add or subtract quantities with different dimensions")
                value, dimension = (
                    left + right if isinstance(node.op, ast.Add) else left - right,
                    ld,
                )
            elif isinstance(node.op, (ast.Mult, ast.Div)):
                sign = 1 if isinstance(node.op, ast.Mult) else -1
                dimension = (ld[0] + sign * rd[0], ld[1] + sign * rd[1], ld[2] + sign * rd[2])
                value = left * right if sign == 1 else left / right
            elif (
                isinstance(node.op, ast.Pow)
                and rd == (0, 0, 0)
                and right.is_integer()
                and abs(right) <= 10
            ):
                dimension = (ld[0] * int(right), ld[1] * int(right), ld[2] * int(right))
                value = left**right
            else:
                raise ValueError("Only +, -, *, / and small integer powers are supported")
        else:
            raise ValueError("Only arithmetic and declared quantities are supported")
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Non-finite or excessive result")
        return value, dimension

    value, dimension = walk(tree)
    factor, expected = UNITS[output_unit]
    if dimension != expected:
        raise ValueError(
            f"Expression dimensions {dimension} do not match output unit {output_unit}"
        )
    return value / factor


class QuantityArgs(BaseModel):
    expression: str = Field(max_length=1000)
    variables: dict[str, Quantity]
    output_unit: Unit


class CalculateQuantityTool(Tool):
    name = "calculate_quantity"
    description = "Calculate physical quantities using ORIGINAL source values and units. Automatically converts units and rejects dimensional errors. Example power expression: density*gravity*flow*head, with flow in m^3/h and output_unit kW. Never manually convert values before supplying source units."
    Args = QuantityArgs

    async def run(self, args: QuantityArgs, ctx: ToolContext) -> ToolResult:
        try:
            value = evaluate_quantity(args.expression, args.variables, args.output_unit)
        except (ValueError, KeyError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            return ToolResult.fail(str(exc))
        inputs = {key: item.model_dump() for key, item in args.variables.items()}
        return ToolResult(
            summary=f"{value:.12g} {args.output_unit}",
            content=f"{args.expression} = {value:.12g} {args.output_unit}\nSource quantities: {inputs}\nDimensions checked; source accuracy and formula applicability still require review.",
            data={
                "value": value,
                "unit": args.output_unit,
                "expression": args.expression,
                "inputs": inputs,
                "dimensions_checked": True,
            },
        )
