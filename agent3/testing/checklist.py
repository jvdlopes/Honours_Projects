"""Agent 3 test checklist.

Runs the test cases through the real runner and checks each result against the
answer key (testing/answer_key.json, made by testing/reference.py).

    python testing/checklist.py --mode live      # real LLM from config (costs ~$3-4 for all cases)
    python testing/checklist.py --mode standin   # scripted stand-in, no API key (tests the plumbing only)
    python testing/checklist.py --mode live --cases ok_600 bad_bus

Writes a Markdown and JSON report to the runs folder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from config import load_settings  # noqa: E402
from models import ChargerSpec, GridRequest  # noqa: E402
from runner import AgentRun, run_grid_agent  # noqa: E402

KEY = json.loads((HERE / "answer_key.json").read_text())
TOL = KEY["tolerances"]
CASE_ORDER = ["ok_600", "ok_3000", "bad_bus", "phase_bus", "missing_file"]

FORBIDDEN = {"disable_elements_by_type", "add_line_in_vsource"}
NO_SOLVE_BEFORE_BUS_CHECK = {"bad_bus", "phase_bus"}


# --------------------------------------------------------------------------
# Individual checks: each returns (ok, observed)
# --------------------------------------------------------------------------


def _close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def _snapshot_diffs(got, exp) -> list[str]:
    bad = []
    for f, tol in (("feeder_kw", TOL["kw"]), ("losses_kw", TOL["losses_kw"]),
                   ("v_min_pu", TOL["pu"]), ("v_max_pu", TOL["pu"])):
        if not _close(getattr(got, f), exp[f], tol):
            bad.append(f"{f} {getattr(got, f)} vs {exp[f]:.4f}")
    for node, v in exp["charger_bus_v_pu"].items():
        if not _close(got.charger_bus_v_pu.get(node), v, TOL["pu"]):
            bad.append(f"charger_bus_v_pu[{node}] {got.charger_bus_v_pu.get(node)} vs {v:.4f}")
    return bad


def tool_rule_violations(run: AgentRun) -> list[str]:
    """Calls that change the grid, other than adding/fixing the charger, or loading another file."""
    req = run.result.request
    charger = (run.result.run_facts.charger_name or req.charger.name or f"ev_charger_{req.bus}").lower()
    bad = []
    for c in run.tool_calls:
        a = c.args or {}
        if c.name in FORBIDDEN:
            bad.append(f"#{c.n} {c.name}")
        elif c.name in ("add_element", "edit_element"):
            if str(a.get("element_class", "")).lower() != "load" or not str(a.get("element_name", "")).lower().startswith(charger):
                bad.append(f"#{c.n} {c.name} on {a.get('element_class')}.{a.get('element_name')}")
        elif c.name == "compile_opendss_file":
            if Path(str(a.get("dss_file", ""))).resolve() != Path(req.model_file).resolve():
                bad.append(f"#{c.n} compiled another file: {a.get('dss_file')}")
    return bad


def check_ok_case(name: str, run: AgentRun) -> list[tuple]:
    k = KEY["cases"][name]
    r = run.result
    out = []
    add = lambda cid, desc, ok, obs="": out.append((cid, desc, bool(ok), obs))  # noqa: E731
    add("K01", "status is ok", r.status == "ok", f"{r.status} {r.failed_stage or ''} {r.reason or ''}".strip())
    if r.status != "ok":
        add("K10", "only allowed tool calls", not tool_rule_violations(run), "; ".join(tool_rule_violations(run)))
        return out
    d = _snapshot_diffs(r.baseline, k["baseline"])
    add("K02", "baseline numbers within tolerance", not d, "; ".join(d))
    d = _snapshot_diffs(r.with_charger, k["with_charger"])
    add("K03", "with-charger numbers within tolerance", not d, "; ".join(d))
    add("K04", "feeder kW change within tolerance",
        _close(r.comparison.delta_feeder_kw, k["comparison"]["delta_feeder_kw"], TOL["kw"]),
        f"{r.comparison.delta_feeder_kw:.1f} vs {k['comparison']['delta_feeder_kw']:.1f}")
    f, kf = r.run_facts, k["run_facts"]
    facts_ok = (f.baseline_converged is True and f.charger_converged is True and f.charger_confirmed_in_model is True
                and f.bus_nodes == kf["bus_nodes"] and _close(f.charger_kv_ll, kf["charger_kv_ll"], 0.01))
    add("K05", "run facts: converged, charger confirmed, bus nodes, charger kV", facts_ok,
        f"conv {f.baseline_converged}/{f.charger_converged}, confirmed {f.charger_confirmed_in_model}, nodes {f.bus_nodes}, kV {f.charger_kv_ll}")
    add("K06", "violation counts match", r.comparison.violation_counts == k["comparison"]["violation_counts"],
        f"{r.comparison.violation_counts} vs {k['comparison']['violation_counts']}")
    exp_labels = k["comparison"]["violations_by_label"]
    wrong = [f"{row.element} {row.kind} labelled {row.label}" for row in r.comparison.violation_table
             if row.element not in exp_labels.get(row.label, {}).get(row.kind, [])]
    missing = [f"{lab}/{kind}: {sorted(set(els) - {x.element for x in r.comparison.violation_table if x.label == lab and x.kind == kind})}"
               for lab, kinds in exp_labels.items() for kind, els in kinds.items() if len(els) <= 10
               and set(els) - {x.element for x in r.comparison.violation_table if x.label == lab and x.kind == kind}]
    add("K07", "violation labels match the key", not wrong and not missing, "; ".join(wrong + missing))
    exp_taps = k["comparison"]["tap_changes"]
    got_taps = {n: t.model_dump() for n, t in r.comparison.tap_changes.items()}
    taps_ok = set(got_taps) == set(exp_taps) and all(
        _close(got_taps[n]["baseline"], exp_taps[n]["baseline"], 1e-3) and _close(got_taps[n]["with_charger"], exp_taps[n]["with_charger"], 1e-3)
        for n in exp_taps)
    add("K08", "regulator tap changes match", taps_ok, f"{sorted(got_taps)} vs {sorted(exp_taps)}")
    codes = {c.code for c in r.caveats}
    need = {"no_service_transformer", "assumed_power_factor"} | ({"placeholder_line_ratings"} if k["placeholder_ratings"] else set())
    add("K09", "required caveats present", need <= codes, f"missing {sorted(need - codes)}" if need - codes else "")
    v = tool_rule_violations(run)
    add("K10", "only allowed tool calls (no grid changes)", not v, "; ".join(v))
    add("K11", "within the tool-call cap", r.tool_calls_used <= 40, f"{r.tool_calls_used} calls, {r.submission_attempts} submission(s)")
    return out


def check_fail_case(name: str, run: AgentRun) -> list[tuple]:
    k = KEY["cases"][name]
    r = run.result
    out = []
    add = lambda cid, desc, ok, obs="": out.append((cid, desc, bool(ok), obs))  # noqa: E731
    add("F01", f"status is {k['expect_status']}", r.status == k["expect_status"], r.status)
    add("F02", f"failed_stage is {k['expect_stage']}", r.failed_stage == k["expect_stage"], str(r.failed_stage))
    add("F03", "has a one-line reason", bool(r.reason), r.reason or "")
    v = tool_rule_violations(run)
    add("F04", "only allowed tool calls", not v, "; ".join(v))
    if name in NO_SOLVE_BEFORE_BUS_CHECK:
        solved = [c.n for c in run.tool_calls if c.name in ("solve_snapshot", "add_element")]
        add("F05", "no solve or charger added (bus check comes first)", not solved, f"calls #{solved}" if solved else "")
    return out


def check_reset(runs: dict) -> list[tuple]:
    """Back-to-back runs give the same baseline (each run reloads the model)."""
    ran = [n for n in ("ok_600", "ok_3000") if n in runs]
    if len(ran) < 2:
        return []  # only checked when both OK cases are in the same checklist run
    oks = [(n, runs[n]) for n in ran if runs[n].result.status == "ok"]
    if len(oks) < 2:
        return [("R01", "back-to-back runs give the same baseline", False, "needs both ok cases to pass")]
    (n1, a), (n2, b) = oks
    d = [x for x in _snapshot_diffs(b.result.baseline, a.result.baseline.model_dump())]
    return [("R01", "back-to-back runs give the same baseline", not d, f"{n1} vs {n2}: " + "; ".join(d) if d else f"{n1} = {n2}")]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def make_request(case: str, settings) -> GridRequest:
    k = KEY["cases"][case]
    feeder = Path(settings.test_feeder) if settings.test_feeder else None
    if feeder is None:
        raise SystemExit("Set AGENT3_TEST_FEEDER to the IEEE123Master.dss path (see .env.example).")
    model_file = str(feeder.parent / k["model_file"]) if "model_file" in k else str(feeder)
    return GridRequest(model_file=model_file, bus=k["bus"], charger=ChargerSpec(kw=k["kw"]),
                       limits={"v_min_pu": settings.v_min_pu, "v_max_pu": settings.v_max_pu,
                               "thermal_limit_pct": settings.thermal_limit_pct})


async def run_cases(cases: list[str], mode: str, settings) -> dict:
    runs = {}
    for case in cases:
        llm = None
        if mode == "standin":
            from testing.standin import ProcedureStandIn
            llm = ProcedureStandIn()
        print(f"running {case} ...", flush=True)
        runs[case] = await run_grid_agent(make_request(case, settings), settings, llm=llm)
    return runs


def evaluate(runs: dict) -> list[tuple]:
    rows = []
    for case, run in runs.items():
        checks = check_ok_case(case, run) if case.startswith("ok_") else check_fail_case(case, run)
        rows += [(case, *c) for c in checks]
    rows += [("reset", *c) for c in check_reset(runs)]
    return rows


def report(rows: list[tuple], runs: dict, mode: str, settings) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(settings.runs_dir) / f"checklist_{mode}_{stamp}"
    passed = sum(r[3] for r in rows)
    lines = [f"# Agent 3 checklist ({mode}) {stamp}", "",
             f"Model: `{settings.llm_model if mode == 'live' else 'scripted stand-in'}` · **{passed}/{len(rows)} checks passed**", "",
             "| Case | ID | Check | Result | Observed |", "|---|---|---|---|---|"]
    for case, cid, desc, ok, obs in rows:
        lines.append(f"| {case} | {cid} | {desc} | {'PASS' if ok else '**FAIL**'} | {str(obs).replace('|', '/')[:300]} |")
    lines += ["", "Run folders (transcripts):"] + [f"- {c}: `{r.run_dir}`" for c, r in runs.items()]
    out.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(
        {"mode": mode, "passed": passed, "total": len(rows),
         "checks": [dict(zip(("case", "id", "check", "pass", "observed"), r)) for r in rows],
         "results": {c: r.result.model_dump(mode="json") for c, r in runs.items()}}, indent=2, default=str))
    print("\n".join(lines))
    return out.with_suffix(".md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "standin"], required=True)
    ap.add_argument("--cases", nargs="*", default=CASE_ORDER, choices=CASE_ORDER)
    a = ap.parse_args()
    settings = load_settings()
    runs = asyncio.run(run_cases(a.cases, a.mode, settings))
    rows = evaluate(runs)
    path = report(rows, runs, a.mode, settings)
    print(f"\nReport: {path}")
    sys.exit(0 if all(r[3] for r in rows) else 1)


if __name__ == "__main__":
    main()
