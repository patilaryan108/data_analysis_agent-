"""Tests the agent loop with a FAKE LLM, so no API key or internet is needed.
Run from the project root:  pytest -v"""
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
CSV = ROOT / "data" / "clean_data.csv"


# ---- a scripted stand-in for the LLM ------------------------------------------ #
class Msg:
    def __init__(self, content=None, calls=None):
        self.content = content
        self.tool_calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=a))
                           for i, (n, a) in enumerate(calls or [])] or None

    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in self.tool_calls]
        return {k: v for k, v in d.items() if v is not None} if exclude_none else d


class FakeClient:
    def __init__(self, script):
        self.script, self.seen, self.kw = list(script), [], []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.seen.append(kw["messages"][:])
        self.kw.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.script.pop(0))])


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    if not CSV.exists():
        pytest.skip("run src/clean_data.py first")
    db = tmp_path_factory.mktemp("db") / "agent.db"
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "GROQ_API_KEY", "GROQ_MODEL"):
        os.environ.pop(k, None)
    os.environ.update(DATABASE_URL=f"sqlite:///{db}", LLM_MODEL="fake-model")
    import tools, agent as a
    importlib.reload(tools); importlib.reload(a)
    tools._engines.clear()
    pd.read_csv(CSV).to_sql("blinkit_sales", tools.get_engine(), index=False)
    return a


def last_tool_result(client):
    return json.loads(next(m for m in reversed(client.seen[-1]) if m["role"] == "tool")["content"])


def test_agent_runs_sql_then_answers(agent):
    q = "SELECT outlet_type, ROUND(SUM(sales)) AS s FROM blinkit_sales GROUP BY outlet_type ORDER BY s DESC"
    fc = FakeClient([Msg(calls=[("run_sql", json.dumps({"query": q}))]),
                     Msg(content="Supermarket Type1 earns the most.")])
    out = agent.run_agent("Which outlet type earns the most?", client=fc)
    assert out["answer"].startswith("Supermarket Type1")
    assert out["steps"][0]["tool"] == "run_sql"
    assert last_tool_result(fc)["rows"][0][0] == "Supermarket Type1"


def test_forbidden_sql_error_goes_back_to_the_llm(agent):
    fc = FakeClient([Msg(calls=[("run_sql", json.dumps({"query": "DROP TABLE blinkit_sales"}))]),
                     Msg(content="Sorry, I can only read data.")])
    out = agent.run_agent("delete everything", client=fc)
    assert "error" in last_tool_result(fc)
    assert out["answer"].startswith("Sorry")
    import tools
    assert tools.run_sql("SELECT COUNT(*) FROM blinkit_sales")["rows"][0][0] == 8523   # table still there


def test_clean_data_needs_approval_and_can_be_declined(agent):
    fc = FakeClient([Msg(calls=[("clean_data", json.dumps({"new_table": "should_not_exist"}))]),
                     Msg(content="Okay, I will leave the data as is.")])
    agent.run_agent("clean it", client=fc, approve=lambda n, a: False)
    assert "declined" in last_tool_result(fc)["error"]
    import tools
    from sqlalchemy import inspect
    assert "should_not_exist" not in inspect(tools.get_engine()).get_table_names()


def test_clean_data_runs_when_approved(agent):
    fc = FakeClient([Msg(calls=[("clean_data", json.dumps({"new_table": "approved_copy"}))]),
                     Msg(content="Done.")])
    agent.run_agent("clean it", client=fc, approve=lambda n, a: True)
    assert last_tool_result(fc)["new_table"] == "approved_copy"


def test_bad_json_arguments_do_not_crash(agent):
    fc = FakeClient([Msg(calls=[("run_sql", "{not json")]), Msg(content="Retrying.")])
    agent.run_agent("x", client=fc)
    assert "not valid JSON" in last_tool_result(fc)["error"]


def test_unknown_tool_is_reported(agent):
    fc = FakeClient([Msg(calls=[("hack_the_planet", "{}")]), Msg(content="ok")])
    agent.run_agent("x", client=fc)
    assert "Unknown tool" in last_tool_result(fc)["error"]


def test_step_limit_stops_endless_tool_loops(agent):
    loop = [Msg(calls=[("run_sql", json.dumps({"query": "SELECT 1"}))]) for _ in range(20)]
    out = agent.run_agent("loop", client=FakeClient(loop), max_steps=3)
    assert "step limit" in out["answer"] and len(out["steps"]) == 3


def test_follow_up_keeps_history_and_make_chart_works(agent, tmp_path):
    import tools
    tools.CHART_DIR = tmp_path
    fc = FakeClient([Msg(calls=[("make_chart", json.dumps({"chart_type": "bar", "x": "outlet_type", "y": "sales"}))]),
                     Msg(content="Chart saved."), Msg(content="Same chart, yes.")])
    first = agent.run_agent("chart sales by outlet type", client=fc)
    assert Path(last_tool_result(fc)["chart_file"]).exists()
    agent.run_agent("is that sum?", history=first["history"], client=fc)
    assert any(m["role"] == "user" and "chart sales" in m["content"] for m in fc.seen[-1])


def test_history_trim_never_starts_with_orphan_tool_message(agent):
    hist = []
    for i in range(10):
        hist += [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "tool_calls": [{"id": "a", "type": "function",
                  "function": {"name": "run_sql", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "a", "content": "{}"},
                 {"role": "assistant", "content": "a"}]
    trimmed = agent._trim(hist)
    assert trimmed[0]["role"] == "user" and sum(m["role"] == "user" for m in trimmed) == agent.MAX_HISTORY_TURNS


# ---- Groq-specific behaviour --------------------------------------------------- #
def test_groq_settings_are_picked_up(agent, monkeypatch):
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GROQ_MODEL", "some-groq-model")
    key, url, model = agent._cfg()
    assert (key, url, model) == ("gsk_test", "https://api.groq.com/openai/v1", "some-groq-model")
    assert agent.get_client().base_url.host == "api.groq.com"


def test_llm_settings_override_groq_settings(agent, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("LLM_API_KEY", "other")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    assert agent._cfg() == ("other", "http://localhost:11434/v1", "llama3.1")


def test_extra_fields_are_stripped_for_groq_but_kept_for_gemini(agent, monkeypatch):
    class M(Msg):
        def model_dump(self, exclude_none=True):
            d = super().model_dump(exclude_none)
            d["reasoning"] = "secret"
            d["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "abc"}}
            return d
    m = M(calls=[("run_sql", "{}")])
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    groq_msg = agent._assistant_message(m)
    assert "reasoning" not in groq_msg and "extra_content" not in groq_msg["tool_calls"][0]
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    gem_msg = agent._assistant_message(m)
    assert gem_msg["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "abc"


class FlakyClient(FakeClient):
    """Raises the given errors first, then behaves like FakeClient."""
    def __init__(self, errors, script):
        super().__init__(script)
        self.errors, self.temps = list(errors), []

    def _create(self, **kw):
        self.temps.append(kw.get("temperature"))
        if self.errors:
            raise Exception(self.errors.pop(0))
        return super()._create(**kw)


def test_tool_use_failed_is_retried_with_a_nudge(agent, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    fc = FlakyClient(["Error code: 400 - {'code': 'tool_use_failed'}"], [Msg(content="recovered")])
    out = agent.run_agent("hi", client=fc)
    assert out["answer"] == "recovered" and fc.temps == [0, 0.3]


def test_rate_limit_waits_the_time_groq_asks_for(agent, monkeypatch):
    waits = []
    monkeypatch.setattr(agent.time, "sleep", waits.append)
    fc = FlakyClient(["Error code: 429 - Rate limit reached. Please try again in 7.5s."], [Msg(content="ok")])
    assert agent.run_agent("hi", client=fc)["answer"] == "ok"
    assert waits == [8.5]
    assert agent._retry_wait("Please try again in 1m2.5s", 0) == 60      # capped at 60
    assert agent._retry_wait("some other 429", 1) == 4


def test_other_errors_are_not_swallowed(agent):
    fc = FlakyClient(["Error code: 401 - invalid api key"], [Msg(content="x")])
    with pytest.raises(Exception, match="401"):
        agent.run_agent("hi", client=fc)


# ---- speed / visibility fixes ---------------------------------------------------- #
def test_progress_lines_are_reported_live(agent):
    fc = FakeClient([Msg(calls=[("run_sql", json.dumps({"query": "SELECT 1"}))]), Msg(content="done")])
    lines = []
    agent.run_agent("x", client=fc, progress=lines.append)
    text = "\n".join(lines)
    assert "thinking" in text and "running run_sql" in text and "finished" in text and "LLM replied" in text


def test_schema_is_in_the_system_prompt_so_no_profile_round_trip_is_needed(agent):
    fc = FakeClient([Msg(content="hi")])
    agent.run_agent("x", client=fc)
    system = fc.seen[0][0]["content"]
    assert "outlet_type" in system and "Supermarket Type1" in system and "8523 rows" in system


def test_client_has_timeout_and_no_silent_sdk_retries(agent, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k"); monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("LLM_MODEL", "m"); monkeypatch.setenv("LLM_TIMEOUT", "45")
    c = agent.get_client()
    assert c.max_retries == 0 and c.timeout == 45


def test_gemini_gets_low_reasoning_effort_by_default(agent, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    fc = FakeClient([Msg(content="hi")])
    agent.run_agent("x", client=fc)
    assert fc.kw[0]["reasoning_effort"] == "low"
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    fc = FakeClient([Msg(content="hi")])
    agent.run_agent("x", client=fc)
    assert "reasoning_effort" not in fc.kw[0]


def test_reasoning_effort_is_dropped_if_provider_rejects_it(agent, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    fc = FlakyClient(["Error code: 400 - Unknown parameter: reasoning_effort"], [Msg(content="ok")])
    lines = []
    assert agent.run_agent("x", client=fc, progress=lines.append)["answer"] == "ok"
    assert fc.kw[-1].get("reasoning_effort") is None and any("reasoning_effort" in l for l in lines)


def test_rate_limit_wait_is_announced(agent, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    fc = FlakyClient(["Error code: 429 - quota exceeded"], [Msg(content="ok")])
    lines = []
    agent.run_agent("x", client=fc, progress=lines.append)
    assert any("rate limit" in l for l in lines)


def test_timeout_is_retried(agent):
    class APITimeoutError(Exception):
        pass
    fc = FakeClient([Msg(content="ok")])
    real = fc._create
    state = {"n": 0}
    def flaky(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise APITimeoutError("Request timed out.")
        return real(**kw)
    fc.chat.completions.create = flaky
    assert agent.run_agent("x", client=fc)["answer"] == "ok"


# ---- answer-quality rules found while checking real answers ----------------------- #
def test_prompt_contains_the_accuracy_rules(agent):
    fc = FakeClient([Msg(content="hi")])
    agent.run_agent("x", client=fc)
    system = fc.seen[0][0]["content"]
    for phrase in ("arithmetic in SQL", "OVER (", "currency", "transactions", "confounder"):
        assert phrase in system


def test_share_query_the_prompt_recommends_gives_the_right_number(agent):
    import tools
    r = tools.run_sql("SELECT outlet_type, ROUND(SUM(sales)*100.0/SUM(SUM(sales)) OVER (), 1) AS share, "
                      "COUNT(DISTINCT outlet_identifier) AS outlets FROM blinkit_sales "
                      "GROUP BY outlet_type ORDER BY share DESC")
    assert r["rows"][0] == ["Supermarket Type1", 65.5, 6]