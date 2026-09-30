"""Normalize a small spreadsheet expression dialect without evaluating any code."""

from __future__ import annotations

import io
import tokenize


def comparison_syntax(expression: str) -> str:
    """Allow Excel '=' comparisons while keeping quoted text and operators intact."""
    if expression.lstrip().startswith("="):
        raise ValueError("Computed expressions must omit the leading '='")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(expression).readline))
    except (tokenize.TokenError, IndentationError) as exc:
        raise ValueError("Unbalanced or invalid computed expression") from exc
    # Tokenization keeps '=' in quoted strings untouched. No evaluation or access
    # to Python names occurs here; the compiler still checks every AST node.
    return tokenize.untokenize(
        (token.type, "==" if token.type == tokenize.OP and token.string == "=" else token.string)
        for token in tokens
    )
