"""Subjects: the Claude Code configurations under test.

A subject supplies the files it needs (e.g. a hook binary), the settings Claude
Code runs with (typically a hook) and, optionally, a way to declare known
secret values ahead of time. To test a new project, add a Subject here and
register it in SUBJECTS.

Files are copied into the sandbox: `settings(where)` receives, for each
`files()` name, the path the file is visible at (a container path under podman).
Hook binaries must run in the sandbox image (Fedora minimal, glibc).
"""

from __future__ import annotations

import os
import shutil
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
    """github.com/svn-arv/redacted (Go, 0.x): a PostToolUse hook, `redacted scrub`.

    Known values are declared as a literal custom pattern in the project's
    .redacted.engine.yml. In host mode the global ~/.config/redacted files
    also apply; the podman sandbox has none.
    """

    def __init__(self, name: str = "redacted", binary: str | None = None) -> None:
        super().__init__(name)
        self.binary = binary or os.environ.get("REDACTED_BIN") or shutil.which("redacted") or ""

    def available(self) -> str | None:
        if not self.binary or not Path(self.binary).exists():
            return "redacted binary not found (set REDACTED_BIN)"
        return None

    def files(self) -> dict[str, Path]:
        return {"redacted": Path(self.binary)}

    def settings(self, where: dict[str, str]) -> dict:
        cmd = f"{where['redacted']} scrub"
        return {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]}}

    def declare_known(self, project: Path, values: list[str]) -> bool:
        assert not any("\\E" in v or "'" in v for v in values), "value cannot be quoted literally"
        regex = "|".join(r"\Q" + v + r"\E" for v in values)
        (project / ".redacted.engine.yml").write_text(f"patterns:\n  - name: bench_known\n    regex: '{regex}'\n")
        return True


SUBJECTS: dict[str, Subject] = {s.name: s for s in [Bare(), Redacted()]}
