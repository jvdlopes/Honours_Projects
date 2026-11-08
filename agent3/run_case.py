"""Run Agent 3 once from the command line.

    python run_case.py --bus 60 --kw 600
    python run_case.py --bus 60 --kw 600 --model-file C:/path/to/IEEE123Master.dss
    python run_case.py --bus 60 --kw 600 --standin     # scripted stand-in, no API key

Prints a short summary and the path of the run folder (full result + transcript).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from config import load_settings
from models import ChargerSpec, GridRequest, Limits
from runner import run_grid_agent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", required=True)
    ap.add_argument("--kw", type=float, required=True)
    ap.add_argument("--model-file", help="Master .dss file (default: AGENT3_TEST_FEEDER)")
    ap.add_argument("--name", help="Charger name (default ev_charger_<bus>)")
    ap.add_argument("--standin", action="store_true", help="Use the scripted stand-in instead of the LLM")
    a = ap.parse_args()

    s = load_settings()
    model_file = a.model_file or (str(s.test_feeder) if s.test_feeder else None)
    if not model_file:
        raise SystemExit("Give --model-file or set AGENT3_TEST_FEEDER.")
    req = GridRequest(model_file=model_file, bus=a.bus, charger=ChargerSpec(kw=a.kw, name=a.name),
                      limits=Limits(v_min_pu=s.v_min_pu, v_max_pu=s.v_max_pu, thermal_limit_pct=s.thermal_limit_pct))
    llm = None
    if a.standin:
        from testing.standin import ProcedureStandIn
        llm = ProcedureStandIn()
    run = asyncio.run(run_grid_agent(req, s, llm=llm))
    r = run.result
    print(f"\nstatus: {r.status}" + (f"  ({r.failed_stage}: {r.reason})" if r.status != "ok" else ""))
    print(f"tool calls: {r.tool_calls_used}, submissions: {r.submission_attempts}, model: {r.llm_model}")
    if r.comparison:
        c = r.comparison
        print(f"feeder kW change: {c.delta_feeder_kw:+.1f}, losses change: {c.delta_losses_kw:+.1f} kW")
        print(f"violations: {json.dumps(c.violation_counts)}")
        print(f"caveats: {[x.code for x in r.caveats]}")
    print(f"run folder: {run.run_dir}")
    (run.run_dir / "result.json").write_text(r.model_dump_json(indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
