"""
Step 6: Power BI output. Three more tools for the agent:

    powerbi_plan(table)          -> DAX measures + a dashboard layout, saved to powerbi/<table>_plan.md and .dax
    create_powerbi_view(table)   -> a MySQL VIEW with Power BI-friendly column names; Power BI reads this
    save_insight(question, answer) -> stores a finding in the agent_insights table (for an "AI Insights" page)

Power BI itself cannot be driven by code from here, so the agent prepares the data and the instructions,
and you place the visuals. Everything is generated from the table's real columns, so it works for any dataset.
"""
import os
import re
from datetime import datetime

import pandas as pd
from sqlalchemy import inspect, text

import tools as T

PBI_DIR = T.ROOT / "powerbi"
INSIGHTS_TABLE = "agent_insights"
_NAME_OK = re.compile(r"[A-Za-z0-9_]+")


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #
def _title(col: str) -> str:
    """item_fat_content -> Item Fat Content (the names Power BI users expect)."""
    return col.replace("_", " ").strip().title()


def _entity(title: str) -> str:
    """'Outlet Identifier' -> 'Outlet'"""
    return re.sub(r"\s*(Identifier|Id|Code|No|Number|Key)$", "", title, flags=re.I).strip() or title


def _view_name(table: str) -> str:
    return f"pbi_{table}"


def _check_name(name: str, what: str) -> str:
    if not _NAME_OK.fullmatch(name or ""):
        raise ValueError(f"{what} may only contain letters, digits and underscores")
    return name


# --------------------------------------------------------------------------- #
# Tool: create_powerbi_view
# --------------------------------------------------------------------------- #
def create_powerbi_view(table: str = None, view_name: str = None) -> dict:
    """(Re)create a VIEW named pbi_<table> that exposes the table with Title Case column names.
    It always reflects the table's current rows, so Power BI only needs a Refresh."""
    table = T._table(table)
    view = _check_name(view_name or _view_name(table), "view_name")
    eng = T.get_engine(readonly=False)
    q = eng.dialect.identifier_preparer.quote
    cols = [c["name"] for c in inspect(eng).get_columns(table)]
    titles = [_title(c) for c in cols]
    if len(set(titles)) != len(titles):
        raise ValueError("Two columns would get the same Power BI name; rename one of them first.")
    select = ", ".join(f"{q(c)} AS {q(t)}" for c, t in zip(cols, titles))
    with eng.begin() as conn:
        conn.execute(text(f"DROP VIEW IF EXISTS {q(view)}"))
        conn.execute(text(f"CREATE VIEW {q(view)} AS SELECT {select} FROM {q(table)}"))
    n = int(pd.read_sql(text(f"SELECT COUNT(*) AS n FROM {q(view)}"), eng)["n"][0])
    db, host = os.getenv("DB_NAME", "your_database"), os.getenv("DB_HOST", "localhost")
    # Power BI's MySQL connector often cannot find views by navigation, so the query form is used.
    m_code = (f'let\n    Source = MySQL.Database("{host}", "{db}", [Query="SELECT * FROM {view}"])\nin\n    Source')
    return {"view": view, "rows": n, "columns": titles, "power_query_m": m_code,
            "sql_for_power_bi": f"SELECT * FROM {view}",
            "next": (f"In Power BI: Home > Get data > More > MySQL database > server {host}, database {db} > "
                     f"Advanced options > SQL statement: SELECT * FROM {view} > OK > Load. "
                     f"Then rename the new table to {view} so the DAX from powerbi_plan matches.")}


# --------------------------------------------------------------------------- #
# Tool: save_insight
# --------------------------------------------------------------------------- #
def save_insight(question: str, answer: str, sql_used: str = None) -> dict:
    """Append one finding to the agent_insights table (created on first use)."""
    row = pd.DataFrame([{"created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "question": (question or "")[:500], "answer": (answer or "")[:4000],
                         "sql_used": (sql_used or "")[:4000]}])
    row.to_sql(INSIGHTS_TABLE, T.get_engine(readonly=False), if_exists="append", index=False)
    return {"saved": True, "table": INSIGHTS_TABLE}


# --------------------------------------------------------------------------- #
# Tool: powerbi_plan
# --------------------------------------------------------------------------- #
_VISUAL_NAMES = {"bar": "Clustered bar chart", "column": "Clustered column chart", "line": "Line chart",
                 "pie": "Donut chart", "scatter": "Scatter chart"}


def powerbi_plan(table: str = None, pbi_table: str = None) -> dict:
    """Build DAX measures and a dashboard layout from the table's columns and save them as files."""
    table = T._table(table)
    pbi_table = pbi_table or os.getenv("PBI_TABLE") or _view_name(table)
    df = T._load(table, limit=50_000)
    roles = T._classify(df)
    measure = T._pick_measure(df, roles)
    if measure is None:
        raise ValueError("No numeric measure column found, so there is nothing to total or average.")
    ref = lambda c: f"'{pbi_table}'[{_title(c)}]"            # noqa: E731
    M = _title(measure)

    # ---- DAX measures --------------------------------------------------------------- #
    measures = []

    def add(name, expr, fmt, why):
        measures.append({"name": name, "dax": f"{name} = {expr}", "format": fmt, "why": why})

    add(f"Total {M}", f"SUM({ref(measure)})", "#,0", f"Headline total of {M}")
    add(f"Average {M}", f"AVERAGE({ref(measure)})", "#,0.00", f"Typical {M} per row")
    add("Row Count", f"COUNTROWS('{pbi_table}')", "#,0", "Number of rows in the current filter")

    id_cols = [c for c in df.columns if T._ID_NAME.search(c) and df[c].nunique() < len(df)]
    entities = []                                            # (entity label, count measure name)
    for c in id_cols:
        ent = _entity(_title(c))
        add(f"{ent} Count", f"DISTINCTCOUNT({ref(c)})", "#,0", f"How many different {ent.lower()}s")
        entities.append((ent, f"{ent} Count", df[c].nunique()))
        if df[c].nunique() <= 100:
            add(f"Average {M} per {ent}", f"DIVIDE([Total {M}], [{ent} Count])", "#,0",
                f"{M} per {ent.lower()}; stops big groups winning just because they have more {ent.lower()}s")

    others = [c for c, r in roles.items() if r == "numeric" and c != measure]
    others.sort(key=lambda c: 0 if re.search(r"rating|score|satisf", c, re.I) else 1)
    for c in others[:3]:
        add(f"Average {_title(c)}", f"AVERAGE({ref(c)})", "0.00", f"Typical {_title(c)}")

    add(f"{M} Share %",
        f"DIVIDE([Total {M}], CALCULATE([Total {M}], ALLSELECTED('{pbi_table}')))", "0.0%",
        "Each bar or slice as a share of the total (slicers still apply)")

    def measure_for(agg, col):
        return f"Total {M}" if (agg == "sum" and col == measure) else f"Average {_title(col)}"

    # ---- Layout --------------------------------------------------------------------- #
    cats = [c for c, r in roles.items() if r == "category" and not T._ID_NAME.search(c)]
    cats.sort(key=lambda c: (df[c].nunique(), c))
    slicers = [_title(c) for c in cats if df[c].nunique() <= 8][:5]

    cards = [f"Total {M}", f"Average {M}"]
    cards += [name for _, name, _ in entities[:2]]
    if others:
        cards.append(f"Average {_title(others[0])}")

    sugg = T.suggest_visuals(table, max_items=60)["suggestions"]
    candidates = []                                          # (priority, visual dict)
    for s in sugg:
        if s["visual"] in ("card", "pie") or (s.get("x") and T._ID_NAME.search(s["x"])):
            continue                                         # cards are listed above; donuts are decided in the bar branch
        x, y, agg, color = s.get("x"), s["y"], s["agg"], s.get("color")
        X, Y = _title(x), _title(y)
        distinct = df[x].nunique() if x in df else 0
        if s["visual"] == "bar" and agg == "sum":
            if distinct <= 4:                                # few categories: a donut shows the share
                candidates.append((3.0 if "type" in x else 3.5, {
                    "kind": "donut", "visual": "Donut chart",
                    "fields": f"Legend: {X}; Values: [{measure_for(agg, y)}]; show percentage",
                    "answers": f"Share of {M} by {X}"}))
            else:
                candidates.append((1, {
                    "kind": "bar", "visual": "Clustered bar chart",
                    "fields": f"Y-axis: {X}; X-axis: [{measure_for(agg, y)}]; sort high to low",
                    "answers": f"Which {X} brings in the most {M}"}))
        elif s["visual"] == "column" and not color:
            candidates.append((2, {
                "kind": "column", "visual": "Clustered column chart",
                "fields": f"X-axis: {X}; Y-axis: [{measure_for(agg, y)}]",
                "answers": f"{M} across {X} (columns, not an area chart: these are separate values)"}))
        elif s["visual"] == "column":
            candidates.append((4, {
                "kind": "column", "visual": "Clustered column chart",
                "fields": f"X-axis: {X}; Legend: {_title(color)}; Y-axis: [{measure_for(agg, y)}]",
                "answers": f"Compare {_title(color)} inside each {X}"}))
        elif s["visual"] == "bar":                           # average of another numeric column
            candidates.append((5, {
                "kind": "bar", "visual": "Clustered bar chart",
                "fields": f"Y-axis: {X}; X-axis: [{measure_for(agg, y)}]",
                "answers": f"Does average {Y} differ by {X}?"}))
        elif s["visual"] == "scatter":
            detail = f"; Details: {_title(id_cols[0])}" if id_cols else ""
            candidates.append((6, {
                "kind": "scatter", "visual": "Scatter chart",
                "fields": f"X-axis: {X}; Y-axis: {Y}{detail}",
                "answers": f"Relationship between {X} and {Y}"}))

    # the "fair comparison" visual: group totals can mislead when groups have different sizes
    big = [e for e in entities if e[2] <= 100]
    if big:
        ent, count_name, _ = big[0]
        peers = [c for c in cats if _title(c).startswith(ent)]
        peers.sort(key=lambda c: (0 if "type" in c else 1, len(c)))
        if peers:
            candidates.append((0, {
                "kind": "bar", "visual": "Clustered bar chart",
                "fields": f"Y-axis: {_title(peers[0])}; X-axis: [Average {M} per {ent}]; sort high to low",
                "answers": f"Typical {M} per {ent.lower()} for each {_title(peers[0])} (fair comparison)"}))

    candidates.sort(key=lambda t: t[0])                      # stable: keeps suggest_visuals order inside a priority
    charts, donuts = [], 0
    for _, v in candidates:
        if v["kind"] == "donut":
            if donuts >= 3:
                continue
            donuts += 1
        charts.append(v)
        if len(charts) == 8:
            break
    for i, v in enumerate(charts):
        page, j = ("Overview", i) if i < 6 else ("Details page", i - 6)
        v["zone"] = f"{page}, row {j // 2 + 1}, column {j % 2 + 1}"
        v.pop("kind")

    # ---- Files ---------------------------------------------------------------------- #
    PBI_DIR.mkdir(exist_ok=True)
    dax_path = PBI_DIR / f"{table}_measures.dax"
    md_path = PBI_DIR / f"{table}_plan.md"
    dax_path.write_text("-- In Power BI: Modeling > New measure. Paste ONE measure at a time (the line(s) below).\n\n"
                        + "\n\n".join(m["dax"] for m in measures) + "\n", encoding="utf-8")

    md = [f"# Power BI plan for `{table}`\n",
          f"Power BI table name used in the DAX: **{pbi_table}** "
          f"(change it with the PBI_TABLE setting or the `pbi_table` argument).\n",
          "## 1. Measures\nAdd one at a time with **Modeling > New measure**.\n"]
    for m in measures:
        md.append(f"**{m['name']}** - {m['why']} (format: `{m['format']}`)\n```DAX\n{m['dax']}\n```\n")
    md.append("## 2. Pages: Overview (+ Details if there are more than 6 charts)\n")
    md.append(f"- **Top row, cards:** {', '.join('[' + c + ']' for c in cards)}")
    md.append(f"- **Left rail, slicers:** {', '.join(slicers) or 'none suggested'}")
    md.append("\n| Where | Visual | Fields | Answers |\n|---|---|---|---|")
    for v in charts:
        md.append(f"| {v['zone']} | {v['visual']} | {v['fields']} | {v['answers']} |")
    md.append("\n## 3. Page 2: AI Insights (optional)\n"
              f"Get data > MySQL > `{INSIGHTS_TABLE}` (it appears after the agent saves its first insight). "
              "Add a **Table** visual with Created At, Question, Answer, and press Refresh after new insights.\n")
    md_path.write_text("\n".join(md), encoding="utf-8")

    return {"table": table, "power_bi_table": pbi_table, "measure_column": measure,
            "measures": [{"name": m["name"], "dax": m["dax"]} for m in measures],
            "cards": cards, "slicers": slicers, "charts": charts,
            "files": [str(md_path), str(dax_path)]}


POWERBI_TOOLS = {"powerbi_plan": powerbi_plan, "create_powerbi_view": create_powerbi_view,
                 "save_insight": save_insight}