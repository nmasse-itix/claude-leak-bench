from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from bench.cases import CASES
from bench.subjects import SUBJECTS

ROOT = Path(__file__).parent
RESULTS: list[dict] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("leak bench")
    g.addoption("--subject", action="append", default=[],
                help=f"subject to test, repeatable (default: all). Known: {', '.join(SUBJECTS)}")
    g.addoption("--case", action="append", default=[],
                help=f"case to run, repeatable (default: all). Known: {', '.join(CASES)}")
    g.addoption("--model", default="claude-haiku-4-5-20251001", help="model for the Claude Code runs")
    g.addoption("--attempts", type=int, default=2,
                help="retries when the model does not exercise the scenario (inconclusive run)")
    g.addoption("--keep-transcripts", action="store_true",
                help="keep the container's ~/.claude directory in the report")
    g.addoption("--claude-version", default=None,
                help="Claude Code version for the sandbox image (default: the host's `claude --version`)")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if {"subject", "case_name"} <= set(metafunc.fixturenames):
        subjects = metafunc.config.getoption("subject") or list(SUBJECTS)
        cases = metafunc.config.getoption("case") or list(CASES)
        unknown = [s for s in subjects if s not in SUBJECTS] + [c for c in cases if c not in CASES]
        if unknown:
            raise pytest.UsageError(f"unknown subject/case: {', '.join(unknown)}")
        metafunc.parametrize(
            "subject,case_name",
            [(SUBJECTS[s], c) for s in subjects for c in cases],
            ids=[f"{s}-{c}" for s in subjects for c in cases],
        )


@pytest.fixture(scope="session")
def sandbox(request):
    from bench.runner import Sandbox

    version = request.config.getoption("claude_version")
    if not version:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout
        m = re.search(r"\d+\.\d+\.\d+", out)
        if not m:
            raise pytest.UsageError("cannot detect the host Claude Code version: pass --claude-version")
        version = m.group(0)
    try:
        sb = Sandbox(version)
    except RuntimeError as e:
        pytest.exit(str(e), returncode=2)
    yield sb
    sb.close()


@pytest.fixture(scope="session")
def report_dir() -> Path:
    d = ROOT / "reports" / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True)
    return d


@pytest.fixture
def record():
    def _record(**row):
        RESULTS.append(row)
    return _record


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if not RESULTS:
        return
    subjects = list(dict.fromkeys(r["subject"] for r in RESULTS))
    cases = list(dict.fromkeys(r["case"] for r in RESULTS))
    cell = {(r["subject"], r["case"]): r["cell"] for r in RESULTS}

    lines = ["| case | " + " | ".join(subjects) + " |", "|---" * (len(subjects) + 1) + "|"]
    for c in cases:
        lines.append(f"| {c} | " + " | ".join(cell.get((s, c), "") for s in subjects) + " |")
    legend = ("🔴 = canary sent to the Anthropic API. 🟠 = not sent to the API, but written to the "
              "transcript on disk. 🟢 = neither. ? = inconclusive (scenario not exercised). "
              "n/a = subject cannot declare known values. skip = subject not available.")

    terminalreporter.write_sep("=", "leak matrix")
    for line in lines:
        terminalreporter.write_line(line)
    terminalreporter.write_line(legend)

    out = Path(RESULTS[0]["report_dir"])
    (out / "matrix.md").write_text("\n".join(lines) + "\n\n" + legend + "\n")
    (out / "results.json").write_text(json.dumps(RESULTS, indent=2))
    terminalreporter.write_line(f"report: {out}")
