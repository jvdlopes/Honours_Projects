"""Generate the answer key for the checklist, independently of the agent.

Calls the PowerMCP OpenDSS tool functions directly (in-process, no MCP, no LLM),
using exactly Agent 3's settings. Rerun it whenever the settings or the feeder
change. It must run with the Python that has PowerMCP's dependencies (mcp>=2), e.g.

    <powermcp-python> testing/reference.py --powermcp <path-to-PowerMCP> --feeder <IEEE123Master.dss>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from models import PROCEDURE_VERSION, ChargerSpec, GridRequest  # noqa: E402
from testing import compare as C  # noqa: E402

CASES = {"ok_600": ("60", 600.0), "ok_3000": ("60", 3000.0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--powermcp", required=True, help="Path to the PowerMCP repo root")
    ap.add_argument("--feeder", required=True, help="Path to IEEE123Master.dss")
    ap.add_argument("--out", default=str(HERE / "answer_key.json"))
    a = ap.parse_args()

    os.environ.setdefault("POWERIO_MCP_ALLOWED_ROOTS", str(Path(a.feeder).resolve().parent))
    sys.path[:0] = [str(Path(a.powermcp) / "OpenDSS"), a.powermcp]
    from core.engine import ensure_engine
    ensure_engine()
    from opendss_tools import configuration as CF, model as M, results as R, simulation as S

    P = lambda r: r["payload"]  # noqa: E731

    def record(bus, regs, caps, lim):
        summary = P(R.get_results_summary_records())
        vv = P(R.get_violation_voltage_ln_nodes_records(v_min_pu=lim.v_min_pu, v_max_pu=lim.v_max_pu))
        iv = P(R.get_violation_currents_elements_records(current_limit_type="norm_amps"))
        lp = P(R.get_current_loading_percent_records(current_limit_type="norm_amps"))
        ln = P(R.get_voltage_mag_ln_nodes_records())
        tr = P(M.get_transformers_records())
        caps_s = {c: C.parse_states(P(M.get_element_data("capacitor", c))["states"][0]) for c in caps}
        volt = C.voltage_violations(vv["records"], lim)
        therm = C.thermal_violations(iv["records"].get("index", []), lp["records"], lim)
        taps = {r: float(t) for r, t in zip(tr["name"], tr["tap"]) if r in regs}
        return C.snapshot(summary, volt, therm, ln, bus, taps, caps_s), {**volt, **therm}, ln

    key = {"generated": datetime.now(timezone.utc).isoformat(), "procedure_version": PROCEDURE_VERSION,
           "feeder": Path(a.feeder).name,
           "settings": "3-phase, wye, model=1, pf=1.0, kV=kv_base*sqrt(3), Static control, solver defaults, LN 0.95-1.05, norm_amps 100%",
           "tolerances": {"kw": 3.0, "losses_kw": 2.0, "pu": 0.002, "pct": 1.0},
           "cases": {}}
    for case, (bus, kw) in CASES.items():
        req = GridRequest(model_file=a.feeder, bus=bus, charger=ChargerSpec(kw=kw))
        lim = req.limits
        CF.compile_opendss_file(a.feeder, force_recompile=True)
        summ = P(M.get_model_summary_records())
        b = P(M.get_buses_records())
        i = b["name"].index(bus)
        kv = C.charger_kv_ll(b["kv_base"][i])
        regs = P(M.get_regcontrols_records())["transformer"]
        caps = [n.split(".", 1)[1] for n in P(M.get_pd_elements_records())["name"] if n.startswith("capacitor.")]
        s0 = P(S.solve_snapshot())
        b0, v0, ln0 = record(bus, regs, caps, lim)
        name = f"ev_charger_{bus}"
        M.add_element("load", name, {"bus1": f"{bus}.1.2.3", "phases": 3, "kV": kv, "kW": kw, "pf": 1.0, "model": 1, "conn": "wye"})
        s1 = P(S.solve_snapshot())
        b1, v1, ln1 = record(bus, regs, caps, lim)
        comp = C.compare(b0, b1, v0, v1, ln0, ln1, lim)
        labels = {}
        for (el, kind) in set(v0) | set(v1):
            row = next(r for r in C.compare(b0, b1, {(el, kind): v0[(el, kind)]} if (el, kind) in v0 else {},
                                            {(el, kind): v1[(el, kind)]} if (el, kind) in v1 else {},
                                            ln0, ln1, lim).violation_table)
            labels.setdefault(row.label, {}).setdefault(kind, []).append(el)
        key["cases"][case] = {
            "bus": bus, "kw": kw,
            "run_facts": {"bus_nodes": b["nodes"][i], "bus_kv_base_ln": b["kv_base"][i], "charger_kv_ll": kv,
                          "baseline_converged": s0["converged"], "charger_converged": s1["converged"]},
            "placeholder_ratings": summ.get("line min norm amps") == summ.get("line max norm amps"),
            "baseline": b0.model_dump(), "with_charger": b1.model_dump(),
            "comparison": {"delta_feeder_kw": comp.delta_feeder_kw, "delta_losses_kw": comp.delta_losses_kw,
                           "charger_bus_delta_v_pu": comp.charger_bus_delta_v_pu,
                           "tap_changes": {k: v.model_dump() for k, v in comp.tap_changes.items()},
                           "violation_counts": comp.violation_counts,
                           "violations_by_label": {l: {k: sorted(v) for k, v in d.items()} for l, d in labels.items()}},
        }
    key["cases"]["bad_bus"] = {"bus": "999", "kw": 600.0, "expect_status": "bus_not_found", "expect_stage": "bus_check"}
    key["cases"]["phase_bus"] = {"bus": "26", "kw": 600.0, "expect_status": "phase_mismatch", "expect_stage": "bus_check",
                                 "note": "bus 26 has nodes [1, 3]"}
    key["cases"]["missing_file"] = {"bus": "60", "kw": 600.0, "model_file": "does_not_exist.dss",
                                    "expect_status": "model_not_found", "expect_stage": "compile"}
    Path(a.out).write_text(json.dumps(key, indent=2))
    print("wrote", a.out)
    for c in CASES:
        k = key["cases"][c]
        print(c, "dP", round(k["comparison"]["delta_feeder_kw"], 1), "counts", k["comparison"]["violation_counts"])


if __name__ == "__main__":
    main()
