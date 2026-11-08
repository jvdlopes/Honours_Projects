"""Offline tests for the runner rules, the preflight, the checklist itself and the
Anthropic request format. No API key or LLM call needed.

    python -m pytest testing/test_runner.py -q
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from config import load_settings  # noqa: E402
from models import ChargerSpec, GridRequest  # noqa: E402
from runner import AgentRun, ToolCallRecord, preflight_model, run_grid_agent, run_loop, submit_tool_spec  # noqa: E402
from testing import checklist as CL  # noqa: E402
from testing.standin import AlwaysInvalid, InvalidThenValid, Looper, NeverSubmits, ProcedureStandIn  # noqa: E402

S = load_settings()
FEEDER = str(S.test_feeder) if S.test_feeder else None
needs_feeder = pytest.mark.skipif(not FEEDER or not Path(FEEDER).is_file(), reason="AGENT3_TEST_FEEDER not set")


def req(bus="60", kw=600.0, model_file=None):
    return GridRequest(model_file=model_file or FEEDER or "x.dss", bus=bus, charger=ChargerSpec(kw=kw))


def loop(llm, tools=()):
    log = []
    run = asyncio.run(run_loop(req(), llm, list(tools), S, lambda k, d: log.append((k, d))))
    return run, log


# ---------------------------------------------------------------- loop rules


def test_never_submits_gets_one_reminder_then_fails():
    llm = NeverSubmits()
    run, log = loop(llm)
    assert run.reminded and llm.calls == 2
    assert run.result.status == "unexpected_error" and run.result.failed_stage == "agent_loop"
    assert sum(1 for k, _ in log if k == "reminder") == 1


def test_invalid_submissions_stop_at_the_attempt_limit():
    llm = AlwaysInvalid()
    run, _ = loop(llm)
    assert llm.calls == S.max_attempts == 3
    assert run.result.status == "unexpected_error" and run.result.failed_stage == "submission"
    assert run.result.submission_attempts == 3


def test_invalid_then_valid_is_accepted_and_counted():
    run, log = loop(InvalidThenValid())
    assert run.result.status == "bus_not_found" and run.result.submission_attempts == 2
    assert any(k == "submit_invalid" for k, _ in log)


def test_tool_call_cap():
    class FakeTool:
        name = "get_model_summary_records"
        async def ainvoke(self, call):
            return '{"success": true, "payload": {"buses": 1}}'
    run, _ = loop(Looper(), [FakeTool()])
    assert run.result.status == "step_limit_reached"
    assert run.result.tool_calls_used == S.max_tool_calls == 40


def test_runner_fills_request_and_counts_not_the_llm():
    run, _ = loop(InvalidThenValid())
    assert run.result.request.bus == "60" and run.result.llm_model == "scripted-standin"


# ---------------------------------------------------------------- preflight


def test_preflight_missing_file(tmp_path):
    assert "not found" in preflight_model(req(model_file=str(tmp_path / "nope.dss")), S)


def test_preflight_missing_redirect(tmp_path):
    master = tmp_path / "m.dss"
    master.write_text("Clear\nRedirect  Lines.dss  ! comment\nsolve\n")
    s = load_settings(allowed_roots=[str(tmp_path)])
    assert "Lines.dss" in preflight_model(req(model_file=str(master)), s)
    (tmp_path / "Lines.dss").write_text("")
    assert preflight_model(req(model_file=str(master)), s) is None


def test_preflight_outside_roots(tmp_path):
    master = tmp_path / "m.dss"
    master.write_text("Clear\n")
    s = load_settings(allowed_roots=[str(tmp_path / "elsewhere")])
    assert "outside the allowed roots" in preflight_model(req(model_file=str(master)), s)


@needs_feeder
def test_preflight_passes_on_123_bus():
    assert preflight_model(req(), S) is None


# ---------------------------------------------------------------- submit tool + provider format


def test_submit_schema_has_no_refs():
    assert "$ref" not in json.dumps(submit_tool_spec())


def test_anthropic_request_payload_builds(monkeypatch):
    """Format a real conversation (stand-in run through PowerMCP) exactly as LangChain would send it to Anthropic."""
    if not FEEDER:
        pytest.skip("AGENT3_TEST_FEEDER not set")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    from langchain_anthropic import ChatAnthropic

    captured = {}

    class Capturing(ProcedureStandIn):
        async def ainvoke(self, messages):
            captured["messages"] = list(messages)
            captured["tools"] = self.tools
            return await super().ainvoke(messages)

    run = asyncio.run(run_grid_agent(req(), S, llm=Capturing()))
    assert run.result.status == "ok"
    from langchain_anthropic.chat_models import convert_to_anthropic_tool
    model = ChatAnthropic(model="claude-sonnet-5-5", max_tokens=S.llm_max_tokens)
    tools = [convert_to_anthropic_tool(t) for t in captured["tools"]]
    body = model._get_request_payload(captured["messages"], tools=tools)
    assert body["system"] and len(body["messages"]) > 10
    names = {t["name"] for t in body["tools"]}
    assert "submit_result" in names and "solve_snapshot" in names
    # every tool_use the model made is answered by a tool_result in the next user turn
    uses = [b["id"] for m in body["messages"] if m["role"] == "assistant" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_use"]
    results = {b["tool_use_id"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"}
    assert set(uses[:-1]) <= results  # the last one is the accepted submit_result


# ---------------------------------------------------------------- the checklist itself catches problems


@pytest.fixture(scope="module")
def good_run():
    if not FEEDER:
        pytest.skip("AGENT3_TEST_FEEDER not set")
    return asyncio.run(run_grid_agent(req(), S, llm=ProcedureStandIn()))


def _fails(rows, cid):
    return [r for r in rows if r[0] == cid and not r[2]]


def test_checklist_passes_good_run(good_run):
    rows = CL.check_ok_case("ok_600", good_run)
    assert all(r[2] for r in rows), [r for r in rows if not r[2]]


def test_checklist_catches_wrong_number(good_run):
    bad = copy.deepcopy(good_run)
    bad.result.baseline.feeder_kw += 10
    assert _fails(CL.check_ok_case("ok_600", bad), "K02")


def test_checklist_catches_wrong_label(good_run):
    bad = copy.deepcopy(good_run)
    row = next(r for r in bad.result.comparison.violation_table if r.label == "new")
    object.__setattr__(row, "label", "made_worse")
    assert _fails(CL.check_ok_case("ok_600", bad), "K07")


def test_checklist_catches_missing_caveat(good_run):
    bad = copy.deepcopy(good_run)
    bad.result.caveats = [c for c in bad.result.caveats if c.code != "placeholder_line_ratings"]
    assert _fails(CL.check_ok_case("ok_600", bad), "K09")


def test_checklist_catches_grid_change(good_run):
    bad = copy.deepcopy(good_run)
    bad.tool_calls.append(ToolCallRecord(99, "edit_element", {"element_class": "line", "element_name": "l115"}, True, 10))
    bad.tool_calls.append(ToolCallRecord(100, "disable_elements_by_type", {"element_type": "capacitor"}, True, 10))
    obs = _fails(CL.check_ok_case("ok_600", bad), "K10")
    assert obs and "line.l115" in obs[0][3] and "disable_elements_by_type" in obs[0][3]


def test_checklist_allows_fixing_the_charger(good_run):
    ok = copy.deepcopy(good_run)
    ok.tool_calls.append(ToolCallRecord(99, "edit_element", {"element_class": "load", "element_name": "ev_charger_60"}, True, 10))
    assert not _fails(CL.check_ok_case("ok_600", ok), "K10")


def test_checklist_catches_wrong_status_and_solve_before_bus_check(good_run):
    bad = copy.deepcopy(good_run)
    rows = CL.check_fail_case("bad_bus", bad)  # an ok run checked as if it were the bad-bus case
    assert _fails(rows, "F01") and _fails(rows, "F02") and _fails(rows, "F05")


def test_checklist_reset_catches_different_baselines(good_run):
    other = copy.deepcopy(good_run)
    other.result.baseline.losses_kw += 5
    assert _fails(CL.check_reset({"ok_600": good_run, "ok_3000": other}), "R01")


def test_llm_error_returns_failure_not_crash():
    class Broken:
        model_label = "scripted-standin"
        def bind_tools(self, tools): return self
        async def ainvoke(self, messages): raise RuntimeError("no credits")
    run, log = loop(Broken())
    assert run.result.status == "unexpected_error" and "no credits" in run.result.reason
    assert any(k == "llm_error" for k, _ in log)


def test_env_file_api_key_reaches_this_process_only(tmp_path, monkeypatch):
    """The key in .env must be visible to LangChain (os.environ of this process)."""
    import os
    import config
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    monkeypatch.setattr(config, "HERE", tmp_path)
    config.load_settings()
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-from-dotenv"


def test_bad_server_command_is_reported_clearly():
    s = load_settings(mcp_command="C:/nope/python.exe")
    run = asyncio.run(run_grid_agent(req(), s, llm=NeverSubmits()))
    assert run.result.status == "unexpected_error" and "AGENT3_MCP_COMMAND" in run.result.reason
    s = load_settings(mcp_args=["C:/nope/opendss_mcp.py"])
    run = asyncio.run(run_grid_agent(req(), s, llm=NeverSubmits()))
    assert "AGENT3_MCP_ARGS" in run.result.reason


def test_reset_check_skipped_when_only_one_ok_case_ran(good_run):
    assert CL.check_reset({"ok_600": good_run}) == []
