"""Run Claude Code headless in an isolated project and collect the evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .proxy import RecordingProxy

CLAUDE_BIN = os.environ.get("BENCH_CLAUDE_BIN", "claude")
PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"

# Allowlist, not denylist: the runs print their environment (`env` case) to the
# API and the report, so only what Claude Code needs to start is passed on. It
# also drops the variables a parent Claude Code session exports to children.
# Auth comes from ~/.claude credentials, or from the variables kept below.
_KEEP = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
         "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
         "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
         "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}
_KEEP_PREFIXES = ("LC_", "CLAUDE_CODE_USE_")


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _KEEP or k.startswith(_KEEP_PREFIXES)}
    env.update(extra or {})
    return env


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class Run:
    session_id: str
    returncode: int
    events: list[dict]
    stderr: str
    wire: str
    transcript: str
    answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    def called(self, name: str, needle: str = "") -> bool:
        """True when tool `name` was called with `needle` somewhere in its input."""
        return any(c.name == name and needle in json.dumps(c.input) for c in self.tool_calls)

    def leaks(self, secret: str) -> dict[str, bool]:
        return {
            "wire": secret in self.wire,
            "transcript": secret in self.transcript,
            "answer": secret in self.answer,
        }


def transcript_text(session_id: str) -> str:
    """Session transcript plus anything stored beside it (subagents, tool results)."""
    parts = []
    for path in PROJECTS_DIR.glob(f"*/{session_id}*"):
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for f in files:
            parts.append(f.read_text(errors="replace"))
    return "\n".join(parts)


def run_claude(
    prompt: str,
    *,
    cwd: Path,
    settings: dict,
    artifacts: Path,
    model: str,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> Run:
    """Run `claude -p` with only `settings` loaded, behind a recording proxy."""
    artifacts.mkdir(parents=True, exist_ok=True)
    settings_path = artifacts / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))
    session_id = str(uuid.uuid4())

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--session-id", session_id,
        "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        # Only the test's settings: no user hooks, no project CLAUDE.md settings.
        "--setting-sources", "project",
        "--settings", str(settings_path),
        "--strict-mcp-config",
    ]

    proxy = RecordingProxy().start()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            env=clean_env({"ANTHROPIC_BASE_URL": proxy.base_url, **(env or {})}),
        )
    finally:
        proxy.stop()

    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    tool_calls = [
        ToolCall(block["name"], block.get("input", {}))
        for ev in events if ev.get("type") == "assistant"
        for block in ev.get("message", {}).get("content", [])
        if block.get("type") == "tool_use"
    ]
    answer = "\n".join(ev.get("result", "") for ev in events if ev.get("type") == "result")

    run = Run(session_id, proc.returncode, events, proc.stderr, proxy.wire_text(),
              transcript_text(session_id), answer, tool_calls)

    (artifacts / "stream.jsonl").write_text(proc.stdout)
    (artifacts / "stderr.txt").write_text(proc.stderr)
    (artifacts / "wire.txt").write_text(run.wire)
    (artifacts / "transcript.jsonl").write_text(run.transcript)
    (artifacts / "requests.json").write_text(json.dumps(
        [{"method": e.method, "path": e.path, "status": e.status, "bytes": len(e.body)} for e in proxy.exchanges],
        indent=2))
    return run


def cleanup_transcript(session_id: str) -> None:
    for path in PROJECTS_DIR.glob(f"*/{session_id}*"):
        shutil.rmtree(path) if path.is_dir() else path.unlink()
