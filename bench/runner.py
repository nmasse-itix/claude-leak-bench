"""Run Claude Code headless in an isolated project and collect the evidence.

Two sandboxes:
- PodmanSandbox (default): Claude Code in a rootless container on an internal
  network with no egress and a dummy credential. A proxy container is the
  only way out; it records every request and swaps in the real credential.
- HostSandbox: Claude Code on the host, behind an in-process recording proxy.

A subject's interceptor (a redacting proxy) sits between Claude Code and the
recording proxy, so what is recorded is what the interceptor let through.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .proxy import RecordingProxy
from .subjects import Interceptor, Sidecar

CLAUDE_BIN = os.environ.get("BENCH_CLAUDE_BIN", "claude")
ROOT = Path(__file__).resolve().parent.parent

# Allowlist, not denylist: the runs print their environment (`env` case) to the
# API and the report, so only what Claude Code needs to start is passed on. It
# also drops the variables a parent Claude Code session exports to children.
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


@dataclass
class RawRun:
    returncode: int
    stdout: str
    stderr: str
    wire: str
    transcript: str


def _read_tree(root: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in sorted(root.rglob("*")) if p.is_file())


def claude_args(prompt: str, session_id: str, model: str, settings_path: str) -> list[str]:
    return [
        "-p", prompt,
        "--session-id", session_id,
        "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        # Only the test's settings: no user hooks, no user or project settings files.
        "--setting-sources", "project",
        "--settings", settings_path,
        "--strict-mcp-config",
    ]


class HostSandbox:
    name = "host"
    subject_dir: str | None = None  # subject files are used where they are

    def __init__(self) -> None:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        self.projects = config_dir / "projects"

    def exec(self, sidecar: Sidecar, argv: list[str], project: Path) -> None:
        subprocess.run([*sidecar.host_cmd, *argv], cwd=project, check=True, capture_output=True,
                       env=clean_env(), timeout=300)

    def run(self, prompt, *, session_id, model, project, settings, subject_files, env, artifacts, timeout,
            keep_transcripts, interceptor: Interceptor | None = None) -> RawRun:
        settings_path = artifacts / "settings.json"
        settings_path.write_text(json.dumps(settings, indent=2))
        proxy = RecordingProxy().start()
        sidecar = None
        try:
            base_url = proxy.base_url
            if interceptor:
                sidecar, base_url = self._start_interceptor(interceptor, proxy.base_url, project, artifacts)
            proc = subprocess.run(
                [CLAUDE_BIN, *claude_args(prompt, session_id, model, str(settings_path))],
                cwd=project, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                env=clean_env({"ANTHROPIC_BASE_URL": base_url, **env}),
            )
        finally:
            if sidecar:
                sidecar.terminate()
                sidecar.wait(timeout=10)
            proxy.stop()
        paths = list(self.projects.glob(f"*/{session_id}*"))
        transcript = "\n".join(_read_tree(p) if p.is_dir() else p.read_text(errors="replace") for p in paths)
        if not keep_transcripts:
            for p in paths:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        return RawRun(proc.returncode, proc.stdout, proc.stderr, proxy.wire_text(), transcript)

    @staticmethod
    def _start_interceptor(icp: Interceptor, upstream: str, project: Path,
                           artifacts: Path) -> tuple[subprocess.Popen, str]:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        argv = [a.format(host="127.0.0.1", port=port) for a in icp.argv]
        proc = subprocess.Popen([*icp.sidecar.host_cmd, *argv], cwd=project, stdin=subprocess.DEVNULL,
                                stdout=(artifacts / "interceptor.log").open("w"), stderr=subprocess.STDOUT,
                                env=clean_env({icp.upstream_env: upstream}))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"interceptor exited: {(artifacts / 'interceptor.log').read_text()[-2000:]}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return proc, f"http://127.0.0.1:{port}"
            except OSError:
                time.sleep(0.2)
        proc.kill()
        raise RuntimeError("interceptor did not start listening")

    def close(self) -> None:
        pass


def _podman(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], check=check, capture_output=True, text=True, **kw)


def host_auth() -> tuple[str, dict[str, str]]:
    """The real credential for the proxy, and the dummy env for the sandbox.

    Order: BENCH_AUTH ("oauth:<token>" | "apikey:<key>"), ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN, then the claude.ai access token from
    ~/.claude/.credentials.json (read only, never refreshed here).
    """
    if v := os.environ.get("BENCH_AUTH"):
        auth = v
    elif v := os.environ.get("ANTHROPIC_API_KEY"):
        auth = f"apikey:{v}"
    elif v := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        auth = f"oauth:{v}"
    else:
        creds = Path.home() / ".claude" / ".credentials.json"
        if not creds.exists():
            raise RuntimeError("no credential: set BENCH_AUTH, ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        oauth = json.loads(creds.read_text())["claudeAiOauth"]
        left = oauth["expiresAt"] / 1000 - time.time()
        if left < 30 * 60:
            raise RuntimeError(f"claude.ai access token expires in {int(left / 60)} min: "
                               "run `claude -p hi` on the host to refresh it, then retry")
        auth = f"oauth:{oauth['accessToken']}"
    dummy = ({"CLAUDE_CODE_OAUTH_TOKEN": "bench-dummy-oauth-token"} if auth.startswith("oauth:")
             else {"ANTHROPIC_API_KEY": "sk-ant-bench-dummy-key"})
    return auth, dummy


def host_feature_cache() -> dict:
    """The host's cached feature flags, to seed the sandbox's config.

    Some tools (Monitor among them) are enabled by remotely fetched flags. The
    sandbox cannot fetch them (no egress), so without this seed it would run
    with fewer tools than the host. Only the flag cache is copied: no account
    data, no credentials.
    """
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home())) / ".claude.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: data[k] for k in ("cachedGrowthBookFeatures", "cachedGrowthBookFeaturesAt") if k in data}


class PodmanSandbox:
    name = "podman"
    subject_dir = "/opt/subject"
    INTERNAL = "leak-bench-internal"  # no egress: Claude Code lives here
    EGRESS = "leak-bench-egress"      # the proxy's way out
    PROXY_IMAGE = "leak-bench-proxy"
    UID = "1000"                      # the `bench` user in the image

    def __init__(self, claude_version: str) -> None:
        self.claude_image = f"leak-bench-claude:{claude_version}"
        self._build(self.claude_image, "claude.Containerfile", CLAUDE_VERSION=claude_version)
        self._build(self.PROXY_IMAGE, "proxy.Containerfile")
        if _podman("network", "exists", self.INTERNAL, check=False).returncode:
            _podman("network", "create", "--internal", self.INTERNAL)
        if _podman("network", "exists", self.EGRESS, check=False).returncode:
            _podman("network", "create", self.EGRESS)

        self.feature_cache = host_feature_cache()
        auth, self.dummy_env = host_auth()
        # The real credential stays in memory-backed storage and only the proxy sees it.
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self._secret_dir = Path(tempfile.mkdtemp(prefix="leak-bench-", dir=runtime))
        self.auth_file = self._secret_dir / "auth"
        self.auth_file.touch(mode=0o600)
        self.auth_file.write_text(auth)

    def _build(self, tag: str, containerfile: str, context: Path = ROOT, **build_args: str) -> None:
        if _podman("image", "exists", tag, check=False).returncode == 0:
            return
        args = [f"--build-arg={k}={v}" for k, v in build_args.items()]
        _podman("build", "-q", "-f", str(ROOT / "containers" / containerfile), *args, "-t", tag, str(context),
                timeout=1200)

    def _sidecar_image(self, sidecar: Sidecar) -> str:
        image = f"leak-bench-{sidecar.name}:{sidecar.tag}"
        self._build(image, sidecar.containerfile, context=sidecar.checkout)
        return image

    def _sidecar_args(self) -> list[str]:
        return [f"--userns=keep-id:uid={self.UID},gid={self.UID}", "--network", self.INTERNAL,
                "--cap-drop=all", "--security-opt=no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,U"]

    def exec(self, sidecar: Sidecar, argv: list[str], project: Path) -> None:
        _podman("run", "--rm", *self._sidecar_args(), "-v", f"{project}:/work:z", "-w", "/work",
                self._sidecar_image(sidecar), *argv, timeout=300)

    def close(self) -> None:
        shutil.rmtree(self._secret_dir, ignore_errors=True)

    def run(self, prompt, *, session_id, model, project, settings, subject_files, env, artifacts, timeout,
            keep_transcripts, interceptor: Interceptor | None = None) -> RawRun:
        work = Path(tempfile.mkdtemp(prefix="lb-", dir=artifacts.parent))
        config, records, staged = work / "claude-config", work / "records", work / "subject"
        for d in (config, records, staged):
            d.mkdir()
        (config / ".claude.json").write_text(json.dumps(self.feature_cache))
        for name, src in subject_files.items():
            (staged / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, staged / name)
        (staged / "settings.json").write_text(json.dumps(settings, indent=2))
        shutil.copy2(staged / "settings.json", artifacts / "settings.json")

        tag = session_id[:8]
        proxy_name, claude_name, icp_name = f"lb-proxy-{tag}", f"lb-claude-{tag}", f"lb-icp-{tag}"
        userns = f"--userns=keep-id:uid={self.UID},gid={self.UID}"
        _podman("run", "-d", "--rm", "--init", "--name", proxy_name, userns,
                "--network", f"{self.INTERNAL},{self.EGRESS}",
                "--cap-drop=all", "--security-opt=no-new-privileges", "--read-only",
                "-v", f"{self.auth_file}:/run/secrets/bench-auth:ro,z",
                "-v", f"{records}:/records:Z", self.PROXY_IMAGE)
        base_url = f"http://{proxy_name}:8080"
        try:
            self._wait_ready(proxy_name, "listening")
            if interceptor:
                argv = [a.format(host="0.0.0.0", port=8080) for a in interceptor.argv]
                # Same project path as Claude Code: it may locate the project from the system prompt.
                _podman("run", "-d", "--rm", "--init", "--name", icp_name, *self._sidecar_args(),
                        "-v", f"{project}:/work:ro,z", "-w", "/work",
                        "-e", f"{interceptor.upstream_env}={base_url}",
                        self._sidecar_image(interceptor.sidecar), *argv)
                self._wait_ready(icp_name, interceptor.ready)
                base_url = f"http://{icp_name}:8080"
            cmd = ["podman", "run", "--rm", "-i", "--name", claude_name, userns,
                   "--network", self.INTERNAL,
                   "--cap-drop=all", "--security-opt=no-new-privileges", "--read-only",
                   "--pids-limit=1024", "--memory=4g",
                   "--tmpfs", "/tmp:rw,U", "--tmpfs", "/home/bench:rw,U",
                   "-v", f"{config}:/home/bench/.claude:Z",
                   "-v", f"{project}:/work:z",
                   "-v", f"{staged}:{self.subject_dir}:ro,Z",
                   "-e", f"ANTHROPIC_BASE_URL={base_url}"]
            for k, v in {**self.dummy_env, **env}.items():
                cmd += ["-e", f"{k}={v}"]
            cmd += [self.claude_image, *claude_args(prompt, session_id, model, f"{self.subject_dir}/settings.json")]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                      stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                _podman("kill", claude_name, check=False)
                raise
        finally:
            if interceptor:
                logs = _podman("logs", icp_name, check=False)
                (artifacts / "interceptor.log").write_text(logs.stdout + logs.stderr)
                _podman("kill", icp_name, check=False)
            _podman("kill", proxy_name, check=False)

        wire_file = records / "wire.jsonl"
        wire = "\n".join(json.loads(line)["body"] for line in wire_file.read_text().splitlines()) \
            if wire_file.exists() else ""
        transcript = _read_tree(config / "projects") if (config / "projects").exists() else ""
        if keep_transcripts:
            shutil.copytree(config, artifacts / "claude-config", dirs_exist_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        return RawRun(proc.returncode, proc.stdout, proc.stderr, wire, transcript)

    @staticmethod
    def _wait_ready(name: str, line: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = _podman("logs", name, check=False)
            if line in logs.stdout + logs.stderr:
                return
            time.sleep(0.2)
        raise RuntimeError(f"container {name} did not start: {_podman('logs', name, check=False).stderr}")


def run_claude(prompt: str, *, sandbox, project: Path, settings: dict, subject_files: dict[str, Path],
               artifacts: Path, model: str, env: dict[str, str] | None = None, timeout: int = 600,
               keep_transcripts: bool = False, interceptor: Interceptor | None = None) -> Run:
    artifacts.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    raw = sandbox.run(prompt, session_id=session_id, model=model, project=project, settings=settings,
                      subject_files=subject_files, env=env or {}, artifacts=artifacts, timeout=timeout,
                      keep_transcripts=keep_transcripts, interceptor=interceptor)

    events = []
    for line in raw.stdout.splitlines():
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

    (artifacts / "stream.jsonl").write_text(raw.stdout)
    (artifacts / "stderr.txt").write_text(raw.stderr)
    (artifacts / "wire.txt").write_text(raw.wire)
    (artifacts / "transcript.jsonl").write_text(raw.transcript)
    return Run(session_id, raw.returncode, events, raw.stderr, raw.wire, raw.transcript, answer, tool_calls)
