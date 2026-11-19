"""
Step 7 helpers: everything the web page does that is not drawing widgets, so it can be tested.
"""
import io
import json
import re
from pathlib import Path

import pandas as pd
from sqlalchemy import inspect

import tools as T

MAX_UPLOAD_ROWS = 200_000
HIDDEN_TABLES = {"agent_insights"}
_CHART_KEYS = ("chart_type", "x", "y", "agg", "color", "table", "top_n", "title")


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #
def sanitize_columns(cols) -> list:
    """'Item Fat Content' -> item_fat_content; duplicates get _2, _3; names never start with a digit."""
    out, used = [], set()
    for c in cols:
        s = re.sub(r"[^0-9a-zA-Z]+", "_", str(c).strip()).strip("_").lower() or "column"
        if s[0].isdigit():
            s = "c_" + s
        base, n = s, 1
        while s in used:
            n += 1
            s = f"{base}_{n}"
        used.add(s)
        out.append(s)
    return out


def upload_table_name(filename: str) -> str:
    stem = re.sub(r"[^0-9a-zA-Z]+", "_", Path(filename).stem).strip("_").lower() or "data"
    return f"upload_{stem}"[:60]


def read_upload(data: bytes, filename: str, sheet: str = None) -> pd.DataFrame:
    """Read a CSV or Excel upload into a DataFrame."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0)
    if name.endswith((".csv", ".txt")):
        return pd.read_csv(io.BytesIO(data), encoding_errors="replace")
    raise ValueError("Please upload a .csv or .xlsx file.")


def excel_sheets(data: bytes, filename: str) -> list:
    if filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return pd.ExcelFile(io.BytesIO(data)).sheet_names
    return []


def save_upload(df: pd.DataFrame, filename: str) -> dict:
    """Clean the column names and store the data in a new table called upload_<file name>."""
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        raise ValueError("The file has no data.")
    if len(df) > MAX_UPLOAD_ROWS:
        raise ValueError(f"The file has {len(df):,} rows; the limit is {MAX_UPLOAD_ROWS:,}.")
    old = list(df.columns)
    df = df.copy()
    df.columns = sanitize_columns(old)
    table = upload_table_name(filename)
    df.to_sql(table, T.get_engine(readonly=False), if_exists="replace", index=False, chunksize=1000)
    return {"table": table, "rows": len(df), "columns": list(df.columns),
            "renamed": {str(o): n for o, n in zip(old, df.columns) if str(o) != n}}


def list_tables() -> list:
    return [t for t in inspect(T.get_engine()).get_table_names() if t not in HIDDEN_TABLES]


# --------------------------------------------------------------------------- #
# Questions, charts and insights
# --------------------------------------------------------------------------- #
def _words(col: str) -> str:
    return col.replace("_", " ")


def sample_questions(table: str, n: int = 5) -> list:
    """Starter questions generated from the table's own columns."""
    sug = T.suggest_visuals(table, max_items=60)
    measure = sug.get("measure")
    if not measure:
        return ["Describe this dataset."]
    qs = []
    for s in sug["suggestions"]:
        x, y = s.get("x"), s["y"]
        if s["visual"] == "bar" and s["agg"] == "sum":
            qs.append(f"Which {_words(x)} brings in the most {_words(y)}, and what share of the total is that?")
        elif s["visual"] == "column" and not s.get("color") and s["agg"] == "sum":
            qs.append(f"How does {_words(y)} change across {_words(x)}?")
        elif s["visual"] == "bar" and s["agg"] == "avg":
            qs.append(f"Does average {_words(y)} differ by {_words(x)}?")
    return list(dict.fromkeys(qs))[:n] or ["Describe this dataset."]


def chart_specs_from_steps(steps: list) -> list:
    """Pick the successful make_chart calls out of an agent run, so the page can draw them."""
    specs = []
    for s in steps:
        if s.get("tool") != "make_chart" or s.get("result", "").startswith('{"error"'):
            continue
        try:
            a = json.loads(s.get("args") or "{}")
        except json.JSONDecodeError:
            continue
        if all(k in a for k in ("chart_type", "x", "y")):
            specs.append(a)
    return specs


def figure_from_spec(spec: dict):
    """Rebuild a Plotly figure from make_chart arguments. Returns None if it cannot be built."""
    kwargs = {k: spec[k] for k in _CHART_KEYS if spec.get(k) is not None}
    kwargs.setdefault("agg", "sum")
    try:
        return T.build_figure(**kwargs)[0]
    except Exception:                                        # noqa: BLE001
        return None


def last_sql(steps: list):
    """The last SQL the agent ran (saved together with an insight)."""
    for s in reversed(steps):
        if s.get("tool") == "run_sql":
            try:
                return json.loads(s["args"]).get("query")
            except (json.JSONDecodeError, KeyError, TypeError):
                return None
    return None