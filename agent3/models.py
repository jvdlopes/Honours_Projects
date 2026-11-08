"""Agent 3 (Grid Agent): input and output models.

Pydantic models for what Agent 3 receives (GridRequest) and returns (GridResult).

- AgentSubmission is what the LLM fills in through the `submit_result` tool. Its
  JSON Schema is that tool's input schema. Validation errors are sent back to the
  LLM so it can fix and resubmit (this counts against the 3-attempt limit).
- GridResult is what Agent 3 hands back to the orchestrator: the submission plus
  fields the runner owns (the original request, tool-call count, model id).
  The runner also builds a GridResult itself when the LLM can't submit
  (step limit reached, or no submission after one reminder).

Scope: 3-phase chargers only, wye, constant power (OpenDSS model=1), power
factor 1.0, LN voltage limits 0.95-1.05 pu, thermal limit 100% of the normal rating.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROCEDURE_VERSION = "0.1.0"

# Thresholds for the "made_worse" label.
VOLTAGE_WORSE_THRESHOLD_PU = 0.001
THERMAL_WORSE_THRESHOLD_PCT = 1.0
WORST_N_PER_LABEL = 10  # applied per label AND per kind (see Comparison.violation_table)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


class ChargerSpec(_Strict):
    """The charger to test. Built by the orchestrator from the user's station profile."""

    kw: float = Field(gt=0, description="Total charger power in kW (all ports, all phases).")
    power_factor: float = Field(
        default=1.0, gt=0, le=1,
        description="Default 1.0. Set explicitly: the OpenDSS default is 0.88.",
    )
    phases: Literal[3] = Field(default=3, description="3-phase only for now.")
    conn: Literal["wye"] = Field(default="wye", description="Wiring. Wye is also the OpenDSS default.")
    load_model: Literal[1] = Field(default=1, description="OpenDSS load model 1 = constant power.")
    name: Optional[str] = Field(
        default=None,
        description="Name for the new load element. If missing, Agent 3 uses ev_charger_<bus>.",
    )


class Limits(_Strict):
    """Study limits. Filled from configuration, passed explicitly to the tools."""

    v_min_pu: float = Field(default=0.95, description="ANSI C84.1 Range A lower limit.")
    v_max_pu: float = Field(default=1.05, description="ANSI C84.1 Range A upper limit.")
    thermal_limit_pct: float = Field(default=100.0, description="Percent of the element's rating.")
    current_limit_type: Literal["norm_amps"] = Field(
        default="norm_amps", description="Normal rating. Emergency rating is a future option."
    )


class GridRequest(_Strict):
    """What the orchestrator sends Agent 3."""

    model_file: str = Field(description="Path to the master .dss file, under the configured allowed roots.")
    bus: str = Field(description="Bus from Agent 2. Agent 3 assumes the location itself is valid.")
    charger: ChargerSpec
    limits: Limits = Field(default_factory=Limits)


# --------------------------------------------------------------------------
# Output pieces
# --------------------------------------------------------------------------

Status = Literal[
    "ok",
    "model_not_found",
    "compile_failed",
    "bus_not_found",
    "phase_mismatch",
    "charger_add_failed",
    "base_not_converged",
    "charger_not_converged",
    "step_limit_reached",
    "unexpected_error",
]

FailedStage = Literal[
    "compile", "bus_check", "baseline", "charger_add", "charger_run", "submission", "agent_loop"
]

Label = Literal["new", "made_worse", "unchanged", "resolved"]
ViolationKind = Literal["undervoltage", "overvoltage", "thermal"]

CaveatCode = Literal[
    "placeholder_line_ratings",   # all lines share one rating (e.g. OpenDSS default 400 A)
    "regulator_at_tap_limit",     # a regulator sits at its max or min tap
    "controls_not_settled",       # solve converged but hit the control-iteration limit
    "no_service_transformer",     # charger modelled directly on the primary bus
    "assumed_power_factor",       # assumed power factor (default 1.0)
    "charger_name_changed",       # requested/default name was taken, suffix added
    "value_unavailable",          # a value could not be obtained from the tools
    "other",
]


class Caveat(_Strict):
    """A factual flag. No judgement about whether the site is good."""

    code: CaveatCode
    detail: str = Field(description="One factual sentence, with the numbers that triggered it.")


class RecoveryAction(_Strict):
    stage: FailedStage
    problem: str = Field(description="What went wrong, e.g. 'baseline solve did not converge'.")
    action: str = Field(description="What was tried, e.g. 'solve_snapshot(max_iterations=50)'.")
    attempt: int = Field(ge=1, le=3, description="Which attempt at this problem (max 3).")
    succeeded: bool


class RunFacts(_Strict):
    """Facts about the run. Every value must come from a tool result."""

    procedure_version: str = Field(description="The `metadata.version` from SKILL.md.")
    bus_nodes: Optional[list[int]] = Field(default=None, description="'nodes' of the bus from get_buses_records.")
    bus_kv_base_ln: Optional[float] = Field(default=None, description="'kv_base' from get_buses_records (line-to-neutral).")
    charger_kv_ll: Optional[float] = Field(default=None, description="kV used for the charger = kv_base x sqrt(3).")
    charger_name: Optional[str] = Field(default=None, description="Name actually used for the charger load.")
    control_mode: str = Field(default="Static", description="Regulator control mode used in the solves.")
    baseline_max_iterations: Optional[int] = Field(default=None, description="max_iterations of the baseline solve that was kept.")
    charger_max_iterations: Optional[int] = Field(default=None, description="max_iterations of the with-charger solve that was kept.")
    baseline_converged: Optional[bool] = None
    charger_converged: Optional[bool] = None
    charger_confirmed_in_model: Optional[bool] = Field(
        default=None, description="is_element_in_model after adding, and get_element_data matches the request."
    )
    recovery_actions: list[RecoveryAction] = Field(default_factory=list)


class Snapshot(_Strict):
    """One solved state of the feeder (baseline, or with the charger)."""

    feeder_kw: float = Field(description="'P feeder (kW)' from get_results_summary_records.")
    feeder_kvar: float = Field(description="'Q feeder (kvar)'.")
    losses_kw: float = Field(description="'P losses (kW)'.")
    losses_kvar: float = Field(description="'Q losses (kvar)'.")
    v_max_pu: float = Field(description="'max voltage (pu)'. Value only, no location.")
    v_min_pu: float = Field(description="'min voltage (pu)'. Value only, no location.")
    charger_bus_v_pu: dict[str, float] = Field(
        description="LN voltage at the charger bus per node, e.g. {'1': 0.98, '2': 1.01, '3': 0.99}."
    )
    voltage_violation_count: int = Field(ge=0, description="Number of node voltages outside the limits.")
    thermal_violation_count: int = Field(ge=0, description="Number of elements over the thermal limit.")
    regulator_taps: dict[str, float] = Field(
        description="Regulator transformer name -> 'tap' from get_transformers_records."
    )
    capacitor_states: dict[str, list[int]] = Field(
        description="Capacitor name -> 'states' from get_element_data (1 = step on)."
    )


class ViolationRow(_Strict):
    """One violation, compared between the baseline and the run with the charger."""

    element: str = Field(description="Voltage: '<bus>.<node>', e.g. '83.2'. Thermal: element, e.g. 'line.l115'.")
    kind: ViolationKind
    baseline_value: Optional[float] = Field(
        description="pu for voltage, % of normal rating for thermal. null if not a violation in the baseline."
    )
    with_charger_value: Optional[float] = Field(
        description="Same units. null if not a violation with the charger."
    )
    label: Label

    @model_validator(mode="after")
    def _label_matches_values(self) -> "ViolationRow":
        b, c = self.baseline_value, self.with_charger_value
        if b is None and c is None:
            raise ValueError(f"{self.element}: baseline_value and with_charger_value cannot both be null")
        if self.label == "new" and not (b is None and c is not None):
            raise ValueError(f"{self.element}: 'new' needs baseline_value null and with_charger_value set")
        if self.label == "resolved" and not (b is not None and c is None):
            raise ValueError(f"{self.element}: 'resolved' needs baseline_value set and with_charger_value null")
        if self.label in ("made_worse", "unchanged"):
            if b is None or c is None:
                raise ValueError(f"{self.element}: '{self.label}' needs both values")
            worsening = {"undervoltage": b - c, "overvoltage": c - b, "thermal": c - b}[self.kind]
            threshold = THERMAL_WORSE_THRESHOLD_PCT if self.kind == "thermal" else VOLTAGE_WORSE_THRESHOLD_PU
            if self.label == "made_worse" and worsening <= threshold:
                raise ValueError(
                    f"{self.element}: labelled made_worse but got worse by {worsening:.4g} "
                    f"(needs > {threshold}); use 'unchanged'"
                )
            if self.label == "unchanged" and worsening > threshold:
                raise ValueError(
                    f"{self.element}: labelled unchanged but got worse by {worsening:.4g} "
                    f"(> {threshold}); use 'made_worse'"
                )
        return self


class VoltageChange(_Strict):
    node: str = Field(description="'<bus>.<node>'.")
    baseline_pu: float
    with_charger_pu: float
    delta_pu: float = Field(description="with_charger_pu - baseline_pu (negative = drop).")


class TapChange(_Strict):
    baseline: float
    with_charger: float


class Comparison(_Strict):
    delta_feeder_kw: float = Field(description="with_charger.feeder_kw - baseline.feeder_kw (should be close to the charger kW).")
    delta_feeder_kvar: float
    delta_losses_kw: float
    charger_bus_delta_v_pu: dict[str, float] = Field(description="Per node, with_charger - baseline.")
    largest_voltage_drop: Optional[VoltageChange] = Field(
        default=None, description="Node with the most negative change across the feeder, from the two LN voltage tables."
    )
    tap_changes: dict[str, TapChange] = Field(default_factory=dict, description="Only regulators whose tap changed.")
    capacitor_changes: dict[str, str] = Field(
        default_factory=dict, description="Only capacitors whose state changed, e.g. {'c83': '[1] -> [0]'}."
    )
    violation_counts: dict[Label, dict[ViolationKind, int]] = Field(
        description="Total violations per label and kind (all of them, not just the rows listed), "
        "e.g. {'new': {'thermal': 7, 'overvoltage': 13}}. Omit zero entries."
    )
    violation_table: list[ViolationRow] = Field(
        description=f"For each label and kind: the worst {WORST_N_PER_LABEL} (fewer if there are fewer)."
    )

    @model_validator(mode="after")
    def _table_matches_counts(self) -> "Comparison":
        for label in ("new", "made_worse", "unchanged", "resolved"):
            for kind in ("undervoltage", "overvoltage", "thermal"):
                total = self.violation_counts.get(label, {}).get(kind, 0)
                listed = sum(1 for r in self.violation_table if r.label == label and r.kind == kind)
                where = f"'{label}' {kind}"
                if listed > WORST_N_PER_LABEL:
                    raise ValueError(f"{where}: list at most {WORST_N_PER_LABEL} rows, got {listed}")
                if listed > total:
                    raise ValueError(f"{where}: {listed} rows listed but violation_counts says {total}")
                if listed < min(total, WORST_N_PER_LABEL):
                    raise ValueError(
                        f"{where}: violation_counts says {total} but only {listed} rows listed "
                        f"(list min({total}, {WORST_N_PER_LABEL}))"
                    )
        return self


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class AgentSubmission(_Strict):
    """Arguments of the `submit_result` tool. Facts only; no verdict."""

    status: Status
    failed_stage: Optional[FailedStage] = Field(default=None, description="Required when status is not 'ok'.")
    reason: Optional[str] = Field(default=None, description="One line. Required when status is not 'ok'.")
    run_facts: RunFacts
    baseline: Optional[Snapshot] = Field(default=None, description="Include whenever the baseline solved, even on failure.")
    with_charger: Optional[Snapshot] = None
    comparison: Optional[Comparison] = None
    caveats: list[Caveat] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_consistency(self) -> "AgentSubmission":
        if self.status == "ok":
            missing = [n for n in ("baseline", "with_charger", "comparison") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"status 'ok' requires: {', '.join(missing)}")
            if self.failed_stage is not None:
                raise ValueError("status 'ok' must not have failed_stage")
        else:
            if self.failed_stage is None or not self.reason:
                raise ValueError(f"status '{self.status}' requires failed_stage and reason")
        if self.status == "charger_not_converged" and self.baseline is None:
            raise ValueError("charger_not_converged: include the baseline snapshot as a partial result")
        return self


class GridResult(AgentSubmission):
    """What Agent 3 returns to the orchestrator. Any status other than 'ok' is the failure flag."""

    request: GridRequest = Field(description="Set by the runner, not the LLM.")
    tool_calls_used: int = Field(ge=0, description="Counted by the runner.")
    submission_attempts: int = Field(ge=0, description="Counted by the runner.")
    llm_model: str = Field(description="From configuration, e.g. 'anthropic:claude-sonnet-5-5'.")
