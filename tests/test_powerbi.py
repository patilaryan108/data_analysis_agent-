"""Tests for Step 6. Run from the project root:  pytest -v   (uses a throw-away SQLite copy, not MySQL)"""
import importlib
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
CSV = ROOT / "data" / "clean_data.csv"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not CSV.exists():
        pytest.skip("run src/clean_data.py first")
    db = tmp_path_factory.mktemp("pbi") / "pbi.db"
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "GROQ_API_KEY", "GROQ_MODEL"):
        os.environ.pop(k, None)
    os.environ.update(DATABASE_URL=f"sqlite:///{db}", PBI_TABLE="BlinkIT Grocery Data", LLM_MODEL="fake")
    import tools, powerbi, agent
    importlib.reload(tools); importlib.reload(powerbi); importlib.reload(agent)
    tools._engines.clear()
    pd.read_csv(CSV).to_sql("blinkit_sales", tools.get_engine(), index=False)
    powerbi.PBI_DIR = tmp_path_factory.mktemp("powerbi_out")
    return tools, powerbi, agent


# ---- create_powerbi_view -------------------------------------------------------- #
def test_view_has_title_case_columns_and_all_rows(env):
    tools, pbi, _ = env
    out = pbi.create_powerbi_view()
    assert out["view"] == "pbi_blinkit_sales" and out["rows"] == 8523
    assert {"Item Fat Content", "Outlet Type", "Sales", "Item Category", "Outlet Age"} <= set(out["columns"])
    assert '[Query="SELECT * FROM pbi_blinkit_sales"]' in out["power_query_m"]
    assert out["sql_for_power_bi"] == "SELECT * FROM pbi_blinkit_sales" and "Get data" in out["next"]


def test_view_totals_match_the_table(env):
    tools, pbi, _ = env
    pbi.create_powerbi_view()
    eng = tools.get_engine()
    a = pd.read_sql(text("SELECT ROUND(SUM(sales)) s FROM blinkit_sales"), eng)["s"][0]
    b = pd.read_sql(text('SELECT ROUND(SUM("Sales")) s FROM pbi_blinkit_sales'), eng)["s"][0]
    assert a == b == 1201681


def test_view_name_is_validated(env):
    _, pbi, _ = env
    with pytest.raises(ValueError):
        pbi.create_powerbi_view(view_name="x; DROP TABLE blinkit_sales")


def test_view_can_be_refreshed_twice(env):
    _, pbi, _ = env
    pbi.create_powerbi_view(); assert pbi.create_powerbi_view()["rows"] == 8523


# ---- powerbi_plan ------------------------------------------------------------- #
def test_plan_measures(env):
    _, pbi, _ = env
    plan = pbi.powerbi_plan()
    dax = {m["name"]: m["dax"] for m in plan["measures"]}
    assert dax["Total Sales"] == "Total Sales = SUM('BlinkIT Grocery Data'[Sales])"
    assert dax["Item Count"] == "Item Count = DISTINCTCOUNT('BlinkIT Grocery Data'[Item Identifier])"
    assert "DISTINCTCOUNT('BlinkIT Grocery Data'[Outlet Identifier])" in dax["Outlet Count"]
    assert dax["Average Sales per Outlet"].endswith("DIVIDE([Total Sales], [Outlet Count])")
    assert "ALLSELECTED" in dax["Sales Share %"]
    assert "Average Rating" in dax
    assert "Item Count per Item" not in " ".join(dax)          # 1,559 items: no per-item measure


def test_plan_layout(env):
    _, pbi, _ = env
    plan = pbi.powerbi_plan()
    assert plan["cards"][:2] == ["Total Sales", "Average Sales"] and len(plan["cards"]) <= 5
    assert {"Outlet Type", "Outlet Location Type", "Item Fat Content"} <= set(plan["slicers"])
    assert "Outlet Identifier" not in plan["slicers"]
    visuals = [c["visual"] for c in plan["charts"]]
    assert 0 < len(plan["charts"]) <= 8 and visuals.count("Donut chart") <= 3
    first = plan["charts"][0]
    assert "[Average Sales per Outlet]" in first["fields"] and "Outlet Type" in first["fields"]
    assert any("Item Type" in c["fields"] for c in plan["charts"])
    assert all("|" not in c["fields"] for c in plan["charts"])      # would break the markdown table


def test_plan_writes_files(env):
    _, pbi, _ = env
    plan = pbi.powerbi_plan()
    md, dax = (Path(f) for f in plan["files"])
    assert md.exists() and dax.exists()
    assert "Total Sales = SUM(" in dax.read_text() and "## 1. Measures" in md.read_text()


def test_plan_uses_view_name_when_no_pbi_table_is_set(env, monkeypatch):
    _, pbi, _ = env
    monkeypatch.delenv("PBI_TABLE")
    assert "'pbi_blinkit_sales'[Sales]" in pbi.powerbi_plan()["measures"][0]["dax"]


def test_plan_works_on_a_different_dataset(env):
    tools, pbi, _ = env
    pd.DataFrame({"city": ["A", "B", "A", "C"], "revenue": [10.0, 20.0, 30.0, 5.0],
                  "order_id": [1, 2, 3, 4]}).to_sql("shop", tools.get_engine(readonly=False), index=False)
    plan = pbi.powerbi_plan("shop", pbi_table="Shop")
    names = [m["name"] for m in plan["measures"]]
    assert "Total Revenue" in names and plan["slicers"] == ["City"]


# ---- save_insight ------------------------------------------------------------- #
def test_save_insight_roundtrip(env):
    tools, pbi, _ = env
    pbi.save_insight("Which outlet type earns most?", "Supermarket Type1 (65.5%)", "SELECT 1")
    pbi.save_insight("Second", "Answer two")
    rows = tools.run_sql("SELECT question, answer FROM agent_insights ORDER BY question")["rows"]
    assert ["Which outlet type earns most?", "Supermarket Type1 (65.5%)"] in rows and len(rows) == 2


# ---- agent wiring ------------------------------------------------------------- #
def test_every_tool_schema_has_a_matching_function(env):
    tools, _, agent = env
    names = {t["function"]["name"] for t in agent.TOOL_SCHEMAS}
    assert names == set(tools.TOOLS) and {"powerbi_plan", "create_powerbi_view", "save_insight"} <= names
    for t in agent.TOOL_SCHEMAS:
        f = t["function"]
        assert set(f["parameters"].get("required", [])) <= set(f["parameters"]["properties"])


def test_creating_the_view_needs_approval(env):
    tools, pbi, agent = env
    assert "create_powerbi_view" in agent.NEEDS_APPROVAL
    eng = tools.get_engine(readonly=False)
    with eng.begin() as c:
        c.execute(text("DROP VIEW IF EXISTS pbi_blinkit_sales"))
    out = json.loads(agent._call_tool("create_powerbi_view", "{}", lambda n, a: False))
    assert "declined" in out["error"]
    assert "pbi_blinkit_sales" not in inspect(eng).get_view_names()
    out = json.loads(agent._call_tool("create_powerbi_view", "{}", lambda n, a: True))
    assert out["rows"] == 8523


def test_plan_result_fits_in_the_llm_result_limit(env):
    _, _, agent = env
    out = agent._call_tool("powerbi_plan", "{}", lambda n, a: True)
    assert "TRUNCATED" not in out and "measures" in json.loads(out)