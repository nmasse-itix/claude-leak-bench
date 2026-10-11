"""End-to-end: does a canary secret leave the machine through Claude Code?"""

from __future__ import annotations

import pytest

from bench.cases import CASES
from bench.runner import cleanup_transcript, run_claude


def _cell(leaks: dict[str, bool]) -> str:
    where = ",".join(k[0] for k, v in leaks.items() if v)
    return f"LEAK({where})" if where else "clean"


def test_leak(subject, case_name, tmp_path, request, report_dir, record):
    opt = request.config.getoption
    row = dict(subject=subject.name, case=case_name, report_dir=str(report_dir))

    if reason := subject.available():
        record(**row, cell="skip")
        pytest.skip(reason)

    run, scenario, why = None, None, ""
    for attempt in range(1, opt("attempts") + 1):
        project = tmp_path / f"attempt{attempt}" / "project"
        project.mkdir(parents=True)
        scenario = CASES[case_name](project)
        if scenario.known and not subject.declare_known(project, [scenario.secret]):
            record(**row, cell="n/a")
            pytest.skip(f"{subject.name} cannot declare known values")

        if scenario.writer:
            scenario.writer.start()
        try:
            run = run_claude(
                scenario.prompt, cwd=project, settings=subject.settings(), model=opt("model"), env=scenario.env,
                artifacts=report_dir / subject.name / case_name / f"attempt{attempt}",
            )
        finally:
            if scenario.writer:
                scenario.writer.stop()
        if not opt("keep_transcripts"):
            cleanup_transcript(run.session_id)

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
