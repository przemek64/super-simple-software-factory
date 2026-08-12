"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def _work_path(run, value: str) -> Path:
    """Resolve agent-declared repo artifacts from the isolated work root."""
    path = Path(value)
    return path if path.is_absolute() else Path(run.work_root) / path


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _work_path(run, a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _work_path(run, a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _work_path(run, a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must exist on disk."""
    report = GateReport()
    for f in getattr(envelope, "changed_files", []):
        p = _work_path(run, f)
        report.check(f, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "claimed changed file does not exist")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def triage_covers_every_finding(review):
    """Gate factory: triage must rule on every captured finding, and only those.

    The captured review is a frozen contract (see adw_modules/coderabbit.py), so
    the ruling set is knowable exactly. A finding that is silently dropped is
    the failure mode this exists to stop: it looks identical to a rejection, but
    nobody ever decided anything about it.
    """
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        report = GateReport()
        expected = {f.finding_id: f for f in review.findings}
        verdicts = list(getattr(envelope, "verdicts", []))
        ruled = [v.finding_id for v in verdicts]

        for finding_id, finding in expected.items():
            count = ruled.count(finding_id)
            report.check(f"{finding.location} [{finding_id}]", count == 1,
                         "ruled once" if count == 1
                         else "no verdict — every finding must be accepted or rejected"
                         if count == 0 else f"ruled {count} times")

        for finding_id in set(ruled) - set(expected):
            report.check(f"unknown finding [{finding_id}]", False,
                         "verdict for a finding that is not in the captured review")

        # A rejection without a reason is not a decision, it is a skip with
        # better manners. Acceptances need no justification — the finding is it.
        for verdict in verdicts:
            if verdict.accepted:
                continue
            report.check(f"reason for [{verdict.finding_id}]", bool(verdict.reason.strip()),
                         "rejection is explained" if verdict.reason.strip()
                         else "rejected with no reason given")
        return report

    gate.__name__ = f"triage_covers_every_finding({len(review.findings)} findings)"
    return gate


def accepted_findings_touched(review, accepted_ids: list[str]):
    """Gate factory: every accepted finding's file must appear in the build's diff.

    Deliberately mechanical and deliberately shallow. Whether the fix is CORRECT
    is the reviewer's judgement; this only refutes the claim that cannot survive
    a file listing — an accepted finding in a file the builder never opened.
    """
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        report = GateReport()
        changed = {str(Path(f).as_posix()) for f in getattr(envelope, "changed_files", [])}
        by_id = {f.finding_id: f for f in review.findings}

        for finding_id in accepted_ids:
            finding = by_id.get(finding_id)
            if finding is None or not finding.path:
                continue        # nothing anchored to a file to check against
            wanted = str(Path(finding.path).as_posix())
            hit = any(candidate.endswith(wanted) or wanted.endswith(candidate)
                      for candidate in changed)
            report.check(f"{finding.location} [{finding_id}]", hit,
                         "file is in the diff" if hit
                         else f"accepted finding, but {finding.path} was not changed")
        return report

    gate.__name__ = f"accepted_findings_touched({len(accepted_ids)} accepted)"
    return gate


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, cwd=run.work_root,
                                capture_output=True, text=True)
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
