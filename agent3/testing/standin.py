"""Scripted stand-ins for the LLM, so the runner can be tested without an API key.

- ProcedureStandIn follows SKILL.md step by step through the REAL PowerMCP tools (via
  the runner's stateful MCP session) and submits a real result. It tests the plumbing
  end to end: server launch, one session keeping the circuit loaded, tool calls,
  validation and the result wrapper. It is NOT a test of how well an LLM follows the
  procedure. Only a live run tests that.
- NeverSubmits, AlwaysInvalid, InvalidThenValid and Looper exercise the loop rules.
"""

from __future__ import annotations

import itertools
import json
from typing import Generator

from langchain_core.messages import AIMessage, ToolMessage

from models import PROCEDURE_VERSION, Limits
from testing import compare as C

_ids = itertools.count(1)


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def _payload(msg: ToolMessage):
    content = msg.content
    if isinstance(content, list):
        content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {"_raw": content}
    return data.get("payload", data) if isinstance(data, dict) else data


class _GeneratorStandIn:
    """Drives a generator that yields tool calls and receives the parsed tool results."""

    model_label = "scripted-standin"

    def __init__(self):
        self.gen = None
        self.tools = []

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def script(self, request) -> Generator:  # override
        raise NotImplementedError

    async def ainvoke(self, messages):
        if self.gen is None:
            from models import GridRequest
            req_json = messages[1].content.split("GridRequest:\n", 1)[1]
            self.gen = self.script(GridRequest.model_validate_json(req_json))
            call = next(self.gen)
        else:
            last = messages[-1]
            sent = _payload(last) if isinstance(last, ToolMessage) else {"_human": last.content}
            try:
                call = self.gen.send(sent)
            except StopIteration:
                return AIMessage(content="I am done.")
        if call is None:
            return AIMessage(content="I am done.")
        return AIMessage(content="", tool_calls=[call])


class ProcedureStandIn(_GeneratorStandIn):
    def script(self, req):
        lim: Limits = req.limits
        facts = {"procedure_version": PROCEDURE_VERSION, "recovery_actions": []}
        caveats = []

        def fail(status, stage, reason, **extra):
            return _call("submit_result", status=status, failed_stage=stage, reason=reason,
                         run_facts=facts, caveats=caveats, **extra)

        # 1. Load the model
        yield _call("compile_opendss_file", dss_file=req.model_file, force_recompile=True)
        summ = yield _call("get_model_summary_records")
        if not summ.get("buses"):
            yield fail("model_not_found", "compile", "model summary shows 0 buses")
            return
        if summ.get("line min norm amps") == summ.get("line max norm amps"):
            caveats.append({"code": "placeholder_line_ratings",
                            "detail": f"All lines have norm amps {summ.get('line max norm amps')} (min = max)."})

        # 2. Check the bus
        buses = yield _call("get_buses_records")
        names = [n.lower() for n in buses["name"]]
        if req.bus.lower() not in names:
            yield fail("bus_not_found", "bus_check", f"bus '{req.bus}' not in get_buses_records")
            return
        i = names.index(req.bus.lower())
        bus = buses["name"][i]
        nodes = buses["nodes"][i]
        facts.update(bus_nodes=nodes, bus_kv_base_ln=buses["kv_base"][i])
        if not {1, 2, 3} <= set(nodes):
            yield fail("phase_mismatch", "bus_check", f"bus '{bus}' has nodes {nodes}; a 3-phase charger needs 1, 2, 3")
            return
        kv = C.charger_kv_ll(buses["kv_base"][i])
        facts["charger_kv_ll"] = kv
        regs = (yield _call("get_regcontrols_records"))["transformer"]
        pd = yield _call("get_pd_elements_records")
        caps = [n.split(".", 1)[1] for n in pd["name"] if n.lower().startswith("capacitor.")]

        # 3-4. Baseline
        s = yield _call("solve_snapshot")
        facts.update(baseline_converged=s["converged"], baseline_max_iterations=15)
        b0, viol0, ln0 = yield from self._record(bus, regs, caps, lim)

        # 5-6. Add and confirm the charger
        name = req.charger.name or f"ev_charger_{bus}"
        exists = yield _call("is_element_in_model", element_class="load", element_name=name)
        if exists["in_model"]:
            name += "_2"
            caveats.append({"code": "charger_name_changed", "detail": f"name taken; used {name}"})
        facts["charger_name"] = name
        yield _call("add_element", element_class="load", element_name=name,
                    properties={"bus1": f"{bus}.1.2.3", "phases": 3, "kV": kv, "kW": req.charger.kw,
                                "pf": req.charger.power_factor, "model": 1, "conn": "wye"})
        present = yield _call("is_element_in_model", element_class="load", element_name=name)
        data = yield _call("get_element_data", element_class="load", element_name=name)
        facts["charger_confirmed_in_model"] = bool(present["in_model"]) and abs(float(data["kw"][0]) - req.charger.kw) < 1e-6

        # 7-8. With charger
        s = yield _call("solve_snapshot")
        facts.update(charger_converged=s["converged"], charger_max_iterations=15)
        b1, viol1, ln1 = yield from self._record(bus, regs, caps, lim)

        # 9-10. Compare and submit
        comp = C.compare(b0, b1, viol0, viol1, ln0, ln1, lim)
        caveats.append({"code": "no_service_transformer", "detail": f"Charger modelled directly on bus {bus}."})
        caveats.append({"code": "assumed_power_factor", "detail": f"Charger power factor assumed {req.charger.power_factor}."})
        yield _call("submit_result", status="ok", run_facts=facts, caveats=caveats,
                    baseline=b0.model_dump(), with_charger=b1.model_dump(), comparison=comp.model_dump())

    def _record(self, bus, regs, caps, lim):
        summary = yield _call("get_results_summary_records")
        vv = yield _call("get_violation_voltage_ln_nodes_records", v_min_pu=lim.v_min_pu, v_max_pu=lim.v_max_pu)
        iv = yield _call("get_violation_currents_elements_records", current_limit_type="norm_amps")
        lp = yield _call("get_current_loading_percent_records", current_limit_type="norm_amps")
        ln = yield _call("get_voltage_mag_ln_nodes_records")
        tr = yield _call("get_transformers_records")
        cap_states = {}
        for c in caps:
            d = yield _call("get_element_data", element_class="capacitor", element_name=c)
            cap_states[c] = C.parse_states(d["states"][0])
        volt = C.voltage_violations(vv["records"], lim)
        therm = C.thermal_violations(iv["records"].get("index", []), lp["records"], lim)
        taps = {r: float(t) for r, t in zip(tr["name"], tr["tap"]) if r in regs}
        snap = C.snapshot(summary, volt, therm, ln, bus, taps, cap_states)
        return snap, {**volt, **therm}, ln


class NeverSubmits:
    model_label = "scripted-standin"
    """Answers in text only: should get one reminder, then a failure result."""
    def __init__(self): self.calls = 0
    def bind_tools(self, tools): return self
    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content="The feeder looks fine to me.")


class AlwaysInvalid:
    model_label = "scripted-standin"
    """Submits a broken result every time: should fail after the attempt limit."""
    def __init__(self): self.calls = 0
    def bind_tools(self, tools): return self
    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content="", tool_calls=[_call("submit_result", status="ok", run_facts={"procedure_version": "0.1.0"})])


class InvalidThenValid:
    model_label = "scripted-standin"
    """First submission invalid, second a valid failure report: should be accepted on attempt 2."""
    def __init__(self): self.calls = 0
    def bind_tools(self, tools): return self
    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="", tool_calls=[_call("submit_result", status="bus_not_found", run_facts={"procedure_version": "0.1.0"})])
        return AIMessage(content="", tool_calls=[_call("submit_result", status="bus_not_found", failed_stage="bus_check",
                                                       reason="bus 'x' not found", run_facts={"procedure_version": "0.1.0"})])


class Looper:
    model_label = "scripted-standin"
    """Keeps calling a harmless tool forever: should hit the tool-call cap."""
    def __init__(self): self.calls = 0
    def bind_tools(self, tools): return self
    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content="", tool_calls=[_call("get_model_summary_records")])
