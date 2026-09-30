import pytest

from yantra_server.render.column_formulas import compile_row_expression


def test_column_binding_survives_reordering_and_row_changes():
    expr = "(price + freight) * (1 + tax)"
    assert compile_row_expression(expr, ["price", "freight", "tax"], 2) == "=((A2+B2)*(1+C2))"
    assert (
        compile_row_expression(expr, ["tax", "label", "freight", "price"], 15)
        == "=((D15+C15)*(1+A15))"
    )


def test_logic_uses_named_input_instead_of_counting_letters():
    formula = compile_row_expression(
        'delivery <= 6 and material == "SiC/SiC"', ["freight", "delivery", "material"], 4
    )
    assert formula == '=AND((B4<=6),(C4="SiC/SiC"))'
    assert (
        compile_row_expression('col("Unit price") * quantity', ["Unit price", "quantity"], 2)
        == "=(A2*B2)"
    )


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('whoami')",
        "price.__class__",
        "price[0]",
        "[x for x in price]",
        "price ** 999999",
        "1e309",
        "=price+1",
        "missing+1",
        "col(price)",
        "price in [1,2]",
        "9" * 999,
    ],
)
def test_unknown_unsafe_or_unsupported_expressions_rejected(expression):
    with pytest.raises(ValueError):
        compile_row_expression(expression, ["price"], 2)


def test_no_duplicate_column_ambiguity_or_invalid_row():
    with pytest.raises(ValueError, match="unique"):
        compile_row_expression("price", ["price", "price"], 2)
    with pytest.raises(ValueError, match="valid Excel"):
        compile_row_expression("price", ["price"], 1)


def test_conditional_expressions_and_quoted_equality_are_safe():
    assert (
        compile_row_expression('IF(material="A=B", "yes", "no")', ["material"], 2)
        == '=IF((A2="A=B"),"yes","no")'
    )
    assert (
        compile_row_expression(
            "IF(AND(price <= 10, quantity > 0), price * quantity, 0)", ["price", "quantity"], 3
        )
        == "=IF(AND((A3<=10),(B3>0)),(A3*B3),0)"
    )
    assert (
        compile_row_expression("price if quantity > 0 else 0", ["price", "quantity"], 2)
        == "=IF((B2>0),A2,0)"
    )
    assert (
        compile_row_expression('IF(material=="quoted \\"value\\"",1,0)', ["material"], 2)
        == '=IF((A2="quoted ""value"""),1,0)'
    )


@pytest.mark.parametrize(
    "expression",
    [
        "IF(price>0,1)",
        "IF(price>0,1,0,9)",
        'IF(price>0,1,__import__("os"))',
        "AND()",
        "NOT(price, 0)",
        'WEBSERVICE("https://example.com")',
    ],
)
def test_conditional_support_does_not_allow_arbitrary_calls(expression):
    with pytest.raises(ValueError):
        compile_row_expression(expression, ["price"], 2)


def test_computed_columns_render_with_stable_binding_and_inspectable_rules(tmp_path):
    import openpyxl

    from yantra_server.render.service import RenderService

    path = tmp_path / "computed.xlsx"
    RenderService(tmp_path).render(
        doc_type="xlsx",
        schema_id="data_table",
        out_path=path,
        data={
            "sheets": [
                {
                    "columns": ["quantity", "Unit price"],
                    "rows": [[4, 25], [3, 10]],
                    "computed_columns": [
                        {"name": "Total", "expression": 'col("Unit price") * quantity'},
                        {"name": "Eligible", "expression": "Total <= 100"},
                    ],
                }
            ]
        },
    )
    book = openpyxl.load_workbook(path)
    sheet = book.active
    assert sheet["C2"].value == "=(B2*A2)"
    assert sheet["C3"].value == "=(B3*A3)"
    assert sheet["D2"].value == "=(C2<=100)"
    assert 'col("Unit price") * quantity' in sheet["C1"].comment.text
    assert sheet["A2"].data_type == "n"
    book.close()


@pytest.mark.parametrize(
    "computed",
    [
        [{"name": "price", "expression": "price + 1"}],
        [{"name": "Total", "expression": "Missing + 1"}],
        [{"name": "Total", "expression": "Total + 1"}],
        [{"name": "First", "expression": "Later + 1"}, {"name": "Later", "expression": "price"}],
        [{"name": "Total", "expression": "price"}] * 129,
    ],
)
def test_ambiguous_forward_and_excess_computed_columns_rejected(computed, tmp_path):
    from yantra_server.render.service import RenderError, RenderService

    with pytest.raises(RenderError):
        RenderService(tmp_path).validate(
            "data_table",
            {
                "sheets": [
                    {
                        "columns": ["price"],
                        "rows": [[25]],
                        "computed_columns": computed,
                    }
                ]
            },
        )
