"""
Step 5: The agent. An LLM that decides which of the Step 4 tools to call, reads the results,
and answers in plain language.

It talks to the LLM through the OpenAI-style chat API, which Gemini (Google AI Studio), Groq and
Ollama all offer. Put ONE of these in .env:

    # Google AI Studio (Gemini)
    LLM_API_KEY=...
    LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
    LLM_MODEL=<model name from AI Studio>

    # Groq
    GROQ_API_KEY=gsk_...
    GROQ_MODEL=<a Groq model that supports tool use>

LLM_* settings win over GROQ_* ones. Optional: LLM_REASONING_EFFORT, LLM_TIMEOUT, LLM_MAX_TOOL_CHARS.

Run:   python src/agent.py          (chat)
       python src/agent.py --ping   (checks the key, model and speed with one tiny request)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import powerbi as P
import tools as T

T.TOOLS.update(P.POWERBI_TOOLS)      # Step 6 tools join the Step 4 tools

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MAX_STEPS = 8               # tool-call rounds per question
MAX_TOOL_CHARS = int(os.getenv("LLM_MAX_TOOL_CHARS", 6000))   # longer tool results are cut before the LLM sees them
MAX_HISTORY_TURNS = 6       # earlier questions kept for follow-ups
NEEDS_APPROVAL = {"clean_data", "create_powerbi_view"}   # tools that WRITE to the database ask the human first
GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_HOST = "generativelanguage.googleapis.com"

SYSTEM_PROMPT = """You are a data analyst agent for a sales database. The main table is `{table}`.

Table schema (already profiled for you, so you do NOT need profile_data for this table):
{schema}

Rules:
1. Never guess numbers. Get every figure with run_sql (or another tool) before you state it.
2. Go straight to run_sql when the schema above is enough. Use profile_data only for a different table, and clean_data only if real data problems are listed.
3. run_sql accepts one read-only SELECT. Aggregate in SQL (SUM, AVG, COUNT, GROUP BY) instead of pulling raw rows. Use the exact column names above. Several questions can usually be answered with ONE query.
4. To draw a chart, call make_chart. suggest_visuals gives chart ideas that fit the columns.
5. If a tool returns an error, read it, fix your call, and try again.
6. Keep answers short: lead with the answer, then 2-4 bullets of supporting numbers. Round money sensibly.
7. If the data cannot answer the question (for example, there is no date column for a trend), say so plainly.
8. Where useful, add one line on how to show it in Power BI (visual type + fields).
9. Do ALL arithmetic in SQL, never in your head. For shares use ROUND(SUM(x)*100.0/SUM(SUM(x)) OVER (), 1) and report percentages with one decimal. For "bottom N" or "top N" totals, sum them in SQL too.
10. When comparing groups, also return how big each group is (COUNT(*), and COUNT(DISTINCT ...) of the entity that matters, such as outlets) and the average per row or per entity, because a group can lead just by being bigger. Say so when size explains the result.
11. Do not invent units or labels: use no currency symbol unless the data names one, and do not call rows "transactions" unless the schema says each row is one.
12. Describe what the data shows. Do not claim causes (pricing, demand, quality) that the data did not test; if an obvious confounder exists (for example outlet type versus outlet age), mention it in one short line.
13. Power BI: when asked what to show in Power BI, or for DAX measures or a dashboard layout, call powerbi_plan and summarise its result (it also saves files). Call create_powerbi_view only when asked to prepare or refresh the Power BI table. Call save_insight only when asked to save a finding."""

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "profile_data",
        "description": "Describe a table: columns, types, missing values, top values, and likely data problems.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string", "description": "Table name. Omit for the default table."}}}}},
    {"type": "function", "function": {
        "name": "clean_data",
        "description": "Clean a table and save the result as a NEW table (original is untouched). Needs human approval.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string"},
            "new_table": {"type": "string", "description": "Name for the cleaned table. Default: <table>_clean."},
            "value_map_json": {"type": "string",
                               "description": 'Optional JSON string of label fixes, e.g. {"item_fat_content": {"LF": "Low Fat"}}'}}}}},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Run ONE read-only SELECT query and get the rows back (max 200).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "suggest_visuals",
        "description": "Suggest charts that fit the table's columns. Each suggestion can be passed to make_chart.",
        "parameters": {"type": "object", "properties": {"table": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "make_chart",
        "description": "Draw a chart and save it as an HTML file. Returns the file path and the data behind it.",
        "parameters": {"type": "object", "properties": {
            "chart_type": {"type": "string", "enum": ["bar", "column", "line", "pie", "scatter"]},
            "x": {"type": "string", "description": "Column for categories / x axis."},
            "y": {"type": "string", "description": "Numeric column to measure."},
            "agg": {"type": "string", "enum": ["sum", "avg", "count", "min", "max"]},
            "color": {"type": "string", "description": "Optional second category column to split by."},
            "top_n": {"type": "integer"},
            "title": {"type": "string"}},
            "required": ["chart_type", "x", "y"]}}},
    {"type": "function", "function": {
        "name": "powerbi_plan",
        "description": "Create DAX measures and a dashboard layout (cards, slicers, charts) for Power BI from the table's columns. Saves files in powerbi/.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string"},
            "pbi_table": {"type": "string", "description": "Table name inside Power BI, used in the DAX. Optional."}}}}},
    {"type": "function", "function": {
        "name": "create_powerbi_view",
        "description": "Create or refresh a database VIEW with Power BI-friendly column names. Needs human approval.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string"},
            "view_name": {"type": "string", "description": "Optional. Default: pbi_<table>."}}}}},
    {"type": "function", "function": {
        "name": "save_insight",
        "description": "Save a finding to the agent_insights table so Power BI can show it. Only when the user asks to save.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "answer": {"type": "string"},
            "sql_used": {"type": "string"}},
            "required": ["question", "answer"]}}},
]


# --------------------------------------------------------------------------- #
# Configuration / client
# --------------------------------------------------------------------------- #
def _cfg():
    """(api_key, base_url, model). LLM_* settings win; otherwise GROQ_* settings are used."""
    key = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
    url = os.getenv("LLM_BASE_URL") or (GROQ_URL if os.getenv("GROQ_API_KEY") else None)
    model = os.getenv("LLM_MODEL") or os.getenv("GROQ_MODEL")
    return key, url, model


def get_client():
    """timeout: never wait silently for minutes. max_retries=0: WE retry, and tell you about it."""
    from openai import OpenAI
    key, url, model = _cfg()
    if not (key and url and model):
        raise RuntimeError("Set LLM_API_KEY, LLM_BASE_URL and LLM_MODEL in your .env file "
                           "(or GROQ_API_KEY and GROQ_MODEL for Groq).")
    return OpenAI(api_key=key, base_url=url, timeout=float(os.getenv("LLM_TIMEOUT", 60)), max_retries=0)


def _effort_kwargs() -> dict:
    """Gemini 3 models 'think' before answering, which is slow. Ask for low effort unless told otherwise."""
    effort = os.getenv("LLM_REASONING_EFFORT")
    if not effort and GEMINI_HOST in (_cfg()[1] or ""):
        effort = "low"
    return {"reasoning_effort": effort} if effort else {}


def _assistant_message(msg) -> dict:
    """The assistant message to store in the history.
    Only Gemini needs its extra fields sent back (thought signatures). Other providers, Groq included,
    can reject unknown fields, so for them we keep just role / content / tool_calls."""
    full = msg.model_dump(exclude_none=True)
    if GEMINI_HOST in (_cfg()[1] or ""):
        return full
    out = {"role": "assistant", "content": msg.content or ""}
    if full.get("tool_calls"):
        out["tool_calls"] = [{"id": c["id"], "type": "function",
                              "function": {"name": c["function"]["name"],
                                           "arguments": c["function"]["arguments"]}}
                             for c in full["tool_calls"]]
    return out


# --------------------------------------------------------------------------- #
# Schema hint: tell the LLM about the table up front, so it needs no profile_data round trip
# --------------------------------------------------------------------------- #
_hint_cache = {}                                             # table name -> schema text


def schema_hint() -> str:
    table = T.DEFAULT_TABLE
    if table not in _hint_cache:
        try:
            p = T.profile_data(table)
        except Exception:                                    # noqa: BLE001 - DB down: agent can still call tools
            return "(not available - call profile_data)"
        lines = [f"{p['table']} - {p['rows']} rows"]
        for c in p["columns"]:
            line = f"- {c['name']} [{c['role']}]"
            if "top_values" in c:
                more = c["distinct"] - len(c["top_values"])
                line += ": " + ", ".join(c["top_values"]) + (f" (+{more} more)" if more > 0 else "")
            elif "min" in c:
                line += f": {c['min']} to {c['max']}"
            lines.append(line)
        if p["issues"]:
            lines.append("Known issues: " + "; ".join(p["issues"]))
        _hint_cache[table] = "\n".join(lines)
    return _hint_cache[table]


# --------------------------------------------------------------------------- #
# Tool execution
# --------------------------------------------------------------------------- #
def cli_approve(name: str, args: dict) -> bool:
    print(f"\n[approval needed] The agent wants to run {name} with {json.dumps(args)}")
    return input("Allow it? [y/N] ").strip().lower() == "y"


def _dispatch(name: str, args: dict):
    """Call the real Python tool. Translates the LLM-friendly value_map_json."""
    if name not in T.TOOLS:
        raise ValueError(f"Unknown tool {name!r}. Available: {sorted(T.TOOLS)}")
    args = dict(args)
    if name == "clean_data" and "value_map_json" in args:
        raw = args.pop("value_map_json")
        args["value_map"] = json.loads(raw) if raw else None
    return T.TOOLS[name](**args)


def _call_tool(name: str, raw_args: str, approve) -> str:
    """Run one tool call and return a JSON string. Errors go back to the LLM so it can self-correct."""
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Arguments were not valid JSON: {e}"})
    if name in NEEDS_APPROVAL and not approve(name, args):
        return json.dumps({"error": "The user declined this action. Do not retry; continue without it."})
    try:
        out = json.dumps(_dispatch(name, args), default=str)
    except Exception as e:                                   # noqa: BLE001 - we want to report any failure
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    if name == "clean_data" and '"error"' not in out[:20]:
        _hint_cache.clear()                                  # tables changed: rebuild the schema hint next time
    if len(out) > MAX_TOOL_CHARS:
        out = out[:MAX_TOOL_CHARS] + '... [TRUNCATED - aggregate in SQL or add a LIMIT]'
    return out


# --------------------------------------------------------------------------- #
# LLM call with visible retries
# --------------------------------------------------------------------------- #
def _retry_wait(error_text: str, attempt: int) -> float:
    """Groq's 429 message says e.g. 'Please try again in 7.5s' - wait that long (max 60s)."""
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", error_text)
    if m:
        return min(int(m.group(1) or 0) * 60 + float(m.group(2)) + 1, 60)
    return 2 ** (attempt + 1)


def _chat(client, progress=None, **kwargs):
    """One LLM call. Retries (and says so) on: rate limits (429), timeouts, Groq's 'tool_use_failed'.
    If the provider rejects reasoning_effort, it is dropped and the call repeated."""
    for attempt in range(4):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:                               # noqa: BLE001
            msg, last = str(e), attempt == 3
            name = e.__class__.__name__
            if "reasoning_effort" in kwargs and "reasoning_effort" in msg:
                kwargs.pop("reasoning_effort")
                _say(progress, "  provider does not accept reasoning_effort - retrying without it")
                continue
            if "tool_use_failed" in msg and not last:
                kwargs["temperature"] = 0.3                  # a small nudge usually fixes the format
                _say(progress, "  model wrote a malformed tool call - retrying")
                continue
            if ("429" in msg or name == "RateLimitError") and not last:
                wait = _retry_wait(msg, attempt)
                _say(progress, f"  rate limit hit - waiting {wait:.0f}s (free tiers are limited)")
                time.sleep(wait)
                continue
            if ("Timeout" in name or "timed out" in msg) and not last:
                _say(progress, "  request timed out - retrying")
                continue
            raise


def _say(progress, text: str):
    if progress:
        progress(text)


def _trim(history: list) -> list:
    """Keep the last few user turns, always cutting at a user message so tool calls are never orphaned."""
    user_idx = [i for i, m in enumerate(history) if m["role"] == "user"]
    if len(user_idx) <= MAX_HISTORY_TURNS:
        return history
    return history[user_idx[-MAX_HISTORY_TURNS]:]


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #
def run_agent(question: str, history: list = None, client=None, approve=cli_approve,
              max_steps: int = MAX_STEPS, progress=None) -> dict:
    """Answer one question. Returns {"answer", "steps", "history"} (pass history back in for follow-ups).
    progress: optional function(str) that receives live status lines (the CLI passes print)."""
    client = client or get_client()
    model = _cfg()[2]
    history = _trim(list(history or []))
    new_turn = [{"role": "user", "content": question}]
    system = SYSTEM_PROMPT.format(table=T.DEFAULT_TABLE, schema=schema_hint())
    messages = [{"role": "system", "content": system}] + history + new_turn
    steps = []

    for _ in range(max_steps):
        _say(progress, "  thinking...")
        t0 = time.perf_counter()
        resp = _chat(client, progress, model=model, messages=messages, tools=TOOL_SCHEMAS,
                     tool_choice="auto", temperature=0, **_effort_kwargs())
        _say(progress, f"  LLM replied in {time.perf_counter() - t0:.1f}s")
        msg = resp.choices[0].message
        assistant = _assistant_message(msg)
        messages.append(assistant)
        new_turn.append(assistant)

        if not msg.tool_calls:
            return {"answer": msg.content or "", "steps": steps, "history": history + new_turn}

        for call in msg.tool_calls:
            name, raw = call.function.name, call.function.arguments
            _say(progress, f"  running {name}({(raw or '')[:150]})")
            t1 = time.perf_counter()
            result = _call_tool(name, raw, approve)
            _say(progress, f"  {name} finished in {time.perf_counter() - t1:.1f}s")
            steps.append({"tool": name, "args": raw, "result": result[:300]})
            tool_msg = {"role": "tool", "tool_call_id": call.id, "content": result}
            messages.append(tool_msg)
            new_turn.append(tool_msg)

    answer = "I could not finish within the step limit. Try a narrower question."
    new_turn.append({"role": "assistant", "content": answer})
    return {"answer": answer, "steps": steps, "history": history + new_turn}


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def ping():
    """Tiny request that checks key, model and speed - no database, no tools."""
    key, url, model = _cfg()
    print(f"Provider: {url}\nModel:    {model}")
    client = get_client()
    t0 = time.perf_counter()
    try:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with the single word OK"}],
            **_effort_kwargs())
    except Exception as e:                                   # noqa: BLE001
        print(f"FAILED after {time.perf_counter() - t0:.1f}s: {type(e).__name__}: {e}")
        return
    print(f"Reply: {r.choices[0].message.content!r} in {time.perf_counter() - t0:.1f}s")


def main():
    if "--ping" in sys.argv:
        return ping()
    print(f"Data agent ready (model: {_cfg()[2]}). Type 'exit' to quit.\n")
    client, history = get_client(), []
    while True:
        q = input("You: ").strip()
        if q.lower() in {"exit", "quit", ""}:
            break
        t0 = time.perf_counter()
        try:
            out = run_agent(q, history, client, progress=print)
        except Exception as e:                               # noqa: BLE001
            print(f"\nError: {type(e).__name__}: {e}\n")
            continue
        history = out["history"]
        print(f"\nAgent: {out['answer']}\n(total {time.perf_counter() - t0:.1f}s)\n")


if __name__ == "__main__":
    main()