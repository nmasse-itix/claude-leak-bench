"""Subjects: the Claude Code configurations under test.

A subject supplies the files it needs (e.g. a hook binary), the settings Claude
Code runs with (typically a hook) and, optionally, a way to declare known
secret values ahead of time. To test a new project, add a Subject here and
register it in SUBJECTS.

Files are copied into the sandbox: `settings(where)` receives, for each
`files()` name, the path the file is visible at (a container path under podman).
A name may contain `/` to lay out a directory tree.
Hook binaries must run in the sandbox image (Fedora minimal, glibc).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


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
    versions read. In host mode the global ~/.config/redacted files also
    apply; the podman sandbox has none, so 1.0.0 runs without learned secrets.
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


SUBJECTS: dict[str, Subject] = {s.name: s for s in [
    Bare(),
    Redacted(),
    Redacted("redacted-pr47", env_var="REDACTED_PR47_BIN", which=False),
    RedactedHeuristic(),
]}
