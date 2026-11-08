"""Agent 3 configuration.

All machine-specific values come from environment variables or a `.env` file, so
nothing is hard-coded to one machine. See `.env.example`.

API key: the Anthropic key is read by LangChain from ANTHROPIC_API_KEY. Put it in
this project's `.env`, NOT as a system-wide variable, otherwise Claude Code would
start billing your Pro usage to the API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

HERE = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT3_", env_file=HERE / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM (provider-neutral: any LangChain "provider:model" string) ---
    llm_model: str = Field(default="anthropic:claude-sonnet-5-5")
    llm_max_tokens: int = Field(default=8192, description="Room for the submit_result payload.")

    # --- PowerMCP OpenDSS server, launched per run over stdio ---
    mcp_command: str = Field(default=sys.executable, description="Usually the Python that has PowerMCP installed.")
    mcp_args: list[str] = Field(
        default_factory=list,
        description='JSON list, e.g. ["C:/path/to/PowerMCP/OpenDSS/opendss_mcp.py"] or ["run", "opendss"] with mcp_command=powermcp.',
    )
    filter_server_stdout: bool = Field(
        default=True, description="Wrap the server with mcp_stdio_filter.py (OpenDSS prints text on stdout)."
    )
    allowed_roots: list[str] = Field(
        default_factory=list,
        description="JSON list of folders the server may read. Passed as POWERIO_MCP_ALLOWED_ROOTS.",
    )

    # --- Agent rules ---
    max_tool_calls: int = 40
    max_attempts: int = 3  # shared counter: failed submissions count here too
    skill_path: Path = HERE / "SKILL.md"

    # --- Study limits used to build requests (ANSI C84.1 Range A; normal rating) ---
    v_min_pu: float = 0.95
    v_max_pu: float = 1.05
    thermal_limit_pct: float = 100.0

    # --- Logs: one folder per run with the transcript and tool-call log ---
    runs_dir: Path = HERE / "runs"

    # --- Testing: the IEEE 123-bus master file used by testing/checklist.py ---
    test_feeder: Optional[Path] = None

    def server_env(self) -> dict[str, str]:
        """Environment for the MCP server process (inherits the current env, adds allowed roots)."""
        env = dict(os.environ)
        if self.allowed_roots:
            env["POWERIO_MCP_ALLOWED_ROOTS"] = os.pathsep.join(self.allowed_roots)
        return env


def load_settings(**overrides) -> Settings:
    # Put provider keys from .env (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) into this
    # process only, where LangChain looks for them. Existing variables win. Nothing
    # is set system-wide, so Claude Code in other terminals keeps using your Pro plan.
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env", override=False)
    return Settings(**overrides)
