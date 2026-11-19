"""
Step 4: The agent's tools.

Five plain Python functions the LLM agent can call (the agent itself comes in Step 5):

    profile_data(table)      -> what is in the table and what looks wrong
    clean_data(table, ...)   -> generic cleaning, writes a NEW table (original is never touched)
    run_sql(query)           -> read-only SELECT queries only
    suggest_visuals(table)   -> which charts fit which columns
    make_chart(...)          -> draws a chart (saved as an HTML file in charts/)

Every function returns JSON-friendly dicts/lists so the LLM can read the result.
"""
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = ROOT / "charts"
load_dotenv(ROOT / ".env")

DEFAULT_TABLE = os.getenv("DB_TABLE", "blinkit_sales")
MAX_ROWS = 200          # max rows run_sql returns to the LLM
_engines = {}


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def get_engine(readonly: bool = True):
    """MySQL engine from .env. If DB_RO_USER is set, read-only calls use that user.
    DATABASE_URL (optional) overrides everything - handy for tests."""
    url = os.getenv("DATABASE_URL")
    if not url:
        ro_user = os.getenv("DB_RO_USER")
        if readonly and ro_user:
            user, pwd = ro_user, os.getenv("DB_RO_PASSWORD", "")
        else:
            user, pwd = os.getenv("DB_USER"), os.getenv("DB_PASSWORD", "")
        host = os.getenv("DB_HOST", "localhost")
        name = os.getenv("DB_NAME")
        url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}/{name}"
    if url not in _engines:
        _engines[url] = create_engine(url)
    return _engines[url]


def _table(table=None) -> str:
    t = table or DEFAULT_TABLE
    names = inspect(get_engine()).get_table_names()
    if t not in names:
        raise ValueError(f"Unknown table {t!r}. Available tables: {names}")
    return t


def _q(engine, name: str) -> str:
    """Safely quote a table/column name."""
    return engine.dialect.identifier_preparer.quote(name)


def _load(table: str, limit=100_000, engine=None) -> pd.DataFrame:
    eng = engine or get_engine()
    sql = f"SELECT * FROM {_q(eng, table)}" + (f" LIMIT {int(limit)}" if limit else "")
    return pd.read_sql(text(sql), eng)


def _py(v):
    """Convert DB / numpy values into plain Python so the result is JSON-friendly."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, (datetime, date, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, float) and np.isnan(v):
        return None
    if v is pd.NA:
        return None
    return v


def _records(df: pd.DataFrame, limit=None) -> list:
    if limit:
        df = df.head(limit)
    return [{c: _py(v) for c, v in row.items()} for row in df.to_dict("records")]


# --------------------------------------------------------------------------- #
# Column classification (used by profile_data and suggest_visuals)
# --------------------------------------------------------------------------- #
_ID_NAME = re.compile(r"(^|_)(id|identifier|code|no|number|key)$", re.I)
_TIME_NAME = re.compile(r"(year|date|month|day|week|quarter)", re.I)
_MEASURE_NAME = re.compile(r"(sales|revenue|amount|price|profit|total|quantity|units|cost)", re.I)


def _classify(df: pd.DataFrame) -> dict:
    """col -> 'id' | 'time' | 'numeric' | 'category' | 'text'"""
    n = max(len(df), 1)
    roles = {}
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            roles[c] = "time"
        elif pd.api.types.is_numeric_dtype(s):
            if _ID_NAME.search(c):
                roles[c] = "id"
            elif _TIME_NAME.search(c) and s.nunique() <= 40:
                roles[c] = "time"
            else:
                roles[c] = "numeric"
        else:
            distinct = s.nunique()
            if distinct <= 20:
                roles[c] = "category"
            elif _ID_NAME.search(c) or distinct / n > 0.5:
                roles[c] = "id"
            else:
                roles[c] = "text"
    return roles


def _pick_measure(df: pd.DataFrame, roles: dict):
    nums = [c for c, r in roles.items() if r == "numeric"]
    for c in nums:
        if _MEASURE_NAME.search(c):
            return c
    return nums[0] if nums else None


# --------------------------------------------------------------------------- #
# Tool 1: profile_data
# --------------------------------------------------------------------------- #
def profile_data(table: str = None) -> dict:
    """Describe the table: columns, types, missing values, top values, and likely problems."""
    table = _table(table)
    df = _load(table)
    roles = _classify(df)
    issues, columns = [], []

    for c in df.columns:
        s = df[c]
        info = {"name": c, "dtype": str(s.dtype), "role": roles[c],
                "nulls": int(s.isna().sum()), "distinct": int(s.nunique())}
        if pd.api.types.is_numeric_dtype(s):
            info.update(min=_py(s.min()), max=_py(s.max()),
                        mean=round(float(s.mean()), 3), median=_py(s.median()),
                        zeros=int((s == 0).sum()))
            if info["zeros"]:
                issues.append(f"{c}: {info['zeros']} zero values (check if zero is realistic)")
        elif info["distinct"] <= 30:
            info["top_values"] = {str(k): int(v) for k, v in s.value_counts().head(15).items()}
            # same label written in different case / spacing
            norm = s.dropna().astype(str).str.strip().str.lower()
            raw = s.dropna().astype(str)
            bad = [sorted(set(g)) for _, g in raw.groupby(norm) if len(set(g)) > 1]
            if bad:
                issues.append(f"{c}: same label written differently (case/spaces): {bad}")
        if info["nulls"]:
            issues.append(f"{c}: {info['nulls']} missing values")
        columns.append(info)

    dups = int(df.duplicated().sum())
    if dups:
        issues.append(f"{dups} fully duplicated rows")

    return {"table": table, "rows": len(df), "columns": columns, "issues": issues,
            "note": "Read top_values yourself: labels like 'LF' vs 'Low Fat' are not auto-detected.",
            "sample_rows": _records(df, 3)}


# --------------------------------------------------------------------------- #
# Tool 2: clean_data
# --------------------------------------------------------------------------- #
def clean_data(table: str = None, new_table: str = None, value_map: dict = None) -> dict:
    """Generic cleaning. Writes the result to a NEW table (default '<table>_clean').

    value_map lets the agent fix labels it spotted, e.g.
        {"item_fat_content": {"LF": "Low Fat", "reg": "Regular"}}
    Steps: trim spaces -> apply value_map -> merge case variants -> drop duplicate rows
           -> fill numeric nulls with the median and text nulls with 'Unknown'.
    """
    table = _table(table)
    new_table = new_table or f"{table}_clean"
    if not re.fullmatch(r"[A-Za-z0-9_]+", new_table):
        raise ValueError("new_table may only contain letters, digits and underscores")

    eng = get_engine(readonly=False)
    df = _load(table, limit=None, engine=eng)
    actions = {"rows_before": len(df)}

    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c]):
            continue
        s = df[c].astype("string").str.strip()
        before = s.copy()
        if value_map and c in value_map:
            s = s.replace(value_map[c])
        norm = s.str.lower()
        canon = s.groupby(norm).agg(lambda x: x.value_counts().idxmax())
        s = norm.map(canon).where(s.notna())
        changed = int((s.fillna("") != before.fillna("")).sum())
        if changed:
            actions[f"{c}_labels_fixed"] = changed
        df[c] = s.astype(object).where(s.notna(), None)

    n_dups = int(df.duplicated().sum())
    df = df.drop_duplicates()
    actions["duplicates_removed"] = n_dups

    for c in df.columns:
        n_null = int(df[c].isna().sum())
        if not n_null:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            df[c] = df[c].fillna(df[c].median())
            actions[f"{c}_nulls_filled_with_median"] = n_null
        else:
            df[c] = df[c].fillna("Unknown")
            actions[f"{c}_nulls_filled_with_Unknown"] = n_null

    df.to_sql(new_table, eng, if_exists="replace", index=False, chunksize=1000)
    actions.update(rows_after=len(df), new_table=new_table)
    return actions


# --------------------------------------------------------------------------- #
# Tool 3: run_sql  (read-only)
# --------------------------------------------------------------------------- #
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|rename|call|exec|"
    r"execute|handler|into|outfile|dumpfile|load_file|sleep|benchmark|lock|unlock|merge)\b",
    re.I,
)


def validate_sql(query: str) -> str:
    """Return the cleaned query if it is a single read-only SELECT, else raise ValueError."""
    q = re.sub(r"/\*.*?\*/", " ", query, flags=re.S)       # /* comments */
    q = re.sub(r"(--|#)[^\n]*", " ", q)                    # -- and # comments
    q = q.strip().rstrip(";").strip()
    no_strings = re.sub(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"", "''", q)  # ignore text in quotes
    if ";" in no_strings:
        raise ValueError("Only one statement is allowed.")
    if not re.match(r"(?is)^\s*(select|with)\b", no_strings):
        raise ValueError("Only SELECT queries are allowed.")
    hit = _FORBIDDEN.search(no_strings)
    if hit:
        raise ValueError(f"Query contains a forbidden keyword: {hit.group(0).upper()}")
    return q


def run_sql(query: str, max_rows: int = MAX_ROWS) -> dict:
    """Run a read-only SELECT and return columns + rows (at most max_rows)."""
    q = validate_sql(query)
    with get_engine().connect() as conn:
        result = conn.execute(text(q.replace(":", r"\:")))     # '\:' stops ':' being read as a parameter
        cols = list(result.keys())
        rows = result.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {"columns": cols,
            "rows": [[_py(v) for v in r] for r in rows],
            "row_count": len(rows), "truncated": truncated}


# --------------------------------------------------------------------------- #
# Tool 4: suggest_visuals
# --------------------------------------------------------------------------- #
def suggest_visuals(table: str = None, max_items: int = 24) -> dict:
    """Rule-based chart ideas. Each item can be passed straight to make_chart()."""
    table = _table(table)
    df = _load(table, limit=50_000)
    roles = _classify(df)
    measure = _pick_measure(df, roles)
    if measure is None:
        return {"table": table, "measure": None, "suggestions": [],
                "note": "No numeric measure found."}

    cats = sorted([c for c, r in roles.items() if r == "category"], key=lambda c: df[c].nunique())
    times = [c for c, r in roles.items() if r == "time"]
    others = [c for c, r in roles.items() if r == "numeric" and c != measure]
    others.sort(key=lambda c: 0 if re.search(r"rating|score|satisf", c, re.I) else 1)   # ratings first
    out = []

    out.append({"visual": "card", "y": measure, "agg": "sum", "why": f"Headline total of {measure}"})
    out.append({"visual": "card", "y": measure, "agg": "avg", "why": f"Typical {measure} per row"})
    for c in cats[:8]:
        out.append({"visual": "bar", "x": c, "y": measure, "agg": "sum",
                    "why": f"Which {c} brings in the most {measure}"})
        if df[c].nunique() <= 6:
            out.append({"visual": "pie", "x": c, "y": measure, "agg": "sum",
                        "why": f"Share of {measure} by {c} (few categories, so a donut works)"})
    for t in times[:1]:
        out.append({"visual": "column", "x": t, "y": measure, "agg": "sum",
                    "why": f"{measure} across {t} (use columns, not an area chart, if {t} is only a few separate years)"})
    if len(cats) >= 2:
        out.append({"visual": "column", "x": cats[0], "y": measure, "agg": "sum", "color": cats[1],
                    "why": f"Compare {cats[1]} inside each {cats[0]}"})
    for o in others[:2]:
        for c in cats[:2]:
            out.append({"visual": "bar", "x": c, "y": o, "agg": "avg",
                        "why": f"Does average {o} differ by {c}?"})
        out.append({"visual": "scatter", "x": o, "y": measure, "agg": "none",
                    "why": f"Relationship between {o} and {measure}"})

    return {"table": table, "measure": measure, "roles": roles, "suggestions": out[:max_items]}


# --------------------------------------------------------------------------- #
# Tool 5: make_chart
# --------------------------------------------------------------------------- #
_AGGS = {"sum": "SUM", "avg": "AVG", "count": "COUNT", "min": "MIN", "max": "MAX"}
_CHARTS = {"bar", "column", "line", "pie", "scatter"}


def build_figure(chart_type: str, x: str, y: str, agg: str = "sum", color: str = None,
                 table: str = None, top_n: int = None, title: str = None):
    """Build the SQL, run it, and return (plotly_figure, dataframe, sql_used).
    Column names are checked against the real table, so nothing from the LLM goes into SQL unchecked."""
    table = _table(table)
    chart_type, agg = chart_type.lower(), agg.lower()
    if chart_type not in _CHARTS:
        raise ValueError(f"chart_type must be one of {sorted(_CHARTS)}")
    eng = get_engine()
    valid = [c["name"] for c in inspect(eng).get_columns(table)]
    for col in (x, y, color):
        if col and col not in valid:
            raise ValueError(f"Unknown column {col!r}. Columns: {valid}")
    qt, qx, qy = _q(eng, table), _q(eng, x), _q(eng, y)
    qc = _q(eng, color) if color else None

    if chart_type == "scatter":
        sql = f"SELECT {qx}, {qy}" + (f", {qc}" if qc else "") + f" FROM {qt} LIMIT 5000"
        df = pd.read_sql(text(sql), eng)
        fig = px.scatter(df, x=x, y=y, color=color, title=title or f"{y} vs {x}")
        return fig, df, sql

    if agg not in _AGGS:
        raise ValueError(f"agg must be one of {sorted(_AGGS)}")
    value = f"{agg}_{y}"
    group = f"{qx}" + (f", {qc}" if qc else "")
    order = f"{qx}" if chart_type == "line" or _looks_like_time(x) else f"{_q(eng, value)} DESC"
    sql = (f"SELECT {group}, {_AGGS[agg]}({qy}) AS {_q(eng, value)} FROM {qt} "
           f"GROUP BY {group} ORDER BY {order}")
    if top_n:
        sql += f" LIMIT {int(top_n)}"
    df = pd.read_sql(text(sql), eng)
    df[value] = df[value].astype(float)
    title = title or f"{agg.upper()} of {y} by {x}"

    if chart_type == "bar":                       # horizontal, biggest at the top
        fig = px.bar(df, x=value, y=x, color=color, orientation="h", title=title)
        fig.update_yaxes(categoryorder="total ascending")
    elif chart_type == "column":
        df[x] = df[x].astype(str)
        fig = px.bar(df, x=x, y=value, color=color, barmode="group", title=title)
    elif chart_type == "line":
        fig = px.line(df, x=x, y=value, color=color, markers=True, title=title)
    else:                                         # pie -> donut
        fig = px.pie(df, names=x, values=value, hole=0.45, title=title)
    return fig, df, sql


def _looks_like_time(col: str) -> bool:
    return bool(_TIME_NAME.search(col))


def make_chart(chart_type: str, x: str, y: str, agg: str = "sum", color: str = None,
               table: str = None, top_n: int = None, title: str = None) -> dict:
    """Draw a chart, save it as charts/<name>.html, and return the path plus the data behind it."""
    fig, df, sql = build_figure(chart_type, x, y, agg, color, table, top_n, title)
    CHART_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", f"{chart_type}_{agg}_{y}_by_{x}{'_' + color if color else ''}".lower())
    path = CHART_DIR / f"{slug}.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    return {"chart_file": str(path), "title": fig.layout.title.text, "sql_used": sql,
            "data": _records(df, 50)}


# --------------------------------------------------------------------------- #
# Registry: Step 5 will hand these to the LLM
# --------------------------------------------------------------------------- #
TOOLS = {
    "profile_data": profile_data,
    "clean_data": clean_data,
    "run_sql": run_sql,
    "suggest_visuals": suggest_visuals,
    "make_chart": make_chart,
}


if __name__ == "__main__":
    import json
    print(json.dumps(profile_data(), indent=2)[:1500])
    print(json.dumps(run_sql("SELECT outlet_type, ROUND(SUM(sales),0) AS total_sales "
                             "FROM blinkit_sales GROUP BY outlet_type ORDER BY total_sales DESC"), indent=2))