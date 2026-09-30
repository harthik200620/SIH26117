import pytest

from yantra_server.render.expression_syntax import comparison_syntax


def test_only_operator_equality_is_normalized():
    import ast

    expression = 'IF(material="A=B", "x=y", "n")'
    tree = ast.parse(comparison_syntax(expression), mode="eval")
    assert isinstance(tree.body.args[0].ops[0], ast.Eq)
    assert tree.body.args[0].comparators[0].value == "A=B"
    assert tree.body.args[1].value == "x=y"


@pytest.mark.parametrize("expression", ["=price*quantity", "IF(x=1", "price + (2"])
def test_incomplete_or_prefixed_expressions_fail(expression):
    with pytest.raises(ValueError):
        comparison_syntax(expression)
