"""Test-only helpers: turn PowerMCP tool payloads into the Agent 3 output models.

Used by the answer-key generator (reference.py) and the scripted stand-in agent
(standin.py). NOT used by Agent 3 itself: the LLM does this arithmetic.
"""

from __future__ import annotations

import math

from models import (
    THERMAL_WORSE_THRESHOLD_PCT, VOLTAGE_WORSE_THRESHOLD_PU, WORST_N_PER_LABEL,
    Comparison, Limits, Snapshot, TapChange, ViolationRow, VoltageChange,
)


def charger_kv_ll(kv_base_ln: float) -> float:
    return round(kv_base_ln * math.sqrt(3), 4)


def voltage_violations(vv_records: dict, lim: Limits) -> dict:
    """{(element, kind): value}. Only node values actually outside the limits count."""
    out = {}
    for kind in ("undervoltage", "overvoltage"):
        rec = vv_records.get(kind) or {}
        for i, bus in enumerate(rec.get("index", [])):
            for k, col in rec.items():
                if not k.startswith("node"):
                    continue
                v = col[i]
                if v is None:
                    continue
                if (kind == "undervoltage" and v < lim.v_min_pu) or (kind == "overvoltage" and v > lim.v_max_pu):
                    out[(f"{bus}.{k[4:]}", kind)] = v
    return out


def thermal_violations(violating_index: list, loading_records: dict, lim: Limits) -> dict:
    """{(element, 'thermal'): max % over terminals/phases} for the elements the violation tool listed."""
    idx = loading_records["index"]
    cols = [k for k in loading_records if k != "index"]
    out = {}
    for e in violating_index:
        i = idx.index(e)
        pct = max((loading_records[c][i] or 0) for c in cols)
        if pct > lim.thermal_limit_pct:
            out[(e, "thermal")] = pct
    return out


def parse_states(s: str) -> list[int]:
    return [int(float(x)) for x in str(s).strip("[] ").replace(",", " ").split()]


def snapshot(summary: dict, volt: dict, therm: dict, ln_table: dict, bus: str,
             taps: dict, cap_states: dict) -> Snapshot:
    return Snapshot(
        feeder_kw=summary["P feeder (kW)"][0], feeder_kvar=summary["Q feeder (kvar)"][0],
        losses_kw=summary["P losses (kW)"][0], losses_kvar=summary["Q losses (kvar)"][0],
        v_max_pu=summary["max voltage (pu)"][0], v_min_pu=summary["min voltage (pu)"][0],
        charger_bus_v_pu={k[4:]: v for k, v in ln_table[bus].items()},
        voltage_violation_count=len(volt), thermal_violation_count=len(therm),
        regulator_taps=taps, capacitor_states=cap_states,
    )


def compare(b0: Snapshot, b1: Snapshot, viol0: dict, viol1: dict, ln0: dict, ln1: dict, lim: Limits) -> Comparison:
    rows = []
    for key in set(viol0) | set(viol1):
        bv, cv = viol0.get(key), viol1.get(key)
        kind = key[1]
        if bv is None:
            label = "new"
        elif cv is None:
            label = "resolved"
        else:
            worse = {"undervoltage": bv - cv, "overvoltage": cv - bv, "thermal": cv - bv}[kind]
            thr = THERMAL_WORSE_THRESHOLD_PCT if kind == "thermal" else VOLTAGE_WORSE_THRESHOLD_PU
            label = "made_worse" if worse > thr else "unchanged"
        rows.append(ViolationRow(element=key[0], kind=kind, baseline_value=bv, with_charger_value=cv, label=label))

    def severity(r: ViolationRow) -> float:
        v = r.with_charger_value if r.with_charger_value is not None else r.baseline_value
        return {"undervoltage": lim.v_min_pu - v, "overvoltage": v - lim.v_max_pu,
                "thermal": v - lim.thermal_limit_pct}[r.kind]

    counts: dict = {}
    for r in rows:
        counts.setdefault(r.label, {}).setdefault(r.kind, 0)
        counts[r.label][r.kind] += 1
    table = []
    for label in counts:
        for kind in counts[label]:
            group = sorted([r for r in rows if r.label == label and r.kind == kind], key=severity, reverse=True)
            table += group[:WORST_N_PER_LABEL]

    drops = [(f"{bus}.{k[4:]}", ln0[bus][k], ln1[bus][k])
             for bus in ln0 for k in ln0[bus] if bus in ln1 and k in ln1[bus]
             and ln0[bus][k] is not None and ln1[bus][k] is not None]
    node, a, c = min(drops, key=lambda t: t[2] - t[1])
    return Comparison(
        delta_feeder_kw=b1.feeder_kw - b0.feeder_kw, delta_feeder_kvar=b1.feeder_kvar - b0.feeder_kvar,
        delta_losses_kw=b1.losses_kw - b0.losses_kw,
        charger_bus_delta_v_pu={k: b1.charger_bus_v_pu[k] - b0.charger_bus_v_pu[k] for k in b0.charger_bus_v_pu},
        largest_voltage_drop=VoltageChange(node=node, baseline_pu=a, with_charger_pu=c, delta_pu=c - a),
        tap_changes={r: TapChange(baseline=b0.regulator_taps[r], with_charger=b1.regulator_taps[r])
                     for r in b0.regulator_taps if b0.regulator_taps[r] != b1.regulator_taps[r]},
        capacitor_changes={c: f"{b0.capacitor_states[c]} -> {b1.capacitor_states[c]}"
                           for c in b0.capacitor_states if b0.capacitor_states[c] != b1.capacitor_states[c]},
        violation_counts=counts, violation_table=table,
    )
