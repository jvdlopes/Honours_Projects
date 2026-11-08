# Agent 3 (Grid Agent): prototype v0.1.0

Agent 3 is an LLM-led agent that runs a baseline-versus-charger study on an OpenDSS feeder through PowerMCP and returns a validated `GridResult`. The design and every decision are recorded in `Agent3_Grid_Agent_Record.md`.

## Files
| File | What it is |
|---|---|
| `SKILL.md` | The procedure (Agent Skills format), loaded in full as the system prompt |
| `models.py` | Pydantic input and output models: `GridRequest`, `AgentSubmission`, `GridResult` |
| `runner.py` | The plain loop: `submit_result`, the 3-attempt counter, one reminder, the 40-call cap, preflight, one stateful PowerMCP session |
| `config.py` | Settings from `.env` (see `.env.example`). Nothing machine-specific is hard-coded |
| `mcp_stdio_filter.py` | Keeps OpenDSS's plain-text prints off the MCP channel (they crash the client otherwise) |
| `run_case.py` | Runs one case from the command line |
| `testing/checklist.py` | The test checklist: 5 cases plus the reset check, scored against `answer_key.json` |
| `testing/answer_key.json` | Expected results under the prototype settings, from `testing/reference.py` |
| `testing/test_runner.py` | Offline tests: loop rules, preflight, Anthropic request format, and the checklist catching bad results |
| `testing/standin.py` | Scripted stand-ins for the LLM (no API key needed) |

## Setup (Windows)

**PowerMCP and Agent 3 must be in two separate Python environments.** PowerMCP needs `mcp>=2`, while `langchain-mcp-adapters` (latest, 0.3.2) needs `mcp<2`. Agent 3 launches the PowerMCP server as a separate process with *its own* Python, so the two never share an environment.

1. **PowerMCP environment:** use the one that already works with Claude Desktop. Its `command` and `args` for the OpenDSS server are in your Claude Desktop config.
2. **Agent 3 environment:**
   ```
   cd agent3
   py -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. **Configuration:** copy `.env.example` to `.env` and fill it in. That means the API key, the PowerMCP Python and `opendss_mcp.py` path, the allowed folders, and the test feeder path. Use forward slashes in paths.

## Run
Run these from the `agent3` folder with `.venv\Scripts\python`.

| Command | What it does | Cost |
|---|---|---|
| `python -m pytest testing/test_runner.py -q` | Offline tests (23) | free |
| `python testing/checklist.py --mode standin` | Full checklist with the scripted stand-in; tests the plumbing on your machine | free |
| `python testing/checklist.py --mode live --cases ok_600` | One real run with Claude | ~$3 |
| `python testing/checklist.py --mode live` | All 5 cases plus the reset check with Claude | ~$6–7 |
| `python run_case.py --bus 60 --kw 600` | One run; prints a summary and the run folder | ~$3 |

Each run writes `runs/<run_id>/transcript.jsonl` (every model turn and tool call). The checklist writes a Markdown and JSON report to `runs/`.

**Run the free ones first.** If `--mode standin` passes on your machine, the server, the session and the paths are all correct, and any problem in a live run comes from the LLM's behaviour.

## Regenerating the answer key
If the settings or the feeder change, run this with the **PowerMCP** Python:
```
<powermcp-python> testing/reference.py --powermcp <PowerMCP repo> --feeder <IEEE123Master.dss>
```
