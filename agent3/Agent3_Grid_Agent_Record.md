# Agent 3 (Grid Agent): Design Record

2026-11-02 · Taha Riyaan (with John Lopes) · prototype spec v0.1.0

This is the complete record of what was decided for Agent 3, what has been built, and everything noted for the future. For system-wide decisions, see `claude/EV_Siting_Design_Decisions.md`, which is protected and authoritative.

---

## 1. What Agent 3 is

- **Role:** given a feeder model, a bus and a charger, it solves the feeder **without** the charger (baseline), adds the charger, solves **again**, and compares the two. It reports **facts only**.
- **Not its job:**
  - Whether the location is valid: Agent 2.
  - Whether the results are believable: Agent 4 (Result Checker).
  - The verdict: Agent 5.
- **Design: an LLM-led agent, not a Python core with a helper LLM.** The LLM drives the run from the first step using PowerMCP's tools. Consistency comes from three things:
  1. a written procedure (`SKILL.md`)
  2. a typed output it must complete (`submit_result`, validated by Pydantic)
  3. independent checks downstream in the Result Checker
- **One owner per check.** For example, "a baseline exists" belongs to the Result Checker. Agent 3 has no separate code guard for it.

## 2. Files built (prototype v0.1.0)

| File | What it is |
|---|---|
| `SKILL.md` | The procedure, in Agent Skills format (YAML frontmatter plus markdown). It's loaded **in full** as Agent 3's system prompt, so it works with any provider. It contains 8 rules, 10 mandatory steps, the recovery rules and the failure submissions. |
| `models.py` | The Pydantic input and output models. **Input:** `GridRequest` (`ChargerSpec`, `Limits`). **What the LLM submits:** `AgentSubmission` (the `submit_result` tool's input schema, about 2.7k tokens). **What Agent 3 returns:** `GridResult` (the submission plus fields only the runner fills in). |

**Built in the second pass (runner and tests, 2026-11-02):**

| File | What it is |
|---|---|
| `runner.py` | The plain loop (`submit_result`, the shared 3-attempt counter, one reminder, the 40-call cap), preflight, one stateful PowerMCP session, LLM errors returned as a failure result, and a log of every model turn and tool call |
| `config.py`, `.env.example` | All machine-specific settings, from `.env` |
| `mcp_stdio_filter.py` | Keeps OpenDSS's plain-text prints off the MCP channel (section 9) |
| `run_case.py` | Runs one case from the command line |
| `testing/checklist.py` | The test checklist: 5 cases plus a reset check (37 checks), scored against `testing/answer_key.json` |
| `testing/reference.py` | Generates the answer key independently, by calling the PowerMCP functions directly with no LLM |
| `testing/test_runner.py` | 23 offline tests |
| `testing/standin.py` | Scripted stand-ins for the LLM |
| `README.md` | Setup and run instructions |

See section 9 for what was tested and found.

### How they were checked
- **Tool names and behaviour.** All 55 PowerMCP OpenDSS tools were confirmed in the source (commit `20a9178`, 2026-10-30). Every tool named in `SKILL.md` exists. The tool functions were run directly on the IEEE 123-bus feeder to confirm what each one actually returns (section 7).
- **The output model.** `AgentSubmission` was filled from real 123-bus runs (600 kW and 3 MW at bus 60) and validated.
- **The validators were tested on deliberately wrong submissions.** They caught:
  - a "made worse" violation mislabelled as "unchanged"
  - a table that didn't match its counts
  - a failure submission with no stage or reason

## 3. Decisions

### 3.1 Inputs
- **The charger is a new load element** attached to the bus. The bus doesn't need an existing load.
- **The charger description comes from the orchestrator,** built from the user's station profile.
- **Prototype charger:** three-phase, wye, **constant power** (`model=1`), **power factor 1.0** set explicitly (the OpenDSS default of 0.88 is a generic mixed-load value).
- **kV:** Agent 3 reads it from the model (`get_buses_records`), not from the input. **`kv_base` is line-to-neutral**, so a three-phase charger takes kV = `kv_base × √3`. This was verified: 2.4018 → 4.16 kV.
- **Voltage band:** 0.95–1.05 pu, line-to-neutral, from configuration. It's passed to the tools every time. Basis: ANSI C84.1 Range A (sources in section 11).
- **Thermal limit:** 100% of the **normal** rating (`norm_amps`, also the tool's default).
- **Charger name:** supplied by the orchestrator. If it's missing, Agent 3 uses `ev_charger_<bus>`. If the name is already taken, Agent 3 adds `_2`, `_3`, … and records a caveat.

### 3.2 Study setup
- **Background load:** the feeder file's built-in load, used in both runs.
- **Regulators:** the default, Static (taps move). This matches New York utility practice of keeping regulators in service.
- **Solver:** defaults (`max_iterations` 15, `max_control_iter` 10).
- **The model is reloaded every run** (`force_recompile=true`).
- **Extra evidence recorded:** regulator taps before and after, capacitor states, losses.
- **Voltage check:** line-to-neutral, with `get_violation_voltage_ln_nodes_records`. There's no single default: PowerMCP has separate LN, LL and "smart" tools, and the plain-named tool is the smart one.

### 3.3 What the LLM may and may not do (minimal for the prototype; revisit as a whole)
- **Tools:** all 55 OpenDSS tools are available in the prototype.
- **Hard rule: don't change the grid.** The only change allowed is adding the charger (and fixing the charger itself). Reloading is allowed. In the prototype this is **written in the procedure only, not enforced**.
- **Recovery:** up to **3 attempts per problem**, then a failure status.
  - **Compile error:** reload.
  - **No convergence:** retry at 50, then 100 iterations.
  - **Anything unexpected:** stop with `unexpected_error`.

### 3.4 How the run ends
- **A `submit_result` tool (option A).** This keeps it to a single agent. The tool's arguments *are* the typed output.
- **Validation:**
  - **Valid:** the run ends.
  - **Invalid:** the errors go back to the LLM. Failed validations count against **the same 3-attempt counter**.
- **No submission:** if the LLM stops without calling `submit_result`, it gets one reminder (cheap), then the run returns a failure status.

### 3.5 Output (`AgentSubmission` → `GridResult`)
- **`status`:**
  - `ok`
  - the failures: `model_not_found`, `compile_failed`, `bus_not_found`, `phase_mismatch`, `charger_add_failed`, `base_not_converged`, `charger_not_converged`, `unexpected_error`
  - `step_limit_reached`, set by the runner
- **The failure flag:** any status other than `ok`, together with a **`failed_stage`** and a one-line **`reason`**. **Partial results ride along**, e.g. the baseline is returned even if the run with the charger fails.
- **`run_facts`:**
  - procedure version
  - the bus's nodes and kV, and the charger's kV and name
  - control mode and the iteration limits actually used
  - convergence of each solve, and whether the charger was confirmed in the model
  - the recovery actions taken
- **`baseline` / `with_charger` snapshots:**
  - feeder kW and kvar, and losses
  - minimum and maximum voltage, **values only** (no location)
  - the charger bus's voltage per node
  - violation counts
  - regulator taps and capacitor states
- **`comparison`:**
  - the changes in feeder kW and kvar and in losses
  - the voltage change at the charger bus
  - the largest voltage drop on the feeder
  - tap and capacitor changes
  - **the violation table**
- **The violation table:** one row per violation, giving the element, kind, baseline value, with-charger value and **label**:

| Label | Meaning |
|---|---|
| `new` | only present with the charger |
| `made_worse` | present in both, and worse by **more than 0.001 pu** (voltage) or **more than 1 percentage point** (thermal) |
| `unchanged` | present in both, and not worse by more than that threshold (includes small improvements) |
| `resolved` | only present in the baseline |

  The table also gives the **total count, plus the worst 10** for each label and kind.
- **`caveats`:** factual flags with a code and a detail. **No verdict and no judgement.** The codes:
  - `placeholder_line_ratings`, `regulator_at_tap_limit`, `controls_not_settled`
  - `no_service_transformer` and `assumed_power_factor` (always included in the prototype)
  - `charger_name_changed`, `value_unavailable`, `other`

### 3.6 Procedure (`SKILL.md`)
1. **Load the model:** compile, then check the model summary.
2. **Check the bus:** exists, has three phases, read its kV. Then list the regulators and capacitors. This comes **before** any solve.
3. **Solve the baseline.**
4. **Record the baseline.**
5. **Add the charger.**
6. **Confirm** the charger is in the model.
7. **Solve with the charger.**
8. **Record** the run with the charger.
9. **Compare.**
10. **Submit.**

All ten steps are mandatory and in this order. The LLM is free in *how* it does each one: which reading tools to use, extra digging into an odd result, and recovery within the allowed actions.

**Rules added along the way:**
- **Existing violations aren't failures.**
- **Never write a number that didn't come from a tool.**
- **Prefer the small tools.**
- **The placeholder-ratings caveat.** Test feeders often leave line ratings out, and OpenDSS then fills in 400 A. On the 13-bus, the trunk carries about 592 A, which shows as about 148% "overload". On the 123-bus, five lines are "over" their rating at baseline. Those thermal violations are artifacts of missing data, and Agents 4 and 5 shouldn't treat them as real.

### 3.7 Runtime (built; see section 9)
- **The loop is plain Python, not LangGraph:** model → tools → model.
  - **`submit_result` called:** validate it, then end, or send the errors back.
  - **No tool call:** remind once, then fail.
  - **The cap is 40 tool calls** in total. Hitting it gives `step_limit_reached` plus partial results.
  - The loop plugs into the outer LangGraph later through a node wrapper. It can be turned into a LangGraph graph if the debugger or checkpointing is wanted.
- **LLM:** LangChain `init_chat_model`, with the model chosen in configuration. Start with `anthropic:claude-sonnet-5-5`. **API key only**, from the project `.env`.
  - Not a system-wide `ANTHROPIC_API_KEY`: Claude Code would then bill your usage to the API instead of your Pro plan.
  - The Pro account can't be used: LangChain needs an API key.
- **PowerMCP:**
  - **The bridge:** `langchain-mcp-adapters` wraps PowerMCP's 55 tools for the LLM and passes calls through. It's still PowerMCP doing the work.
  - **Launch:** Agent 3 launches **its own OpenDSS server per run** (stdio). The command and `POWERIO_MCP_ALLOWED_ROOTS` come from configuration (portable).
  - **One stateful session per run, shut down at the end.**
- **⚠ Critical:** `MultiServerMCPClient` is **stateless by default**. Every tool call opens a new session, which means a new server process, and the loaded circuit is lost. **Must** use `async with client.session(...)` plus `load_mcp_tools(session)`.
- **Agent 3 is `async`**, because the MCP adapter is.
- **Cost (Sonnet 5.5: $2 per million input tokens, $10 per million output):** revised after measuring (section 9). The first estimate of $1–1.50 per run was too low.

## 4. Choices made during the build (please confirm)
1. **"Worst 10" is per label *and* per kind** (thermal, undervoltage, overvoltage), not per label alone.
   - **Why:** on the 123-bus feeder's placeholder ratings, thermal overloads of 100–270% crowd out voltage violations of a few hundredths of a pu. In the 3 MW test, 9 new overvoltages would otherwise have been mostly hidden behind 7 thermal rows.
   - **To change it:** one constant.
2. **The LLM's submission and the final result are separate models.**
   - **`AgentSubmission`** is what the LLM fills in.
   - **`GridResult`** adds the request echo, the tool-call count, the submission attempts and the model ID. **The runner fills these in, so the LLM can't misreport them.**
   - The runner can also build a `GridResult` itself, for `step_limit_reached` or when nothing was submitted.
3. **The validators check each label against its threshold.** E.g. a row marked `unchanged` that got worse by more than the threshold is rejected, and the error tells the LLM which label to use. This partly addresses the risk of the LLM doing the comparison arithmetic (section 6.3). It catches mislabels and inconsistencies, **not copying errors**.
4. **Caveats are coded** (code plus detail) rather than free text, so later agents and code can act on them.
5. **A missing model file is detected through the model summary.** `compile_opendss_file` reports success even when the file doesn't exist (OpenDSS only prints "not found" on the server's stdout). So step 1 checks `get_model_summary_records`: 0 buses → `model_not_found`.
6. **The charger bus's voltage comes from the LN voltage table.** `get_element_data` on a load returns its properties but no voltages, which was checked. The table is about 9k characters on the 123-bus, and it's also needed for the largest voltage drop.
7. **Thermal % comes from `get_current_loading_percent_records`.** The thermal violation tool returns currents in amps, not percentages. The procedure uses it to find the violating elements, then reads their % from the loading table (the highest value across terminals and phases).
8. **The voltage violation tool lists a whole bus** if any of its nodes violates, along with values for all its nodes. The procedure counts only node values actually outside the limits. In testing, node 83.1 at 1.0434 pu was listed beside the real violation at 83.2.

## 5. Reference numbers under tonight's settings (IEEE 123-bus, `IEEE123Master.dss`)
These were computed directly with the PowerMCP tool functions, using all of tonight's settings: charger at bus 60, 3-phase, 4.16 kV, power factor 1.0, `model=1`, Static control, LN 0.95–1.05, normal ratings.

| | Baseline | 600 kW at bus 60 | 3 MW at bus 60 |
|---|---|---|---|
| Feeder P (kW) | 3,615.24 | +623.4 | +3,249.5 |
| Losses (kW) | 95.98 | +35.0 | +254.0 |
| Min / max V (pu) | 0.9792 / 1.0500 | 0.9706 / 1.0436 | 0.9527 / 1.0562 |
| Thermal violations | 5 (l115, l3, l7, l10, sw1; placeholder ratings) | 3 new (l116, sw2, l52), 5 made worse | 7 new, 5 made worse |
| Voltage violations | 0 | 0 | 9 new overvoltages (150r and 149 at 1.0562, after reg1a tapped up to 1.05625; also 83.2, 1.2, 2.2) |
| Tap changes | — | none | 6 regulators |
| Largest drop | — | 83.1: −0.0094 pu | 63.1: −0.0265 pu |

The baseline matches the earlier blind test (3,615.2 kW, 96.0 kW, 0.9792 / 1.0500, 5 lines over). **The charger results differ from that test's** (+621 kW and +3,355 kW). The 600 kW difference comes entirely from that test's power factor of 0.98; the 3 MW one is not fully explained (section 10). **The answer key was regenerated under these settings** (`testing/answer_key.json`). The old 17 criteria were pulled and the useful ones carried over (section 9).

## 6. Notes and future work

### 6.1 Beyond the prototype (scope)
- **More phase configurations.** Add single-phase and others, inferring phases from the station type:
  - DC fast charger: three-phase
  - commercial Level 2: three-phase, balanced across the site
  - home Level 2: single-phase

  The Site Resolver might be involved. Single-phase gives about **4× the voltage drop** (123-bus sweep).
- **kV for single-phase:** three-phase loads take line-to-line kV, while a single-phase load takes the voltage it's actually connected across (line-to-neutral if wye). Write this into the procedure when single-phase is added.
- **Load model:** look into it properly. Constant power is the current assumption.
- **The `Vminpu` / `Vmaxpu` issue.** Below `Vminpu` (default 0.95) or above `Vmaxpu` (default 1.05), OpenDSS switches a load to constant impedance. The charger's power is then **understated during voltage sags**. Consider lowering `Vminpu` on the charger (e.g. 0.85). Deferred; the prototype keeps the default.
- **Power factor:** revisit 1.0. Real chargers run at about 0.95–0.99+, and a lower power factor adds reactive power draw.
- **Peak, stress and off-peak cases.** Add later: the 123-bus sweep showed the same charger can be fine off-peak and a problem at peak. No PowerMCP tool scales all loads at once.
  - **LoadMult gotcha:** OpenDSS's global load multiplier scales **every** load, the charger included, unless the charger has `status=fixed`. The earlier time-of-day run made exactly this mistake.
- **Emergency rating.** A charger used every day should be judged on the normal rating. The emergency rating could later help grade severity (over normal but under emergency = less severe) or support contingency studies. The tool supports `current_limit_type="emerg_amps"`.
- **Out of scope for now:**
  - **N-1 outage cases:** they matter less on radial feeders (PowerWF's workflow does them).
  - **Time-series studies:** PowerMCP only supports snapshots.
- **Caveats for the report:**
  - ANSI C84.1 applies at the customer's service point, while we measure on the primary feeder, upstream of service transformers. Some utilities therefore plan to a tighter band on the feeder.
  - Modelling the charger directly on the primary bus skips the service transformer.

### 6.2 What the LLM may do: revisit as a whole
- **Tools:** replace "all 55 tools" with a curated subset.
- **The hard rule:** currently procedure text only. **Enforce it later** through the wrapper, which can block or flag `edit_element`, `disable_elements_by_type` and `add_line_in_vsource`.
- **Recovery:** the full set of allowed recovery actions and attempt limits. Tonight's are placeholders.

### 6.3 The LLM doing the comparison arithmetic (known prototype limitation)
- **Risks:**
  - misaligned rows
  - labels wrong near the thresholds
  - miscounts on long lists
  - subtraction errors
- **What catches it downstream:** the Result Checker catches some (e.g. "feeder power rose by about the charger's kW"), but not a single mislabel. The new validators (section 4.3) now catch label and threshold inconsistencies, but **not copying errors**.
- **The permanent fix:** the wrapper saves tool results as artifacts, and code does the comparison. The LLM only points to artifacts (the stronger tracing).
- **A possible middle step:** a helper tool `compare_violations(baseline_list, charger_list)` that does the matching, labelling and counting in code. It removes arithmetic and labelling errors, but the LLM still copies the lists in, so copying errors remain.

### 6.4 Ending the run: future options
- **Fallback to option B:** if the LLM never calls `submit_result`, fill the output from the transcript with `with_structured_output` instead of failing.
- **A separate counter** for validation and formatting failures, distinct from grid recovery attempts.
- **Option B itself, rejected for now, for the record:** a separate structured-output step after the loop.
  - **For:** it formats more reliably.
  - **Against:** it's really two LLM roles, it can't go back for missing evidence, it's more prone to copying errors from a long transcript, and it costs two calls.

### 6.5 Wrapper stage (next after the core works)
- **The wrapper itself:** on the client side, it saves every tool result (with its arguments) as an artifact under `runs/<run_id>/attempt_N/`. Small results go back to the LLM in full; large ones as a summary plus an artifact ID. PowerMCP stays untouched.
- **Still to decide:**
  - artifact naming and layout
  - the size threshold for "large"
  - the pointer format
  - the extraction code
- **Traceability:** run facts already record the procedure version and settings. With the wrapper, the run folder holds the full record.

### 6.6 Other possible improvements
- **An `improved` label:** "unchanged" currently also covers violations that got better but are still there (e.g. 112% → 108%).
- ~~**A pre-flight check in code**~~ **Done** in the runner (section 9).
- ~~**OpenDSS prints to stdout.**~~ **Confirmed and fixed** with `mcp_stdio_filter.py` (section 9).
- **Prompt caching** to cut the cost of long runs: about $2.85 per OK run as measured (section 9).
- **Cost of `get_pd_elements_records`:** it's used only to find capacitor names, and it returns every element (about 12k characters on the 123-bus). A cheaper lookup would help on larger feeders.

## 7. Verified tool behaviour (123-bus and 13-bus, 2026-11-02)

| Tool | What it returns | Size (123-bus) |
|---|---|---|
| `compile_opendss_file(dss_file, force_recompile)` | load status. ⚠ Reports success even if the file is missing. Rejects paths outside the allowed folders. | small |
| `get_model_summary_records` | counts of buses, lines, capacitors…; line min and max normal amps (both 400 = placeholders) | ~0.4k chars |
| `get_buses_records` | per bus: `nodes`, `kv_base` (**line-to-neutral**), connected loads and lines | ~21k chars |
| `get_regcontrols_records` | regulator controls; `transformer` = the regulated transformer | ~2k |
| `get_transformers_records` | per transformer: `tap`, `maxtap`, `mintap` | ~5k |
| `get_pd_elements_records` | all power-delivery elements (capacitor names are among them) | ~12k |
| `solve_snapshot(control_mode, max_iterations, max_control_iter)` | `converged`, `control_iterations`, `control_iteration_limit_hit` | small |
| `get_results_summary_records` | feeder P and Q, losses P and Q, max and min V (**no location**) | ~0.3k |
| `get_violation_voltage_ln_nodes_records(v_min_pu, v_max_pu)` | buses with any node out of limits, **with all their node values** | small to moderate |
| `get_violation_currents_elements_records(current_limit_type)` | violating elements, with **currents in amps** | ~1k |
| `get_current_loading_percent_records(current_limit_type)` | % loading for every element, per terminal and phase | ~17k |
| `get_voltage_mag_ln_nodes_records` | LN voltage per bus and node | ~9k |
| `get_element_data(class, name)` | all properties of one element (a load: **no voltages**; a capacitor: `states`) | ~1k |
| `is_element_in_model(class, name)` | `in_model` true or false | small |
| `add_element(class, name, properties)` | adds a new element; clears the solution | small |

Also: the violation tools default to **0.95 / 1.05** in the current source, although their descriptions still say 1.04. The design doc has been corrected.

## 8. Next steps
1. **On your machine:**
   - Set up the two environments and `.env` (see `README.md`).
   - Run the **free** tests first: `pytest` and `checklist.py --mode standin`.
   - Then run the live checklist, starting with `--cases ok_600`.
2. **Read the live transcripts** (`runs/<id>/transcript.jsonl`) to see how the LLM actually follows the procedure: which steps it skips, merges or repeats, and whether its numbers match.
3. **Open item: the two 123-bus baselines** (section 10).
4. **Wrapper stage** (section 6.5).

## 9. Runner and checklist: what was built, tested and found (2026-11-02)

### How to run it
From the `agent3` folder, with the Agent 3 environment active (setup in `README.md`):

| Command | What it does | Cost |
|---|---|---|
| `python -m pytest testing\test_runner.py -q` | Offline tests (23) | free |
| `python testing\checklist.py --mode standin` | Full checklist with the scripted stand-in (tests the plumbing) | free |
| `python testing\checklist.py --mode live --cases ok_600` | One live case with Claude | ~$3 |
| `python testing\checklist.py --mode live --cases ok_3000 bad_bus phase_bus missing_file` | The remaining live cases | ~$3.50 |
| `python testing\checklist.py --mode live` | All 5 cases plus the reset check | ~$6–7 |
| `python run_case.py --bus 60 --kw 600` | One run by hand; prints a summary and the run folder | ~$3 |

Reports are written to `runs\checklist_*.md` / `.json`, and each run's transcript to `runs\<id>\transcript.jsonl`.

**Test cases:**

| Case | Charger | Expected result |
|---|---|---|
| `ok_600` | 600 kW at bus 60 | `ok` |
| `ok_3000` | 3 MW at bus 60 (stress case) | `ok`, with new violations |
| `bad_bus` | bus 999 (doesn't exist) | `bus_not_found` |
| `phase_bus` | bus 26 (only 2 phases) | `phase_mismatch` |
| `missing_file` | a file that doesn't exist | `model_not_found` |

### The 11 checks for each OK case

| ID | Check |
|---|---|
| K01 | Status is `ok` |
| K02 | Baseline numbers match the answer key (feeder kW ±3, losses ±2 kW, min/max voltage and charger-bus voltage ±0.002 pu) |
| K03 | With-charger numbers match the answer key (same tolerances) |
| K04 | The change in feeder kW matches (±3 kW) |
| K05 | Run facts: both solves converged, the charger was confirmed in the model, the bus has 3 phases, the charger kV is correct (4.16) |
| K06 | Violation counts per label and kind match exactly |
| K07 | Every violation has the right label (new / made worse / unchanged / resolved) |
| K08 | Regulator tap changes match |
| K09 | The required caveats are present (placeholder line ratings, no service transformer, assumed power factor) |
| K10 | Only allowed tool calls were made: no grid changes other than adding or fixing the charger |
| K11 | The run stayed within the 40-call cap |

**Failure cases (F01–F05):** the right status, the right failed stage, a one-line reason, only allowed tool calls, and (for the two bus cases) nothing solved or added before the bus check. **Reset (R01):** when both OK cases are run together, their baselines must match.

**First live result (2026-11-02, `claude-sonnet-5-5`):** `ok_600` passed all 11 checks, with 32 tool calls and 1 submission.

### What the checklist checks (37 checks)
- **Two OK cases** (600 kW and 3 MW at bus 60), 11 checks each:
  - status `ok`
  - baseline and with-charger numbers within tolerance (3 kW, 2 kW for losses, 0.002 pu)
  - the change in feeder kW
  - run facts: converged, charger confirmed, bus nodes, charger kV
  - violation counts
  - violation labels
  - regulator tap changes
  - required caveats
  - **only allowed tool calls (no grid changes)**
  - within the 40-call cap
- **Three failure cases**, each checked for the right status, the right stage and a reason:
  - **bus 999:** expect `bus_not_found`
  - **bus 26, which has nodes [1, 3]:** expect `phase_mismatch`
  - **missing file:** expect `model_not_found`

  For the two bus cases, the checklist also checks that **nothing was solved or added before the bus check**.
- **Reset:** two back-to-back runs give the same baseline.
- **Carried over from the old 17 criteria:** the allowed-tools check, the preflight and the reset check.
- **Dropped:** "limits passed on every call". It's redundant while PowerMCP's default (0.95/1.05) matches ours. Add it back if the configured limits ever differ from the tool default.
- **The answer key is regenerated independently** by `testing/reference.py`, which calls the PowerMCP functions directly with no LLM.

### Tested here (no API key available)
- **23 offline tests pass:**
  - the loop rules: one reminder then failure; failure after 3 invalid submissions; invalid-then-valid accepted on the second attempt; the 40-call cap; LLM errors returned as a failure
  - the preflight: missing file, a missing referenced file, a file outside the allowed folders
  - **the request as it would be sent to Anthropic,** built from a real conversation with all 56 tool schemas, and every tool call answered
  - **the checklist itself** catches a wrong number, a wrong label, a missing caveat, a grid-changing call, a wrong status and differing baselines, and still allows fixing the charger
- **The checklist passes 37/37 with the scripted stand-in through the real PowerMCP server,** run from a clean environment built from `requirements.txt`.
- **A live run with a deliberately invalid key** reached Anthropic's API (401) and came back as a clean `unexpected_error` result. So model selection and the request path work up to authentication.
- **Not tested here: an actual LLM run.** The stand-in proves the plumbing, not whether Claude follows the procedure. That's the live run on your machine.

### Found during the build
1. **⚠ PowerMCP and Agent 3 need two separate Python environments.** PowerMCP requires `mcp>=2`; `langchain-mcp-adapters` (latest, 0.3.2) requires `mcp<2`. Agent 3 launches the server with PowerMCP's own Python (`AGENT3_MCP_COMMAND`), so this works. A client using mcp 1.30 talking to a server using mcp 2.3 was verified.
2. **⚠ OpenDSS prints plain text on the MCP channel.** At shutdown the engine writes "Actor 1 terminated" to stdout, which crashed the MCP client after an otherwise complete run (the risk flagged in section 6.6). It's fixed on our side with `mcp_stdio_filter.py`: it forwards only JSON lines to the client and moves everything else to stderr. PowerMCP is untouched. It can be switched off with `AGENT3_FILTER_SERVER_STDOUT=false`.
3. **Measured cost is higher than estimated.** The stand-in's run makes **32 model calls** (one tool call per turn), and the context grows from about 10k to about 65k tokens. That's about **1.4 million input tokens, or about $2.85 per OK run.** The full checklist costs about **$6–7**.
   - A real model may batch several tool calls per turn and need fewer turns.
   - **Prompt caching** would cut most of this, since the procedure, tool schemas and earlier results are resent on every call. It's the obvious next optimisation.
4. **Preflight in code** (agreed): a missing model file, referenced files that don't exist, or a path outside the allowed folders now returns `model_not_found` **without starting the LLM**. Missing PowerMCP tools are also caught before any LLM call.
5. **LLM errors** (bad key, no credits, network, rate limits) return `unexpected_error` with the reason, instead of crashing.
6. **Each run gets its own folder**, `runs/<run_id>/`, holding a transcript of every model turn and tool call. The checklist's allowed-tools check reads from it. This is a log only, not the wrapper.

## 10. Open items
- **The two 123-bus baselines** (raise with Taha):
  - **Taha's standalone run** used `Run_IEEE123Bus.DSS`. Its final solve fixes the regulator taps at the published IEEE values (+7, −1, 0, −1, +8, +1, +5): 3,621.6 kW, 1,323.9 kvar, 95.3 kW losses, 0.9858–1.0437 pu.
  - **PowerMCP and Agent 3** compile `IEEE123Master.dss` with free regulators (reg1a settles at +6): 3,615.2 kW, 1,311.5 kvar, 96.0 kW, 0.9792–1.0500 pu.
  - Both were reproduced exactly, which explains the "unexplained tap difference" from the chat "Taha learns OpenDSS power flow basics".
  - **Still to decide:** what to do with it, e.g. use the fixed-tap run as a correctness check of PowerMCP.
- **The 600 kW answer-key difference** from the old blind test is fully explained by its power factor of 0.98. The **3 MW** difference is not: it gives +3,260.7 kW at power factor 0.98, against +3,355 kW in the old key, so that case differed in some other way.

## 11. References
- **ANSI C84.1 Range A (0.95–1.05 pu):**
  - NY DPS interconnection study criteria (overvoltage < 105%, undervoltage > 95%, at primary nodes, compared with and without the new system, regulators in service): https://dps.ny.gov/itwg-cesir-analysis-evaluation-initiative-v5-industry-followup
  - Vermont Electric Cooperative planning criteria (114 V = 0.95 pu, 126 V = 1.05 pu): https://vermontelectric.coop/client_media/files/VEC_Planning_Criteria_07122024.pdf
  - Sandia (OSTI 1107705), an OpenDSS hosting-capacity study using ANSI Range A and 100% of the normal line rating: https://www.osti.gov/servlets/purl/1107705
  - USDA RUS Bulletin 1724D-113 (quotes C84.1-2011; covers primary distribution voltages): https://rd.usda.gov/sites/default/files/UEP_Bulletin_1724D-113.pdf
  - PG&E, Voltage Tolerance: https://PGE.com/assets/pge/docs/contact-us/report-an-issue/Voltage_Tolerance.pdf
- **OpenDSS Load properties** (`conn` defaults to wye; `model=1` constant power is the default; `Vminpu` / `Vmaxpu` revert the load to constant impedance): https://opendss.epri.com/Properties7.html
- **Agent Skills format:** agentskills.io (open standard, December 2025).
- **LangChain MCP adapters, stateful sessions:** https://docs.langchain.com/oss/python/langchain/mcp
- **Claude API pricing:** https://platform.claude.com/docs/en/about-claude/pricing
