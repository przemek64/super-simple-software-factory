"""Adversarial two-pass PR review: one file per call, then a second mind audits.

Ported from the Archon workflow `archonx-fix-github-issue-p3a-4m3m3k-scoped`.
Three things in that workflow are worth keeping, and this module keeps all three:

  1. ONE FILE PER CALL. A reviewer handed a 40-file diff reads none of it
     properly and times out trying. The queue is a list of changed files and
     every call sees exactly one of them, with fresh context.

  2. A SECOND, DIFFERENT MIND. Pass 1 reviews the code; pass 2 reviews pass 1 —
     hunting for what pass 1 MISSED and for what it got WRONG. A reviewer
     auditing itself agrees with itself; that is not a review, it is an echo.

  3. COVERAGE THAT CANNOT BE FAKED. Completion is counted from the per-file
     artifacts actually on disk (`deep/*.md`), never from a model saying it is
     done. Archon learned this the hard way: a model echoed its own checklist
     without writing the analysis and the review "passed" having read nothing.

Files under `.archive/` are never reviewed — they are frozen version copies of
files the PR also changed live, so reviewing them pays twice for one set of
findings and files half of them against code nobody will edit again. The skip is
logged and recorded in the verdict, never silent.

What is deliberately NOT ported: Archon's self-fix half (p3b), the human pause,
and the frozen orphan-ref bundle. This ADW reviews and reports. It never edits
the code it is reviewing — the review agents have no `edit` tool and an empty
`writes` list, so "it cannot quietly fix what it should be reporting" is a
property of the config, not a promise in a prompt.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import session
from .data_types import AgentCall, GenericOutput, PhaseParams, ReviewOutput

# Beyond this, a single run stops being a review and becomes a bill. A PR this
# large should be split; failing loudly says so at minute one instead of hour two.
MAX_FILES = 60

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


# ── git / gh plumbing ────────────────────────────────────────────────────────

def _run(argv: list[str], *, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def current_repo(*, cwd: Path) -> str:
    return _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq",
                 ".nameWithOwner"], cwd=cwd)


def pr_meta(pr: int, *, repo: str, cwd: Path) -> tuple[str, str, int]:
    """(head branch, base branch, changed-file count GitHub reports)."""
    raw = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json",
                "headRefName,baseRefName,changedFiles"], cwd=cwd)
    data = json.loads(raw)
    if not data.get("headRefName"):
        raise RuntimeError(f"PR #{pr} has no head branch — is the number right?")
    return data["headRefName"], data["baseRefName"], int(data.get("changedFiles") or 0)


def make_worktree(branch: str, *, repo_root: Path, worktree_root: Path,
                  adw_id: str, pr: int) -> Path:
    """A fresh DETACHED checkout at the PR head — same shape as adw_coderabbit.

    Detached because git refuses one branch in two worktrees and a leftover SDLC
    tree on that branch is the normal case. Nothing here pushes, so no branch
    name is needed.
    """
    destination = (Path(worktree_root) / "pr_review" / f"pr{pr}-{adw_id}").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "fetch", "origin", branch], cwd=repo_root)
    _run(["git", "worktree", "add", "--detach", str(destination),
          f"origin/{branch}"], cwd=repo_root)
    return destination


def remove_worktree(path: Path, *, repo_root: Path) -> None:
    _run(["git", "worktree", "remove", "--force", str(path)], cwd=repo_root,
         check=False)


# Version copies under `.archive/` are frozen snapshots of files that also exist
# live in the tree. Reviewing them doubles the bill to report the same findings
# twice, and every finding lands on a file nobody will ever edit again.
EXCLUDED = re.compile(r"(^|/)\.archive/")


def changed_files(*, work_root: Path, repo_root: Path,
                  base: str) -> tuple[list[str], list[str]]:
    """(files to review, files skipped). Skipped is returned, never silent —
    a file dropped without a line in the trace is a file nobody knows was dropped."""
    _run(["git", "fetch", "origin", base], cwd=repo_root, check=False)
    out = _run(["git", "diff", "--name-only", "--diff-filter=d",
                f"origin/{base}...HEAD"], cwd=work_root)
    all_files = [line for line in out.splitlines() if line.strip()]
    keep = [f for f in all_files if not EXCLUDED.search(f)]
    skipped = [f for f in all_files if EXCLUDED.search(f)]
    return keep, skipped


def post_comment(pr: int, body_file: Path, *, repo: str, cwd: Path) -> None:
    _run(["gh", "pr", "comment", str(pr), "--repo", repo, "--body-file",
          str(body_file)], cwd=cwd)


# ── artifacts ────────────────────────────────────────────────────────────────

def slug(path: str) -> str:
    """`src/a.ts` -> `src__a__ts`. Matches Archon's `tr '/.' '__'`."""
    return re.sub(r"[/.]", "_", path)


class ReviewBundle:
    """Where every artifact of one review run lives, and what is in it so far."""

    def __init__(self, run) -> None:
        self.root = Path(run.context_handoff_dir) / "review"
        self.deep = self.root / "deep"
        self.audit = self.root / "audit"
        for directory in (self.root, self.deep, self.audit):
            directory.mkdir(parents=True, exist_ok=True)
        self.files_txt = self.root / "files.txt"
        self.consolidated = self.root / "consolidated-review.md"
        self.final_summary = self.root / "final-summary.md"
        self.verdict_json = self.root / "verdict.json"

    def record_queue(self, files: list[str]) -> None:
        self.files_txt.write_text("\n".join(files) + "\n", encoding="utf-8")

    def deep_path(self, file: str) -> Path:
        return self.deep / f"{slug(file)}.md"

    def audit_path(self, file: str) -> Path:
        return self.audit / f"{slug(file)}.md"

    def missing(self, files: list[str], *, pass_two: bool = False) -> list[str]:
        """Files with no analysis artifact — the un-fakeable coverage measure."""
        pick = self.audit_path if pass_two else self.deep_path
        return [f for f in files if not pick(f).is_file()]

    def severity_counts(self) -> dict[str, int]:
        """Count `## [SEVERITY]` headings across both passes' per-file reports."""
        counts = {s: 0 for s in SEVERITIES}
        for path in sorted(self.deep.glob("*.md")) + sorted(self.audit.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for severity in SEVERITIES:
                counts[severity] += len(re.findall(
                    rf"^##+\s*\[{severity}\]", text, re.M | re.I))
        return counts


# ── prompts handed to the per-file agents ────────────────────────────────────

REVIEW_TASK = """\
Review EXACTLY ONE file of pull request #{pr}. Ignore every other file in the diff.

file: {file}
base_ref: origin/{base}
diff command: git diff "origin/{base}...HEAD" -- "{file}"
write your findings to: {out}

Run BOTH framings and union the findings:
  1. ATTACK  — assume a bug EXISTS in the changed lines; construct a concrete
     counterexample (input, state, sequence) that breaks it. Run the code or a
     test if that is cheap.
  2. VERIFY  — try to PROVE each changed line correct. Anywhere you cannot, that
     inability IS the finding.

Cover every aspect: correctness/logic, error handling, test coverage (would the
tests fail if this code were wrong?), comment and docstring accuracy, docs
impact, security, performance, concurrency and shared state.

Write the file with this exact structure, one section per finding:

    # review: {file}
    ## [SEVERITY] Title            (CRITICAL | HIGH | MEDIUM | LOW)
    - **Location**: {file}:<line>
    - **Framing**: attack | verify
    - **Aspect**: <aspect>
    - **Evidence**: the concrete reasoning, command, or output vs expected
    - **Fix**: the concise fix

If the file is genuinely clean, write the heading and one line saying so, with
the reason you are confident. Do not invent a finding to look thorough, and do
not report style preferences the request never asked about.
"""

AUDIT_TASK = """\
You are a SECOND, INDEPENDENT reviewer auditing the FIRST reviewer's work on one
file of pull request #{pr}. You are not re-reviewing from scratch and you are not
here to agree.

file: {file}
base_ref: origin/{base}
the first reviewer's findings: {previous_findings}
the consolidated pass-1 review: {consolidated}
diff command: git diff "origin/{base}...HEAD" -- "{file}"
write your audit to: {out}

Do BOTH jobs:
  1. MISSES — find NET-NEW issues the first reviewer did NOT report for this
     file, with file:line and concrete evidence. Repeating a pass-1 finding is a
     failure of this job, not a contribution to it.
  2. FALSE POSITIVES — rule on each pass-1 finding for this file. If one is
     wrong, overstated, or graded too high, say so with the evidence that
     refutes it. An unjustified CRITICAL costs the team as much as a missed one.

Write the file with this exact structure:

    # audit: {file}
    ## NET-NEW
    ## [SEVERITY] Title — **Location**: {file}:<line> — **Evidence**: ... — **Fix**: ...
    ## REBUTTALS
    - <pass-1 finding> -> UPHELD | REFUTED (<evidence>)

If pass 1 was right and complete for this file, say exactly that under both
headings. Confirming good work is a real outcome; padding is not.
"""

SYNTHESIZE_TASK = """\
Aggregate the pass-1 per-file findings for pull request #{pr} into one report.

per-file findings: {deep}/*.md   ({count} file(s) reviewed)
write the report to: {consolidated}

Read every per-file report. Merge them, DEDUPLICATE (same file:line plus the
same claim is one finding), and order by severity. The report needs: an
executive summary, a verdict line reading exactly
`Verdict: APPROVE` or `Verdict: REQUEST_CHANGES` or `Verdict: NEEDS_DISCUSSION`,
a severity count table, then one section per severity. Every CRITICAL and HIGH
uses exactly this shape:

    ### <Title>
    **Location**: `<file>:<line>`
    **Problem**: <what is wrong>
    **Recommended Fix**: <the fix>
    **Why**: <the impact>

MEDIUM and LOW go in brief tables. Change nothing in the repository — you are
writing the report, not acting on it.

## Report

Respond with ONLY valid JSON matching `GenericOutput`:

    {{
      "status": "success",
      "summary": "<one sentence: N findings across M files, highest severity X>",
      "artifacts": ["{consolidated}"],
      "notes_for_next_agent": "<what the audit pass should press hardest on>"
    }}
"""

FINALIZE_TASK = """\
Write the closing summary of the two-pass review of pull request #{pr}.

pass-1 consolidated review: {consolidated}
pass-2 audit reports: {audit}/*.md
write the summary to: {final}

The summary states: total findings by severity across both passes; the NET-NEW
issues the audit found that pass 1 missed; the pass-1 findings the audit
REFUTED, with the audit's reason; and the final verdict as a line reading
`Verdict: APPROVE` or `Verdict: REQUEST_CHANGES` or `Verdict: NEEDS_DISCUSSION`.

Set `approved` in your JSON to true only when the final verdict is APPROVE, and
list every CRITICAL and HIGH finding that survived the audit in `blocking`.

## Report

Respond with ONLY valid JSON matching `ReviewOutput`:

    {{
      "status": "success",
      "approved": false,
      "summary": "<one sentence: the verdict and why>",
      "findings": [
        {{"requirement": "<the concern>", "met": true, "evidence": "<file:line — what settles it>"}}
      ],
      "blocking": ["<each CRITICAL/HIGH that survived the audit>"],
      "artifacts": ["{final}"],
      "notes_for_next_agent": "<what a human should look at first>"
    }}

`status` is `success` when the summary was written — it is not the verdict. The
verdict is `approved`.
"""


# ── the flow both variants share ─────────────────────────────────────────────

def review_pr(*, pr: int, reviewer: str, auditor: str, synthesizer: str,
              config: str, adw_id: str | None, repo: str | None,
              post: bool, keep_worktree: bool) -> int:
    """Two-pass adversarial review of one PR. Reports; never edits."""
    required = [reviewer, auditor, synthesizer]
    cfg, repo_root = session.bootstrap(config, required)
    repo = repo or current_repo(cwd=repo_root)
    branch, base, gh_changed = pr_meta(pr, repo=repo, cwd=repo_root)

    # This ADW owns its checkout (the PR head), so the shared
    # new-branch-from-a-base worktree machinery is switched off before the run
    # opens — same reason as adw_coderabbit.py.
    cfg.defaults.worktree.enabled = False
    run = session.ensure(cfg, adw_id, repo_root=repo_root,
                         prompt=f"Adversarially review PR #{pr}", base=None)
    if run.worktree_root is None:
        raise RuntimeError("worktree_root is not configured; this ADW needs somewhere "
                           "to put the PR head checkout. Set defaults.worktree_root.")
    work_root = make_worktree(branch, repo_root=repo_root,
                              worktree_root=run.worktree_root,
                              adw_id=run.adw_id, pr=pr)
    run.work_root = work_root
    bundle = ReviewBundle(run)
    clean = False

    try:
        with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                                   description="Name the pull request this run reviews "
                                               "and the two minds that will review it")) as ph:
            ph.log(pr=f"PR #{pr}", repo=repo, head=branch, base=base,
                   reviewer=reviewer, auditor=auditor, worktree=str(work_root))

        with run.phase(PhaseParams(name="scope", kind="code", owner="git",
                                   description="Build the file queue from the PR's own diff, "
                                               "and refuse to review nothing")) as ph:
            files, skipped = changed_files(work_root=work_root, repo_root=repo_root,
                                           base=base)
            bundle.record_queue(files)
            ph.log(files=len(files), skipped_archive=len(skipped),
                   github_reports=gh_changed, base=f"origin/{base}")
            if skipped:
                ph.log(excluded=", ".join(skipped))
            # The guard Archon needed twice: GitHub says the PR changed files but
            # the local diff is empty, so HEAD is not on the PR's work. Reviewing
            # nothing and calling it APPROVE is the worst outcome this ADW has.
            # Measured BEFORE the .archive filter — an all-archive PR is an empty
            # queue for a legitimate reason, and must not trip the collapse alarm.
            if gh_changed > 0 and not files and not skipped:
                raise RuntimeError(
                    f"PR #{pr} has {gh_changed} changed file(s) on GitHub but the local "
                    f"diff against origin/{base} is empty — HEAD is not on the PR's work. "
                    "Refusing to review nothing.")
            if not files:
                ph.log(outcome=f"nothing to review ({len(skipped)} .archive file(s) skipped)"
                       if skipped else "the PR changes no files — nothing to review")
                clean = True
            if len(files) > MAX_FILES:
                raise RuntimeError(
                    f"PR #{pr} changes {len(files)} files, over the {MAX_FILES}-file "
                    "limit for one review run. Split the PR.")

        if clean:
            return run.finish(accepted=True, reason="")

        # ── pass 1: one file, one call, fresh context ────────────────────────
        for i, file in enumerate(files, start=1):
            with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner=reviewer,
                                       retries=1,
                                       description=f"Attack and verify one changed file "
                                                   f"({i}/{len(files)}): {file}")) as ph:
                ph.call(AgentCall(output_type=GenericOutput, prompt=REVIEW_TASK.format(
                    pr=pr, file=file, base=base, out=bundle.deep_path(file))))

        with run.phase(PhaseParams(name="coverage_1", kind="code", owner="quality",
                                   description="Count the per-file reports actually written — "
                                               "coverage is measured, never claimed")) as ph:
            missing = bundle.missing(files)
            ph.log(reviewed=f"{len(files) - len(missing)}/{len(files)}",
                   missing=", ".join(missing) or "none")
            if missing:
                raise RuntimeError("pass 1 left files unreviewed (no report on disk): "
                                   + ", ".join(missing))

        with run.phase(PhaseParams(name="synthesize", kind="agent", owner=synthesizer,
                                   retries=1,
                                   description="Merge the per-file findings into one "
                                               "deduplicated, severity-ordered review")) as ph:
            ph.call(AgentCall(output_type=GenericOutput, prompt=SYNTHESIZE_TASK.format(
                pr=pr, deep=bundle.deep, count=len(files),
                consolidated=bundle.consolidated)))

        if post and bundle.consolidated.is_file():
            with run.phase(PhaseParams(name="post_review", kind="code", owner="git",
                                       description="Put the pass-1 review on the PR before the "
                                                   "audit runs, so it survives a later failure")) as ph:
                post_comment(pr, bundle.consolidated, repo=repo, cwd=repo_root)
                ph.log(posted=f"PR #{pr}", body=str(bundle.consolidated))

        # ── pass 2: a different mind audits pass 1, file by file ─────────────
        for i, file in enumerate(files, start=1):
            with run.phase(PhaseParams(name=f"audit_{i}", kind="agent", owner=auditor,
                                       retries=1,
                                       description=f"Audit pass 1 on one file for misses and "
                                                   f"false positives ({i}/{len(files)}): {file}")) as ph:
                ph.call(AgentCall(output_type=GenericOutput, prompt=AUDIT_TASK.format(
                    pr=pr, file=file, base=base, out=bundle.audit_path(file),
                    previous_findings=bundle.deep_path(file),
                    consolidated=bundle.consolidated)))

        with run.phase(PhaseParams(name="coverage_2", kind="code", owner="quality",
                                   description="Count the audit reports on disk, same "
                                               "un-fakeable measure as pass 1")) as ph:
            missing = bundle.missing(files, pass_two=True)
            ph.log(audited=f"{len(files) - len(missing)}/{len(files)}",
                   missing=", ".join(missing) or "none")
            if missing:
                raise RuntimeError("pass 2 left files unaudited (no report on disk): "
                                   + ", ".join(missing))

        with run.phase(PhaseParams(name="finalize", kind="agent", owner=synthesizer,
                                   retries=1,
                                   description="State the verdict after the audit: what stood, "
                                               "what was added, what was refuted")) as ph:
            verdict = ph.call(AgentCall(output_type=ReviewOutput, prompt=FINALIZE_TASK.format(
                pr=pr, consolidated=bundle.consolidated, audit=bundle.audit,
                final=bundle.final_summary)))

        with run.phase(PhaseParams(name="publish", kind="code", owner="git",
                                   description="Derive the machine-readable verdict from the "
                                               "artifacts and post the closing comment")) as ph:
            counts = bundle.severity_counts()
            record = {"schema": "sssf-review/1", "pr": pr, "repo": repo,
                      "head": branch, "base": base,
                      "reviewer": reviewer, "auditor": auditor,
                      "files": len(files),
                      "skipped_archive": skipped,
                      "reviewed": len(files) - len(bundle.missing(files)),
                      "audited": len(files) - len(bundle.missing(files, pass_two=True)),
                      "by_severity": counts,
                      "approved": bool(verdict.approved),
                      "blocking": verdict.blocking}
            bundle.verdict_json.write_text(json.dumps(record, indent=2), encoding="utf-8")
            if post:
                body = bundle.root / "closing-comment.md"
                summary = (bundle.final_summary.read_text(encoding="utf-8", errors="replace")
                           if bundle.final_summary.is_file() else verdict.summary)
                body.write_text(f"{summary}\n\n<!-- sssf-review\n"
                                f"{json.dumps(record, indent=2)}\n-->\n", encoding="utf-8")
                post_comment(pr, body, repo=repo, cwd=repo_root)
            ph.log(approved=verdict.approved, posted=bool(post), **{
                s.lower(): counts[s] for s in SEVERITIES})

        clean = True
        # A review that RAN is a successful run. REQUEST_CHANGES is the review
        # working, not the ADW failing — the same rule the tester phase follows.
        return run.finish(accepted=True, reason="")
    finally:
        # Kept on failure so the tree that produced it can be inspected.
        if clean and not keep_worktree:
            remove_worktree(work_root, repo_root=repo_root)
