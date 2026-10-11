"""End-to-end: does a canary secret leave the machine through Claude Code?"""

from __future__ import annotations

import pytest

from bench.cases import CASES
from bench.runner import run_claude


def _cell(leaks: dict[str, bool]) -> str:
    if leaks["wire"]:
        return "🔴"
    return "🟠" if leaks["transcript"] else "🟢"


def test_leak(subject, case_name, tmp_path, request, report_dir, record, sandbox):
    opt = request.config.getoption
    row = dict(subject=subject.name, case=case_name, report_dir=str(report_dir))

    if reason := subject.available():
        record(**row, cell="skip")
        pytest.skip(reason)

    files = subject.files()
    where = {name: f"{sandbox.subject_dir}/{name}" for name in files}

    run, scenario, why = None, None, ""
    for attempt in range(1, opt("attempts") + 1):
        project = tmp_path / f"attempt{attempt}" / "project"
        project.mkdir(parents=True)
        scenario = CASES[case_name](project)
        if scenario.known and not subject.declare_known(project, [scenario.secret]):
            record(**row, cell="n/a")
            pytest.skip(f"{subject.name} cannot declare known values")
        subject.prepare(project, lambda sidecar, argv: sandbox.exec(sidecar, argv, project))

        if scenario.writer:
            scenario.writer.start()
        try:
            run = run_claude(
                scenario.prompt, sandbox=sandbox, project=project, settings=subject.settings(where),
                subject_files=files, model=opt("model"), env=scenario.env,
                artifacts=report_dir / subject.name / case_name / f"attempt{attempt}",
                keep_transcripts=opt("keep_transcripts"), interceptor=subject.interceptor(),
            )
        finally:
            if scenario.writer:
                scenario.writer.stop()

        ok, why = scenario.check(run)
        if ok:
            break
    else:
        record(**row, cell="?")
        pytest.skip(f"inconclusive after {opt('attempts')} attempt(s): {why}")

    leaks = run.leaks(scenario.secret)
    record(**row, cell=_cell(leaks), leaks=leaks, session_id=run.session_id)

    if subject.expect == "leak":
        assert leaks["wire"], "control did not leak: the bench is not detecting anything"
    else:
        assert not leaks["wire"] and not leaks["transcript"], f"secret leaked: {leaks}"
