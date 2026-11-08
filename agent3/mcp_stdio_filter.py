"""Keep the MCP stdio channel clean.

The OpenDSS engine prints plain-text messages (e.g. "Actor 1 terminated") on stdout,
which is also the channel the MCP server uses for its JSON-RPC messages. The MCP
client then fails to parse them and crashes the session when it closes.

This wrapper starts the real server, passes stdin through unchanged, forwards only
JSON lines to stdout and moves everything else to stderr. PowerMCP itself is untouched.

Usage: python mcp_stdio_filter.py <server command> [args...]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: mcp_stdio_filter.py <command> [args...]", file=sys.stderr)
        return 2
    cmd = sys.argv[1:]
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        child = subprocess.Popen([exe, *cmd[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    except FileNotFoundError:
        print(f"mcp_stdio_filter: server command not found: {cmd[0]!r}. Check AGENT3_MCP_COMMAND in .env.",
              file=sys.stderr)
        return 127

    def pump_stdin():
        try:
            while True:
                chunk = sys.stdin.buffer.read1(65536) if hasattr(sys.stdin.buffer, "read1") else sys.stdin.buffer.read(1)
                if not chunk:
                    break
                child.stdin.write(chunk)
                child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    threading.Thread(target=pump_stdin, daemon=True).start()

    out, err = sys.stdout.buffer, sys.stderr.buffer
    for line in iter(child.stdout.readline, b""):
        stripped = line.strip()
        is_json = False
        if stripped[:1] in (b"{", b"["):
            try:
                json.loads(stripped)
                is_json = True
            except ValueError:
                pass
        target = out if is_json else err
        target.write(line if line.endswith(b"\n") else line + b"\n")
        target.flush()
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
