"""Subjects: the Claude Code configurations under test.

A subject supplies the files it needs (e.g. a hook binary), the settings Claude
Code runs with (typically a hook) and, optionally, a way to declare known
secret values ahead of time. To test a new project, add a Subject here and
register it in SUBJECTS.

Files are copied into the sandbox: `settings(where)` receives, for each
`files()` name, the path the file is visible at (a container path under podman).
A name may contain `/` to lay out a directory tree.
Hook binaries must run in the sandbox image (Fedora minimal, glibc).

A subject that is not a hook (e.g. a redacting API proxy) runs as a sidecar:
a container whose image is built from its checkout. `prepare()` may run it once in the project (setup steps), and
`interceptor()` inserts it between Claude Code and the recording proxy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Runs a sidecar command with the project as working directory.
RunSidecar = Callable[["Sidecar", list[str]], None]


@dataclass
class Sidecar:
    """A program the subject runs next to Claude Code, from a local checkout."""
    name: str            # podman image: leak-bench-<name>:<tag>
    checkout: Path       # build context
    containerfile: str   # in containers/

    @property
    def tag(self) -> str:
        """The checkout's commit, or a stash commit when it has local changes."""
        git = ["git", "-C", str(self.checkout)]
        rev = subprocess.run([*git, "stash", "create"], capture_output=True, text=True).stdout.strip()
        rev = rev or subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                                    check=True).stdout.strip()
        return rev[:12]


@dataclass
class Interceptor:
    """A sidecar proxy between Claude Code and the recording proxy."""
    sidecar: Sidecar
    argv: list[str]      # must listen on 0.0.0.0:8080
    upstream_env: str    # env var that receives the recording proxy's base URL
    ready: str           # log line printed once it listens


@dataclass
class Subject:
    name: str
    # "leak": a control, the canary must leak (proves the bench detects leaks).
    # "clean": a protection, the canary must not leave the machine.
    expect: str = "clean"

    def available(self) -> str | None:
        """None when usable, else the reason to skip."""
        return None

    def files(self) -> dict[str, Path]:
        return {}

    def settings(self, where: dict[str, str]) -> dict:
        return {}

    def declare_known(self, project: Path, values: list[str]) -> bool:
        """Tell the subject about values to always redact. False if unsupported."""
        return False

    def prepare(self, project: Path, run: RunSidecar) -> None:
        """Set the project up, after the case and `declare_known` wrote theirs."""

    def interceptor(self) -> Interceptor | None:
        return None


class Bare(Subject):
    def __init__(self) -> None:
        super().__init__("bare", expect="leak")

    def declare_known(self, project: Path, values: list[str]) -> bool:
        return True  # nothing to declare: the control leaks either way


class Redacted(Subject):
    """github.com/svn-arv/redacted: a PostToolUse hook, `redacted scrub`.

    `redacted` is the Go version (0.x, `main`), `redacted-pr47` the Rust
    rewrite of svn-arv/redacted#47 (1.0.0). Known values are declared as a
    literal custom pattern in the project's .redacted.engine.yml, which both
    versions read. The sandbox has no global ~/.config/redacted, so 1.0.0 runs
    without learned secrets.
    """

    def __init__(self, name: str = "redacted", env_var: str = "REDACTED_BIN", which: bool = True) -> None:
        super().__init__(name)
        self.env_var = env_var
        self.binary = os.environ.get(env_var) or (which and shutil.which("redacted")) or ""

    def available(self) -> str | None:
        if not self.binary or not Path(self.binary).exists():
            return f"{self.name} binary not found (set {self.env_var})"
        return None

    def files(self) -> dict[str, Path]:
        return {"redacted": Path(self.binary)}

    def settings(self, where: dict[str, str]) -> dict:
        cmd = f"{where['redacted']} scrub"
        return {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]}}

    def declare_known(self, project: Path, values: list[str]) -> bool:
        # No \Q...\E: Rust's regex (1.0.0) lacks it. Escaping punctuation works in both.
        assert not any("'" in v or re.search(r"\s", v) for v in values), "value cannot be quoted literally"
        regex = "|".join(re.sub(r"([^0-9A-Za-z_])", r"\\\1", v) for v in values)
        (project / ".redacted.engine.yml").write_text(f"patterns:\n  - name: bench_known\n    regex: '{regex}'\n")
        return True


class RedactedHeuristic(Redacted):
    """redacted 1.0.0 with the entropy heuristic on (`heuristic.enabled: true`).

    It reads the heuristic from the global config only, so the hook runs with a
    HOME of its own that holds .config/redacted/config.yaml. Stats still go to
    the real HOME.
    """

    CONFIG = "home/.config/redacted/config.yaml"

    def __init__(self) -> None:
        super().__init__("redacted-pr47-heuristic", env_var="REDACTED_PR47_BIN", which=False)
        self._config: Path | None = None

    def files(self) -> dict[str, Path]:
        if self._config is None:
            self._config = Path(tempfile.mkdtemp(prefix="leak-bench-redacted-")) / ".config/redacted/config.yaml"
            self._config.parent.mkdir(parents=True)
            self._config.write_text("version: 2\nheuristic:\n  enabled: true\n")
        return {**super().files(), self.CONFIG: self._config}

    def settings(self, where: dict[str, str]) -> dict:
        home = where[self.CONFIG].removesuffix("/.config/redacted/config.yaml")
        cmd = (f'REDACTED_STATS_FILE="$HOME/.config/redacted/stats.jsonl" HOME={home} '
               f"{where['redacted']} scrub")
        return {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]}}


class RdxProxy(Subject):
    """github.com/paroque28/claude-code-redact (`rdx`) in proxy mode.

    An API proxy: it redacts requests on their way to the API and restores the
    real values in responses, so the transcript on disk keeps the secret. It
    only redacts for projects with a .redaction_rules file, found through the
    "Primary working directory" of the system prompt. `prepare` performs the
    file steps of `rdx setup --proxy` (deny permissions in
    .claude/settings.json, RDX.md, CLAUDE.md) with rdx's own code. Checkout from
    RDX_SRC.
    """

    SETUP = ("from pathlib import Path; from rdx.setup import setup as s; p = Path.cwd(); "
             "f = p / s.SETTINGS_FILE; c = s._read_settings(f); s._remove_rdx_hooks(c); "
             "s._merge_deny_permissions(c); s._write_settings(f, c); "
             "s.write_rdx_md(p, mode='proxy'); s._ensure_claude_md_includes_rdx(p)")

    def __init__(self) -> None:
        super().__init__("rdx-proxy")
        src = os.environ.get("RDX_SRC")
        self.sidecar = Sidecar("rdx", Path(src).expanduser() if src else Path(), "rdx.Containerfile")

    def available(self) -> str | None:
        if not os.environ.get("RDX_SRC") or not (self.sidecar.checkout / "src/rdx").is_dir():
            return "claude-code-redact checkout not found (set RDX_SRC)"
        return None

    def declare_known(self, project: Path, values: list[str]) -> bool:
        assert not any("'" in v for v in values), "value cannot be quoted literally"
        rules = "".join(f"  - id: bench-known-{i}\n    pattern: '{v}'\n    is_regex: false\n    category: KEY\n"
                        for i, v in enumerate(values))
        (project / ".redaction_rules").write_text("rules:\n" + rules)
        return True

    def prepare(self, project: Path, run: RunSidecar) -> None:
        rules = project / ".redaction_rules"
        if not rules.exists():
            rules.write_text("rules: []\n")  # without it, rdx passes everything through
        run(self.sidecar, ["python", "-c", self.SETUP])

    def interceptor(self) -> Interceptor:
        return Interceptor(self.sidecar,
                           ["python", "-m", "uvicorn", "rdx.proxy.server:app", "--host", "0.0.0.0", "--port", "8080"],
                           upstream_env="RDX_UPSTREAM_URL", ready="Uvicorn running")


SUBJECTS: dict[str, Subject] = {s.name: s for s in [
    Bare(),
    Redacted(),
    Redacted("redacted-pr47", env_var="REDACTED_PR47_BIN", which=False),
    RedactedHeuristic(),
    RdxProxy(),
]}
