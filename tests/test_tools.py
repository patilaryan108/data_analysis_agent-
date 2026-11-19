"""Run from the project root:  pytest -v
Uses a throw-away SQLite copy of data/clean_data.csv, so MySQL is not touched."""
import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
CSV = ROOT / "data" / "clean_data.csv"


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    if not CSV.exists():
        pytest.skip("run src/clean_data.py first to create data/clean_data.csv")
    import os
    db = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db}"
    import tools as t
    importlib.reload(t)
    t._engines.clear()
    pd.read_csv(CSV).to_sql("blinkit_sales", t.get_engine(), index=False)
    t.CHART_DIR = tmp_path_factory.mktemp("charts")
    return t


# ---- run_sql safety -------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "DROP TABLE blinkit_sales",
    "DELETE FROM blinkit_sales",
    "UPDATE blinkit_sales SET sales = 0",
    "SELECT 1; DROP TABLE blinkit_sales",
    "/* hi */ DROP TABLE blinkit_sales",
    "SELECT * FROM blinkit_sales INTO OUTFILE '/tmp/x'",
    "INSERT INTO blinkit_sales VALUES (1)",
    "SHOW TABLES",
])
def test_blocks_non_select(tools, bad):
    with pytest.raises(ValueError):
        tools.run_sql(bad)


def test_allows_select_with_scary_word_in_string(tools):
    r = tools.run_sql("SELECT COUNT(*) FROM blinkit_sales WHERE item_type = 'delete me'")
    assert r["rows"][0][0] == 0


def test_run_sql_matches_known_numbers(tools):
    r = tools.run_sql("SELECT ROUND(SUM(sales)), COUNT(DISTINCT item_identifier), COUNT(*) FROM blinkit_sales")
    total, items, rows = r["rows"][0]
    assert round(total) == 1201681 and items == 1559 and rows == 8523


def test_truncates_large_results(tools):
    r = tools.run_sql("SELECT * FROM blinkit_sales", max_rows=5)
    assert r["row_count"] == 5 and r["truncated"]


# ---- the 5 project questions ------------------------------------------------ #
def test_top_outlet_type(tools):
    r = tools.run_sql("SELECT outlet_type, SUM(sales) s FROM blinkit_sales GROUP BY outlet_type ORDER BY s DESC")
    assert r["rows"][0][0] == "Supermarket Type1"


def test_top_item_type(tools):
    r = tools.run_sql("SELECT item_type, SUM(sales) s FROM blinkit_sales GROUP BY item_type ORDER BY s DESC")
    assert r["rows"][0][0] == "Fruits and Vegetables"


# ---- profile / suggest / chart --------------------------------------------- #
def test_profile(tools):
    p = tools.profile_data()
    assert p["rows"] == 8523
    assert any(c["name"] == "sales" and c["role"] == "numeric" for c in p["columns"])
    assert any(c["name"] == "item_identifier" and c["role"] == "id" for c in p["columns"])


def test_suggestions_are_valid_inputs_for_make_chart(tools):
    s = tools.suggest_visuals()
    assert s["measure"] == "sales" and s["suggestions"]
    first_bar = next(x for x in s["suggestions"] if x["visual"] == "bar")
    out = tools.make_chart(first_bar["visual"], first_bar["x"], first_bar["y"], first_bar["agg"])
    assert Path(out["chart_file"]).exists()


def test_suggestions_cover_project_questions(tools):
    s = tools.suggest_visuals()["suggestions"]
    bars = {i.get("x") for i in s if i["visual"] == "bar" and i["y"] == "sales"}
    assert {"outlet_type", "item_type", "outlet_location_type", "item_fat_content"} <= bars
    assert any(i["y"] == "rating" for i in s)


def test_make_chart_rejects_unknown_column(tools):
    with pytest.raises(ValueError):
        tools.make_chart("bar", "outlet_type; DROP TABLE x", "sales")


@pytest.mark.parametrize("kind", ["bar", "column", "line", "pie", "scatter"])
def test_all_chart_types(tools, kind):
    y = "item_weight" if kind == "scatter" else "sales"
    x = "sales" if kind == "scatter" else "outlet_type"
    out = tools.make_chart(kind, x, y, "sum")
    assert Path(out["chart_file"]).exists()


# ---- clean_data -------------------------------------------------------------- #
def test_clean_data_fixes_messy_table(tools):
    messy = pd.DataFrame({
        "fat": ["Low Fat", "low fat", "LF", "Regular", "reg", None, "Low Fat"],
        "price": [1.0, 2.0, None, 4.0, 5.0, 6.0, 1.0],
    })
    messy.to_sql("messy", tools.get_engine(readonly=False), index=False, if_exists="replace")
    rep = tools.clean_data("messy", value_map={"fat": {"LF": "Low Fat", "reg": "Regular"}})
    out = pd.read_sql("SELECT * FROM messy_clean", tools.get_engine())
    assert set(out["fat"]) == {"Low Fat", "Regular", "Unknown"}
    assert out["price"].isna().sum() == 0
    assert rep["duplicates_removed"] == 1