import pytest

from yantra_server.render.csv_binding import CSVTable, bind_csv_table


@pytest.mark.parametrize(
    "expression",
    ["price * 2", "price <= 6", "price == 6", "IF(price <= 6, 1, 0)", "col('price') + 1"],
)
def test_text_source_used_as_number_requires_explicit_numeric_column(tmp_path, expression):
    (tmp_path / "source.csv").write_text("id,price\n001,4\n002,8\n")
    spec = CSVTable(
        source_path="source.csv", computed_columns=[{"name": "Result", "expression": expression}]
    )
    with pytest.raises(ValueError, match="numeric_columns"):
        bind_csv_table(spec, lambda p: tmp_path / p)
    spec.numeric_columns = ["price"]
    payload, _ = bind_csv_table(spec, lambda p: tmp_path / p)
    assert payload["sheets"][0]["rows"][0] == ["001", 4]


def test_text_comparison_and_conditional_outputs_retain_string_semantics(tmp_path):
    (tmp_path / "source.csv").write_text("id,price\n001,4\n002,8\n")
    spec = CSVTable(
        source_path="source.csv",
        numeric_columns=["price"],
        computed_columns=[
            {"name": "Status", "expression": "IF(price <= 6, 'yes', 'no')"},
            {"name": "Selected", "expression": "Status == 'yes' and id == '001'"},
        ],
    )
    payload, _ = bind_csv_table(spec, lambda p: tmp_path / p)
    assert payload["sheets"][0]["rows"][0][0] == "001"


def test_computed_string_cannot_be_used_as_arithmetic_input(tmp_path):
    (tmp_path / "source.csv").write_text("id,price\n001,4\n")
    spec = CSVTable(
        source_path="source.csv",
        numeric_columns=["price"],
        computed_columns=[
            {"name": "Status", "expression": "IF(price <= 6, 'yes', 'no')"},
            {"name": "Invalid", "expression": "Status * 2"},
        ],
    )
    with pytest.raises(ValueError, match="numeric_columns"):
        bind_csv_table(spec, lambda p: tmp_path / p)
