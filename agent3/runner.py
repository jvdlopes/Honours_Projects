"""Agent 3 runner: the plain Python loop around the LLM and the PowerMCP OpenDSS tools.

How it works:
- SKILL.md is the system prompt; the GridRequest is the first message.
- Provider-neutral LLM via LangChain `init_chat_model` (model string from config).
- PowerMCP via `langchain-mcp-adapters` with ONE stateful session per run (the
  adapter is stateless by default, which would lose the loaded circuit between calls).
- The run ends when the LLM calls `submit_result`, whose arguments are validated
  against `AgentSubmission`. Invalid → errors go back to the LLM.
- One shared attempt counter (3) for failed submissions.
- One reminder if the LLM stops without submitting, then a failure result.
- Hard cap of 40 tool calls (submit_result included) → `step_limit_reached`.
- Preflight before any LLM call: the model file and the files it redirects to
  exist and sit under the allowed roots; the server command exists; the expected
  tools loaded.

Not in this version: the wrapper (artifacts, summaries, enforcing the
don't-change-the-grid rule). Tool calls are only logged, so tests can check them.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from config import Settings, load_settings
from models import PROCEDURE_VERSION, AgentSubmission, GridRequest, GridResult, RunFacts

SUBMIT_TOOL = "submit_result"

# Tools SKILL.md relies on. Preflight fails if any are missing from the server.
REQUIRED_TOOLS = {
    "compile_opendss_file", "get_model_summary_records", "get_buses_records",
    "get_regcontrols_records", "get_pd_elements_records", "solve_snapshot",
    "get_results_summary_records", "get_violation_voltage_ln_nodes_records",
    "get_violation_currents_elements_records", "get_current_loading_percent_records",
    "get_voltage_mag_ln_nodes_records", "get_transformers_records", "get_element_data",
    "is_element_in_model", "add_element", "edit_element",
}

REMINDER = (
    "You stopped without calling submit_result. The run only ends when you call "
    "submit_result with the full result (or a failure status). Call it now."
)


# --------------------------------------------------------------------------
# Run record (what tests and logs look at)
# --------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    n: int
    name: str
    args: dict
    ok: bool
    result_chars: int
    note: str = ""


@dataclass
class AgentRun:
    result: GridResult
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    run_dir: Optional[Path] = None
    reminded: bool = False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _inline_refs(schema: dict) -> dict:
    """Inline $defs/$ref so the tool schema is plain JSON Schema for any provider."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
                rest = {k: v for k, v in node.items() if k != "$ref"}
                return walk({**target, **rest})
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def submit_tool_spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_TOOL,
            "description": (
                "Finish the run. Call exactly once with the full result, or with a failure status, "
                "failed_stage and reason. If it returns validation errors, fix exactly those and call again."
            ),
            "parameters": _inline_refs(AgentSubmission.model_json_schema()),
        },
    }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _redirected_files(master: Path) -> list[Path]:
    """Files a .dss master pulls in with Redirect/Compile (one level, comments ignored)."""
    found = []
    try:
        text = master.read_text(errors="ignore")
    except OSError:
        return found
    for line in text.splitlines():
        line = line.split("!")[0].split("//")[0].strip()
        m = re.match(r"(?i)^(redirect|compile)\s+\(?\s*\"?([^\")]+)\"?\s*\)?", line)
        if m:
            found.append((master.parent / m.group(2).strip()).resolve())
    return found


def _under_roots(path: Path, roots: list[str]) -> bool:
    if not roots:
        return True  # server decides; with no roots it confines to its start directory
    for r in roots:
        try:
            path.resolve().relative_to(Path(r).resolve())
            return True
        except ValueError:
            continue
    return False


def preflight_model(request: GridRequest, settings: Settings) -> Optional[str]:
    """Return a reason string if the model can't be run, else None. No LLM involved."""
    master = Path(request.model_file)
    if not master.is_file():
        return f"model file not found: {master}"
    if not _under_roots(master, settings.allowed_roots):
        return f"model file is outside the allowed roots {settings.allowed_roots}: {master}"
    missing = [str(p) for p in _redirected_files(master) if not p.is_file()]
    if missing:
        return "files referenced by the model are missing: " + ", ".join(missing)
    return None


def server_command_problem(settings: Settings) -> Optional[str]:
    """Check the PowerMCP server command and script exist before launching anything."""
    import shutil
    cmd = settings.mcp_command
    if not (shutil.which(cmd) or Path(cmd).is_file()):
        return f"MCP server command not found: {cmd!r}. Check AGENT3_MCP_COMMAND in .env."
    for a in settings.mcp_args:
        if a.lower().endswith(".py") and not Path(a).is_file():
            return f"MCP server script not found: {a!r}. Check AGENT3_MCP_ARGS in .env."
    return None


def _failure(request, status, stage, reason, settings, n_calls=0, n_submits=0) -> GridResult:
    return GridResult(
        status=status, failed_stage=stage, reason=reason,
        run_facts=RunFacts(procedure_version=PROCEDURE_VERSION),
        request=request, tool_calls_used=n_calls, submission_attempts=n_submits,
        llm_model=settings.llm_model,
    )


def _first_message(request: GridRequest) -> str:
    return (
        "Run the EV charger grid study for this GridRequest, following your procedure exactly. "
        "Finish by calling submit_result.\n\nGridRequest:\n" + request.model_dump_json(indent=2)
    )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


async def run_loop(request: GridRequest, llm, tools: list, settings: Settings, log) -> AgentRun:
    """Plain loop: model -> tools -> model, with the submit/attempt/reminder/cap rules.

    `llm` is any LangChain chat model (or a test stand-in) with .bind_tools().ainvoke().
    `tools` are LangChain tools (the PowerMCP tools from the session).
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    tools_by_name = {t.name: t for t in tools}
    model_label = getattr(llm, "model_label", settings.llm_model)  # stand-ins identify themselves
    settings = settings.model_copy(update={"llm_model": model_label})
    model = llm.bind_tools(list(tools) + [submit_tool_spec()])
    messages = [
        SystemMessage(content=settings.skill_path.read_text(encoding="utf-8")),
        HumanMessage(content=_first_message(request)),
    ]
    run = AgentRun(result=None)  # type: ignore[arg-type]
    n_calls = n_submits = failed_submits = 0

    def finish(result: GridResult) -> AgentRun:
        run.result = result
        log("result", result.model_dump(mode="json"))
        return run

    while True:
        try:
            ai = await model.ainvoke(messages)
        except Exception as e:  # bad key, no credits, network, rate limit...
            log("llm_error", repr(e))
            return finish(_failure(request, "unexpected_error", "agent_loop",
                                   f"LLM call failed: {type(e).__name__}: {str(e)[:300]}",
                                   settings, n_calls, n_submits))
        messages.append(ai)
        log("ai", {"text": _content_text(ai.content)[:2000], "tool_calls": ai.tool_calls})

        if not ai.tool_calls:
            if not run.reminded:
                run.reminded = True
                messages.append(HumanMessage(content=REMINDER))
                log("reminder", REMINDER)
                continue
            return finish(_failure(request, "unexpected_error", "agent_loop",
                                   "agent stopped without calling submit_result after one reminder",
                                   settings, n_calls, n_submits))

        for tc in ai.tool_calls:
            n_calls += 1
            if n_calls > settings.max_tool_calls:
                return finish(_failure(request, "step_limit_reached", "agent_loop",
                                       f"tool-call cap of {settings.max_tool_calls} reached",
                                       settings, n_calls - 1, n_submits))

            if tc["name"] == SUBMIT_TOOL:
                n_submits += 1
                try:
                    sub = AgentSubmission.model_validate(tc["args"])
                except ValidationError as e:
                    failed_submits += 1
                    errors = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
                    run.tool_calls.append(ToolCallRecord(n_calls, SUBMIT_TOOL, {}, False, 0, "invalid"))
                    log("submit_invalid", errors)
                    if failed_submits >= settings.max_attempts:
                        return finish(_failure(request, "unexpected_error", "submission",
                                               f"submission failed validation {failed_submits} times; last errors: {errors[:500]}",
                                               settings, n_calls, n_submits))
                    messages.append(ToolMessage(
                        content=f"Validation failed ({failed_submits}/{settings.max_attempts}). Fix exactly these and call submit_result again: {errors}",
                        tool_call_id=tc["id"], name=SUBMIT_TOOL))
                    continue
                run.tool_calls.append(ToolCallRecord(n_calls, SUBMIT_TOOL, {}, True, 0, "accepted"))
                return finish(GridResult(**sub.model_dump(), request=request, tool_calls_used=n_calls,
                                         submission_attempts=n_submits, llm_model=settings.llm_model))

            tool = tools_by_name.get(tc["name"])
            if tool is None:
                text, ok = f"Unknown tool '{tc['name']}'.", False
                messages.append(ToolMessage(content=text, tool_call_id=tc["id"], name=tc["name"]))
            else:
                try:
                    out = await tool.ainvoke({**tc, "type": "tool_call"})
                    text = _content_text(getattr(out, "content", out))
                    ok = '"success": false' not in text[:200]
                    messages.append(out if isinstance(out, ToolMessage)
                                    else ToolMessage(content=text, tool_call_id=tc["id"], name=tc["name"]))
                except Exception as e:  # tool transport errors etc.
                    text, ok = f"Tool error: {e}", False
                    messages.append(ToolMessage(content=text, tool_call_id=tc["id"], name=tc["name"]))
            run.tool_calls.append(ToolCallRecord(n_calls, tc["name"], tc.get("args", {}), ok, len(text)))
            log("tool", {"n": n_calls, "name": tc["name"], "args": tc.get("args", {}), "ok": ok,
                         "result": text[:4000], "result_chars": len(text)})


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def run_grid_agent(request: GridRequest, settings: Optional[Settings] = None, llm=None) -> AgentRun:
    """Run Agent 3 for one request. Starts its own PowerMCP OpenDSS server and session."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    settings = settings or load_settings()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    run_dir = Path(settings.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = (run_dir / "transcript.jsonl").open("a", encoding="utf-8")

    def log(kind: str, data: Any) -> None:
        log_file.write(json.dumps({"kind": kind, "data": data}, default=str) + "\n")
        log_file.flush()

    log("request", request.model_dump(mode="json"))
    try:
        reason = preflight_model(request, settings)
        if reason:
            log("preflight", reason)
            return AgentRun(_failure(request, "model_not_found", "compile", reason, settings), run_dir=run_dir)

        bad = server_command_problem(settings)
        if bad:
            log("preflight", bad)
            return AgentRun(_failure(request, "unexpected_error", "agent_loop", bad, settings), run_dir=run_dir)
        command, args = settings.mcp_command, list(settings.mcp_args)
        if settings.filter_server_stdout:  # OpenDSS prints plain text on the MCP channel
            command, args = sys.executable, [str(Path(__file__).with_name("mcp_stdio_filter.py")), command, *args]
        client = MultiServerMCPClient({
            "opendss": {"transport": "stdio", "command": command, "args": args, "env": settings.server_env()}
        })
        async with client.session("opendss") as session:  # ONE stateful session for the whole run
            tools = await load_mcp_tools(session)
            missing = REQUIRED_TOOLS - {t.name for t in tools}
            if missing:
                reason = f"PowerMCP server is missing tools: {sorted(missing)}"
                log("preflight", reason)
                return AgentRun(_failure(request, "unexpected_error", "agent_loop", reason, settings), run_dir=run_dir)
            if llm is None:
                from langchain.chat_models import init_chat_model
                llm = init_chat_model(settings.llm_model, max_tokens=settings.llm_max_tokens)
            run = await run_loop(request, llm, tools, settings, log)
            run.run_dir = run_dir
            return run
    finally:
        log_file.close()


def run_sync(request: GridRequest, settings: Optional[Settings] = None, llm=None) -> AgentRun:
    return asyncio.run(run_grid_agent(request, settings, llm))
