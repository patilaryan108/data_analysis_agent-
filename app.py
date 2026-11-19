"""
Step 7: the web page.   Run:  streamlit run app.py

Upload any CSV/Excel file (or pick a table already in MySQL), then:
  - see what is in the data and what looks wrong,
  - chat with the agent (it runs SQL, draws charts, explains),
  - get DAX measures + a dashboard layout for Power BI.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import streamlit as st

import agent
import powerbi as P
import tools as T
import webapp_logic as W

st.set_page_config(page_title="Data Agent", page_icon="📊", layout="wide")
ss = st.session_state
ss.setdefault("table", None)
ss.setdefault("messages", [])        # what is shown on screen
ss.setdefault("history", [])         # what the LLM remembers


def use_table(name: str):
    if ss.table != name:
        ss.table, ss.messages, ss.history = name, [], []


# --------------------------------------------------------------------------- #
# Sidebar: choose data + permissions
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("📊 Data Agent")
    source = st.radio("Data source", ["Table already in MySQL", "Upload a CSV / Excel file"])

    if source == "Table already in MySQL":
        try:
            tables = W.list_tables()
        except Exception as e:                               # noqa: BLE001
            tables = []
            st.error(f"Cannot reach the database: {e}")
        if tables:
            default = tables.index(ss.table) if ss.table in tables else (
                tables.index(T.DEFAULT_TABLE) if T.DEFAULT_TABLE in tables else 0)
            use_table(st.selectbox("Table", tables, index=default))
        else:
            st.info("No tables found. Upload a file instead.")
    else:
        up = st.file_uploader("CSV or Excel (max 200,000 rows)", type=["csv", "xlsx", "xls", "xlsm"])
        if up is not None:
            data = up.getvalue()
            sheets = W.excel_sheets(data, up.name)
            sheet = st.selectbox("Sheet", sheets) if len(sheets) > 1 else None
            st.caption(f"Will be saved as table `{W.upload_table_name(up.name)}` (replaces an earlier upload of the same name).")
            if st.button("Load into the database", type="primary"):
                try:
                    res = W.save_upload(W.read_upload(data, up.name, sheet), up.name)
                    use_table(res["table"])
                    ss.upload_note = res
                    st.rerun()
                except Exception as e:                       # noqa: BLE001
                    st.error(str(e))
        if ss.get("upload_note"):
            n = ss.upload_note
            st.success(f"Loaded {n['rows']:,} rows into `{n['table']}`.")
            if n["renamed"]:
                st.caption("Columns renamed for SQL: " + ", ".join(f"{o} -> {c}" for o, c in list(n["renamed"].items())[:8]))

    st.divider()
    allow_writes = st.checkbox("Allow the agent to change the database",
                               help="Needed for cleaning data and creating the Power BI view. Off = read-only.")
    if st.button("Clear chat"):
        ss.messages, ss.history = [], []
        st.rerun()

if not ss.table:
    st.title("Data Agent")
    st.info("Pick a table or upload a file in the sidebar to start.")
    st.stop()

T.DEFAULT_TABLE = ss.table            # the agent and all tools now work on this table
st.header(f"Table: `{ss.table}`")
tab_chat, tab_data, tab_pbi = st.tabs(["💬 Ask the agent", "🔎 Data overview", "📈 Power BI"])


# --------------------------------------------------------------------------- #
# Tab 2: data overview
# --------------------------------------------------------------------------- #
with tab_data:
    try:
        prof = T.profile_data(ss.table)
    except Exception as e:                                   # noqa: BLE001
        st.error(str(e))
        prof = None
    if prof:
        c1, c2, c3 = st.columns(3)
        c1.metric("Rows", f"{prof['rows']:,}")
        c2.metric("Columns", len(prof["columns"]))
        c3.metric("Problems found", len(prof["issues"]))
        if prof["issues"]:
            st.warning("\n".join(f"- {i}" for i in prof["issues"]))
        else:
            st.success("No missing values, duplicates, or label problems detected.")
        st.dataframe(pd.DataFrame([{k: c.get(k) for k in ("name", "role", "dtype", "nulls", "distinct")}
                                   for c in prof["columns"]]), width="stretch", hide_index=True)
        st.caption("First 20 rows")
        st.dataframe(T._load(ss.table, limit=20), width="stretch", hide_index=True)
        if st.button("Clean this data (saves a new table)", disabled=not allow_writes,
                     help=None if allow_writes else "Tick 'Allow the agent to change the database' first."):
            try:
                ss.clean_result = T.clean_data(ss.table)
                agent._hint_cache.clear()
            except Exception as e:                           # noqa: BLE001
                st.error(str(e))
    res = ss.get("clean_result")
    if res:
        st.success(f"Saved `{res['new_table']}` ({res['rows_after']:,} rows).")
        st.json(res)
        if st.button(f"Switch to `{res['new_table']}`"):
            ss.clean_result = None
            use_table(res["new_table"])
            st.rerun()


# --------------------------------------------------------------------------- #
# Tab 3: Power BI
# --------------------------------------------------------------------------- #
with tab_pbi:
    st.write("Get DAX measures and a dashboard layout for Power BI, built from this table's columns.")
    pbi_name = st.text_input("Table name inside Power BI (optional)", value="",
                             help="Used in the DAX. Leave empty to use the view name pbi_<table>.")
    if st.button("Generate Power BI plan"):
        try:
            ss.plan = P.powerbi_plan(ss.table, pbi_name or None)
        except Exception as e:                               # noqa: BLE001
            st.error(str(e))
    plan = ss.get("plan")
    if plan and plan["table"] == ss.table:
        st.subheader("DAX measures (add one at a time: Modeling > New measure)")
        for m in plan["measures"]:
            st.code(m["dax"], language="sql")
        st.subheader("Dashboard layout")
        st.write("**Cards:** " + ", ".join(f"[{c}]" for c in plan["cards"]))
        st.write("**Slicers:** " + (", ".join(plan["slicers"]) or "none"))
        st.dataframe(pd.DataFrame(plan["charts"])[["zone", "visual", "fields", "answers"]],
                     width="stretch", hide_index=True)
        for f in plan["files"]:
            st.download_button(f"Download {Path(f).name}", Path(f).read_bytes(), file_name=Path(f).name,
                               key=f"dl_{Path(f).name}")
    st.divider()
    st.write("Power BI reads a **view** with clean, Title Case column names:")
    if st.button("Create / refresh the Power BI view", disabled=not allow_writes,
                 help=None if allow_writes else "Tick 'Allow the agent to change the database' first."):
        try:
            v = P.create_powerbi_view(ss.table)
            st.success(f"View `{v['view']}` is ready ({v['rows']:,} rows). Now bring it into Power BI:")
            st.markdown(
                "**Add it as a NEW table** (keeps your existing dashboard):\n"
                "1. Power BI Desktop: **Home > Get data > More...** > **MySQL database** > Connect\n"
                f"2. Server `localhost`, Database `{os.getenv('DB_NAME') or 'your_database'}`; "
                "open **Advanced options** and paste the SQL below into **SQL statement**\n"
                "3. **OK**, sign in on the *Database* tab if asked, then **Load**\n"
                f"4. Right-click the new table in the Data pane > **Rename** to `{v['view']}` "
                "(so the DAX from the plan matches)\n\n"
                "**Or replace an existing table's data:** Transform data > Advanced Editor > paste the code in the second box.")
            st.code(v["sql_for_power_bi"], language="sql")
            st.code(v["power_query_m"], language="text")
        except Exception as e:                               # noqa: BLE001
            st.error(str(e))


# --------------------------------------------------------------------------- #
# Tab 1: chat
# --------------------------------------------------------------------------- #
def show_message(i: int, m: dict):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        for spec in m.get("charts", []):
            fig = W.figure_from_spec(spec)
            if fig is not None:
                st.plotly_chart(fig, width="stretch")
        if m.get("steps"):
            with st.expander(f"How the agent got this ({len(m['steps'])} tool calls)"):
                for s in m["steps"]:
                    st.code(f"{s['tool']}({s['args']})", language="text")
        if m["role"] == "assistant" and m.get("question") and st.button("Save as insight", key=f"save_{i}"):
            P.save_insight(m["question"], m["content"], W.last_sql(m.get("steps", [])))
            st.toast("Saved to agent_insights")


with tab_chat:
    for i, m in enumerate(ss.messages):
        show_message(i, m)

    if not ss.messages:
        st.caption("Try one of these:")
        try:
            starters = W.sample_questions(ss.table)
        except Exception:                                    # noqa: BLE001
            starters = []
        cols = st.columns(2)
        for j, q in enumerate(starters):
            if cols[j % 2].button(q, key=f"starter_{j}"):
                ss.pending = q

    prompt = st.chat_input("Ask a question about this data") or ss.pop("pending", None)
    if prompt:
        ss.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            blocked = []

            def approve(name, args):
                if not allow_writes:
                    blocked.append(name)
                return allow_writes

            t0 = time.perf_counter()
            with st.status("Working...", expanded=True) as status:
                try:
                    out = agent.run_agent(prompt, ss.history, approve=approve, progress=status.write)
                    status.update(label=f"Done in {time.perf_counter() - t0:.1f}s", state="complete", expanded=False)
                except Exception as e:                       # noqa: BLE001
                    out = None
                    status.update(label="Failed", state="error")
                    st.error(f"{type(e).__name__}: {e}")
            if out:
                ss.history = out["history"]
                msg = {"role": "assistant", "content": out["answer"], "question": prompt,
                       "steps": out["steps"], "charts": W.chart_specs_from_steps(out["steps"])}
                ss.messages.append(msg)
                st.markdown(msg["content"])
                for spec in msg["charts"]:
                    fig = W.figure_from_spec(spec)
                    if fig is not None:
                        st.plotly_chart(fig, width="stretch")
                if blocked:
                    st.warning("The agent wanted to change the database (" + ", ".join(sorted(set(blocked))) +
                               "). Tick 'Allow the agent to change the database' in the sidebar and ask again.")