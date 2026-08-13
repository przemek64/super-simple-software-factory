"""Two-axis PR review: Standards and Spec, judged separately, never merged.

Ported from the Archon workflow `archonx-pr-review-p4-2axis-m3`, which in turn
ports the two-axis idea from the `engineering/code-review` skill. The whole point
is the separation: one axis asks "is this written the way code is written here",
the other asks "is this what the issue actually asked for". Answered by one mind
in one pass, the loud answer masks the quiet one — a diff with beautiful style
and the wrong feature reads as a good review, and so does the reverse.

Read-only. Neither axis has an edit tool or repository write access.

THREE THINGS THIS DOES DIFFERENTLY FROM THE ARCHON ORIGINAL:

  * Findings ride in the typed envelope, not in a JSON file the agent writes.
    Archon parsed `axis/*.json` with a bare `except: return []`, so a malformed
    reply became "no findings" — a parse slip and a clean bill of health looked
    identical. Here a bad reply is re-prompted, and a clean axis is clean.

  * The aggregation is code, not an agent. Archon spent an LLM call assembling
    two reports under two headings and telling it not to rerank across axes. A
    concatenation cannot rerank, cannot invent, and costs nothing.

  * The axes run in sequence, not in parallel — SSSF phases are sequential.
    Nothing is lost: what protects the axes from each other is separate context,
    which each phase already gets. It is slower, not weaker.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import session
from .data_types import AgentCall, AxisOutput, PhaseParams
from .pr_review import EXCLUDED, _run, current_repo, make_worktree, post_comment, remove_worktree

# Docs that describe how code is written here. Read from the BASE branch: a PR
# may not rewrite the standard it is about to be judged against.
STANDARDS_DOCS = ("CODING_STANDARDS.md", "CONTRIBUTING.md", "CLAUDE.md",
                  "STYLE.md", "AGENTS.md", "docs/CODING_STANDARDS.md")

# Spec documents cited by an issue or PR body, in this repo's vocabulary.
SPEC_REF = re.compile(r"(docs/adr/[0-9A-Za-z._/-]+\.md"
                      r"|[A-Za-z0-9._/-]*PRD[A-Za-z0-9._/-]*\.md"
                      r"|[A-Za-z0-9._/-]*-spec\.md"
                      r"|GLOSSARY\.md)")

LINKED_ISSUE = re.compile(r"\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s+#(\d+)",
                          re.I)
ANY_ISSUE = re.compile(r"#(\d+)")


class Sources:
    """Everything both axes read, gathered once, on disk, bounded."""

    def __init__(self, run) -> None:
        self.root = Path(run.context_handoff_dir) / "two_axis"
        self.spec = self.root / "spec"
        self.std = self.root / "std"
        for directory in (self.root, self.spec, self.std):
            directory.mkdir(parents=True, exist_ok=True)
        self.files_txt = self.root / "files.txt"
        self.diff_patch = self.root / "diff.patch"
        self.commits_txt = self.root / "commits.txt"
        self.issue_body = self.spec / "issue-body.md"
        self.standards_md = self.std / "standards.md"
        self.spec_md = self.spec / "spec.md"
        self.review_md = self.root / "review.md"
        self.verdict_json = self.root / "verdict.json"

    @property
    def has_spec(self) -> bool:
        """A spec axis with no spec has nothing to be faithful to."""
        return (self.issue_body.is_file() and self.issue_body.stat().st_size > 0) or \
               any(p.name != "issue-body.md" and p.suffix == ".md" for p in self.spec.iterdir())


def linked_issue(body: str) -> str:
    """The issue this PR closes — a closing keyword first, any reference second."""
    match = LINKED_ISSUE.search(body or "") or ANY_ISSUE.search(body or "")
    return match.group(1) if match else ""


def gather(*, pr: int, repo: str, repo_root: Path, work_root: Path, base: str,
           head_sha: str, sources: Sources) -> tuple[list[str], list[str], str]:
    """Write the diff, the standards docs and the spec docs. Returns (files, skipped, issue)."""
    _run(["git", "fetch", "origin", base], cwd=repo_root, check=False)

    listed = _run(["git", "diff", "--name-only", "--diff-filter=d",
                   f"origin/{base}...{head_sha}"], cwd=work_root).splitlines()
    all_files = [f for f in listed if f.strip()]
    files = [f for f in all_files if not EXCLUDED.search(f)]
    skipped = [f for f in all_files if EXCLUDED.search(f)]

    # Pinned by SHA, and scoped to the reviewed files so the .archive copies stay
    # out of the patch the axes read — not just out of the file list.
    patch = _run(["git", "diff", f"origin/{base}...{head_sha}", "--"] + files,
                 cwd=work_root) if files else ""
    sources.diff_patch.write_text(patch + "\n", encoding="utf-8")
    sources.files_txt.write_text("\n".join(files) + "\n", encoding="utf-8")
    sources.commits_txt.write_text(
        _run(["git", "log", f"origin/{base}..{head_sha}", "--oneline"],
             cwd=work_root, check=False) + "\n", encoding="utf-8")

    pr_body = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "body",
                    "--jq", ".body"], cwd=repo_root, check=False)
    issue = linked_issue(pr_body)
    issue_body = ""
    if issue:
        issue_body = _run(["gh", "issue", "view", issue, "--repo", repo, "--json",
                           "body", "--jq", ".body"], cwd=repo_root, check=False)
    sources.issue_body.write_text(issue_body, encoding="utf-8")

    for ref in sorted(set(SPEC_REF.findall(f"{issue_body}\n{pr_body}"))):
        text = _run(["git", "show", f"origin/{base}:{ref}"], cwd=work_root, check=False)
        if text:
            (sources.spec / ref.replace("/", "_")).write_text(text, encoding="utf-8")

    for doc in STANDARDS_DOCS:
        text = _run(["git", "show", f"origin/{base}:{doc}"], cwd=work_root, check=False)
        if text:
            (sources.std / doc.replace("/", "_")).write_text(text, encoding="utf-8")

    return files, skipped, issue


# ── the two axis tasks ───────────────────────────────────────────────────────

STANDARDS_TASK = """\
You are the STANDARDS axis of a two-axis review of pull request #{pr}. Judge ONLY
whether this diff is written the way code is written in this repository. Whether
it implements the right feature is the other axis's question — do not answer it.

diff (authoritative): {diff}
changed files: {files}
commits: {commits}
documented standards: {std}/*.md  (read every one; may be empty)
write your prose report to: {report}

A file's full contents at the PR head, when a hunk is not enough:
`git show {head_sha}:<path>` — do NOT read the file from the working tree.

Documented repo standards OVERRIDE the smell baseline in your instructions. Where
a documented standard endorses something the baseline would flag, the baseline
loses and you report nothing. Skip anything a linter, formatter or type checker
already enforces — a review that repeats tooling wastes the reader's attention.

Keep the report under 400 words. Cite the standard (file plus the rule) for every
documented violation, and quote the hunk for every baseline smell.

## Report

Respond with ONLY valid JSON matching `AxisOutput`:

    {{
      "status": "success",
      "axis": "standards",
      "summary": "<one sentence: N findings, worst severity X>",
      "findings": [
        {{"kind": "documented", "severity": "HIGH", "title": "<short>",
          "location": "<file:line>", "evidence": "<the standard, quoted>"}},
        {{"kind": "smell", "severity": "LOW", "title": "possible Feature Envy",
          "location": "<file:line>", "evidence": "<the hunk, quoted>"}}
      ],
      "report_path": "{report}",
      "artifacts": ["{report}"],
      "notes_for_next_agent": "<what a human should look at first>"
    }}

`findings` is `[]` when the diff is clean — say so and mean it. Baseline smells
are LOW or MEDIUM by nature; CRITICAL and HIGH are for documented-standard
breaches that actually break the code or a hard project rule.
"""

SPEC_TASK = """\
You are the SPEC axis of a two-axis review of pull request #{pr}. Judge ONLY
whether this diff faithfully implements what the originating issue and its cited
documents asked for. Code taste and style are the other axis's question — do not
answer it.

spec available: {has_spec}
issue: {issue}
issue body: {issue_body}
cited documents: {spec}/*.md  (ADRs, PRD, *-spec, GLOSSARY — read every one)
diff (authoritative): {diff}
changed files: {files}
write your prose report to: {report}

A file's full contents at the PR head: `git show {head_sha}:<path>` — do NOT read
the file from the working tree.

If `spec available` is false, write the report saying exactly `no spec available`,
return `findings: []`, and stop. An absent spec is not a passing spec, and it is
not your job to invent one.

An ADR owns its decision — polarity, ordering, encoding, recovery semantics. Read
the ADR itself, never the issue's summary of it. Do not invent requirements the
spec does not state; a requirement you wish had been written is not a finding.

Report three things, quoting the spec line for each: requirements that are MISSING
or partial; behaviour in the diff that was NOT asked for (scope creep); and
requirements that look implemented but are implemented WRONG. Keep it under 400
words.

## Report

Respond with ONLY valid JSON matching `AxisOutput`:

    {{
      "status": "success",
      "axis": "spec",
      "summary": "<one sentence: N findings, worst severity X>",
      "findings": [
        {{"kind": "missing", "severity": "HIGH", "title": "<short>",
          "location": "<file:line or spec reference>",
          "evidence": "<the spec line, quoted verbatim>"}}
      ],
      "report_path": "{report}",
      "artifacts": ["{report}"],
      "notes_for_next_agent": "<what a human should look at first>"
    }}

`kind` is one of `missing`, `scope-creep`, `wrong`. `findings` is `[]` when the
diff is faithful to the spec.
"""


# ── assembly, verdict, posting — all deterministic ───────────────────────────

# The machine-readable ledger is fenced by sentinels, not by a heading.
# An axis writes its own prose report and one of them wrote its own "## Findings"
# section — so a heading-anchored parser read the agent's "None." and stopped
# before the real ledger. These render as nothing on GitHub and no reviewer
# writing prose will emit them by accident. Kept identical to the constants in
# adw_modules/coderabbit.py, which is the only thing that reads them back.
LEDGER_BEGIN = "<!-- sssf:two-axis-findings:begin -->"
LEDGER_END = "<!-- sssf:two-axis-findings:end -->"


def _finding_id(axis: str, location: str, title: str) -> str:
    """Stable id for one axis finding.

    Same scheme and same width as coderabbit._finding_id, because both ids end up
    in one ruling set and the triager should not be able to tell from an id which
    reviewer it came from. Content-derived rather than positional: re-running an
    axis over an unchanged diff must produce the same ids, or a re-read would
    look like a fresh set of findings.
    """
    raw = f"{axis}|{location}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def assign_ids(axis: AxisOutput) -> AxisOutput:
    """Give every finding on this axis its id, in place.

    Called after the axis replies and before anything renders it. Duplicate
    (location, title) pairs would collide onto one id and the coverage gate would
    then demand a single ruling for what the axis reported twice, so a repeat gets
    a discriminator rather than being silently merged.
    """
    seen: dict[str, int] = {}
    for finding in axis.findings:
        base = _finding_id(axis.axis, finding.location, finding.title)
        count = seen.get(base, 0)
        seen[base] = count + 1
        finding.finding_id = base if count == 0 else _finding_id(
            axis.axis, finding.location, f"{finding.title}#{count}")
    return axis


def _ledger(standards: AxisOutput, spec: AxisOutput) -> str:
    """Every finding from both axes, one heading each, carrying its id.

    This is the half of the posted review that a machine reads. The prose reports
    above it are what a human reads, and they stay exactly as the axes wrote them;
    this section exists so the fix run can recover a finding SET from a PR comment
    rather than a wall of text. The `### [id]` shape matches the rabbit handoff
    format on purpose — one parser shape, one ruling vocabulary.
    """
    lines = [LEDGER_BEGIN, "",
             "## Findings (the ruling set)", "",
             "One heading per finding, from both axes. The id is stable for the "
             "same finding on the same diff; rule on every one of them.", ""]
    pairs = [("standards", standards), ("spec", spec)]
    if not any(axis.findings for _, axis in pairs):
        lines += ["_No findings on either axis._", "", LEDGER_END, ""]
        return "\n".join(lines)
    for name, axis in pairs:
        for finding in axis.findings:
            lines += [
                f"### [{finding.finding_id}] {finding.title or '(untitled)'}", "",
                f"- **axis**: {name}",
                f"- **severity**: {finding.severity}",
                f"- **kind**: {finding.kind}",
                f"- **location**: {finding.location or '(no location)'}",
            ]
            if finding.evidence:
                # One line: the fence is a record, not the report. A multi-line
                # quote here would put "## " and "### " from the diff inside the
                # ledger and break the very framing that makes it parseable.
                lines += [f"- **evidence**: {' '.join(finding.evidence.split())[:300]}"]
            lines += [""]
    lines += [LEDGER_END, ""]
    return "\n".join(lines)


def assemble(sources: Sources, *, pr: int, issue: str,
             standards: AxisOutput, spec: AxisOutput) -> None:
    """Concatenate the two reports under two headings. Code, not an agent:
    a concatenation cannot rerank one axis against the other or invent a finding."""
    def report(axis: AxisOutput, fallback: Path) -> str:
        path = Path(axis.report_path) if axis.report_path else fallback
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
        return axis.summary or "(this axis produced no report)"

    def worst(axis: AxisOutput) -> str:
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            hit = [f for f in axis.findings if f.severity == severity]
            if hit:
                return f"{severity} — {hit[0].title} ({hit[0].location})"
        return "nothing found"

    sources.review_md.write_text(
        f"# PR Two-Axis Review: PR #{pr}"
        + (f" (issue #{issue})" if issue else "")
        + "\n\n## Standards\n\n" + report(standards, sources.standards_md)
        + "\n\n## Spec\n\n" + report(spec, sources.spec_md)
        + "\n\n" + _ledger(standards, spec)
        + "\n## Summary\n\n"
        + f"- **Standards**: {len(standards.findings)} finding(s); worst: {worst(standards)}\n"
        + f"- **Spec**: {len(spec.findings)} finding(s); worst: {worst(spec)}\n\n"
        + "The two axes are reported separately on purpose. There is no cross-axis "
          "winner: a clean Standards axis does not offset a Spec finding, and neither "
          "does the reverse.\n",
        encoding="utf-8")


def verdict_of(standards: AxisOutput, spec: AxisOutput) -> str:
    if standards.blocking or spec.blocking:
        return "changes-requested"
    if standards.findings or spec.findings:
        return "review-with-notes"
    return "clean"


def post_review(pr: int, body: Path, verdict: str, *, repo: str, cwd: Path) -> str:
    """Post as a PR review with the state the verdict implies.

    GitHub forbids approving your own PR, and the factory's PRs are often the
    operator's own — so an APPROVE that bounces falls back to a comment rather
    than losing the review.
    """
    action = {"changes-requested": "--request-changes",
              "clean": "--approve"}.get(verdict, "--comment")
    attempt = subprocess.run(["gh", "pr", "review", str(pr), "--repo", repo, action,
                              "--body-file", str(body)], cwd=cwd, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    if attempt.returncode == 0:
        return action.lstrip("-")
    fallback = subprocess.run(["gh", "pr", "review", str(pr), "--repo", repo, "--comment",
                               "--body-file", str(body)], cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    if fallback.returncode == 0:
        return f"comment (after {action} was rejected)"
    post_comment(pr, body, repo=repo, cwd=cwd)
    return "plain comment (pr review rejected)"


# ── the flow both variants share ─────────────────────────────────────────────

def review_pr_two_axis(*, pr: int, standards_agent: str, spec_agent: str,
                       config: str, adw_id: str | None, repo: str | None,
                       post: bool, keep_worktree: bool) -> int:
    cfg, repo_root = session.bootstrap(config, [standards_agent, spec_agent])
    repo = repo or current_repo(cwd=repo_root)
    raw = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json",
                "headRefName,baseRefName,changedFiles"], cwd=repo_root)
    meta = json.loads(raw)
    branch, base = meta["headRefName"], meta["baseRefName"]
    gh_changed = int(meta.get("changedFiles") or 0)

    cfg.defaults.worktree.enabled = False
    run = session.ensure(cfg, adw_id, repo_root=repo_root,
                         prompt=f"Two-axis review of PR #{pr}", base=None)
    if run.worktree_root is None:
        raise RuntimeError("worktree_root is not configured; this ADW needs somewhere "
                           "to put the PR head checkout. Set defaults.worktree_root.")
    work_root = make_worktree(branch, repo_root=repo_root,
                              worktree_root=run.worktree_root,
                              adw_id=run.adw_id, pr=pr)
    run.work_root = work_root
    head_sha = _run(["git", "rev-parse", "HEAD"], cwd=work_root)
    sources = Sources(run)
    clean = False

    try:
        with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                                   description="Name the pull request and pin the exact "
                                               "commit both axes will judge")) as ph:
            ph.log(pr=f"PR #{pr}", repo=repo, head=branch, base=base,
                   head_sha=head_sha[:12], standards=standards_agent, spec=spec_agent)

        with run.phase(PhaseParams(name="sources", kind="code", owner="git",
                                   description="Gather the diff, the documented standards "
                                               "and the spec each axis will be held to")) as ph:
            files, skipped, issue = gather(pr=pr, repo=repo, repo_root=repo_root,
                                           work_root=work_root, base=base,
                                           head_sha=head_sha, sources=sources)
            ph.log(files=len(files), skipped_archive=len(skipped),
                   github_reports=gh_changed, issue=f"#{issue}" if issue else "none",
                   standards_docs=len(list(sources.std.glob("*.md"))),
                   spec_docs=len(list(sources.spec.glob("*.md"))),
                   has_spec=sources.has_spec)
            # Same collapse guard as the adversarial ADW, measured before the
            # .archive filter so an all-archive PR is not mistaken for a lost head.
            if gh_changed > 0 and not files and not skipped:
                raise RuntimeError(
                    f"PR #{pr} has {gh_changed} changed file(s) on GitHub but the local "
                    f"diff against origin/{base} is empty — HEAD is not on the PR's work. "
                    "Refusing to review nothing.")
            if not files:
                ph.log(outcome="nothing to review")
                clean = True

        if clean:
            return run.finish(accepted=True, reason="")

        # Separate phases, separate context. That separation IS the method.
        with run.phase(PhaseParams(name="standards_axis", kind="agent",
                                   owner=standards_agent, retries=1,
                                   description="Judge the diff against documented repo "
                                               "standards and the smell baseline, only")) as ph:
            standards = ph.call(AgentCall(output_type=AxisOutput, prompt=STANDARDS_TASK.format(
                pr=pr, diff=sources.diff_patch, files=sources.files_txt,
                commits=sources.commits_txt, std=sources.std,
                report=sources.standards_md, head_sha=head_sha)))

        with run.phase(PhaseParams(name="spec_axis", kind="agent", owner=spec_agent,
                                   retries=1,
                                   description="Judge the diff against the issue and its "
                                               "cited documents, only")) as ph:
            spec = ph.call(AgentCall(output_type=AxisOutput, prompt=SPEC_TASK.format(
                pr=pr, has_spec=str(sources.has_spec).lower(),
                issue=f"#{issue}" if issue else "(none linked)",
                issue_body=sources.issue_body, spec=sources.spec,
                diff=sources.diff_patch, files=sources.files_txt,
                report=sources.spec_md, head_sha=head_sha)))

        with run.phase(PhaseParams(name="publish", kind="code", owner="git",
                                   description="Assemble the two axes side by side, derive "
                                               "the verdict, and post it to the PR")) as ph:
            # Ids before assembly: the ledger the fix run parses is built from
            # them, so an unlabelled finding would silently drop out of the set.
            assign_ids(standards)
            assign_ids(spec)
            assemble(sources, pr=pr, issue=issue, standards=standards, spec=spec)
            verdict = verdict_of(standards, spec)
            record = {"schema": "sssf-review/1", "review": "two-axis", "pr": pr,
                      "repo": repo, "issue": issue or None, "head_sha": head_sha,
                      "base": base, "files": len(files), "skipped_archive": skipped,
                      "spec_available": sources.has_spec,
                      "standards": {"total": len(standards.findings),
                                    "by_severity": standards.by_severity(),
                                    "agent": standards_agent},
                      "spec": {"total": len(spec.findings),
                               "by_severity": spec.by_severity(),
                               "agent": spec_agent},
                      "blocking": [f.model_dump() for f in
                                   standards.blocking + spec.blocking],
                      "verdict": verdict}
            sources.verdict_json.write_text(json.dumps(record, indent=2), encoding="utf-8")

            posted = "not posted"
            if post:
                body = sources.root / "review-comment.md"
                body.write_text(sources.review_md.read_text(encoding="utf-8")
                                + f"\n\n<!-- sssf-review\n{json.dumps(record, indent=2)}\n-->\n",
                                encoding="utf-8")
                posted = post_review(pr, body, verdict, repo=repo, cwd=repo_root)
            ph.log(verdict=verdict, standards=len(standards.findings),
                   spec=len(spec.findings),
                   blocking=len(standards.blocking) + len(spec.blocking),
                   posted=posted, report=str(sources.review_md))

        clean = True
        # The run succeeded if the review ran. `changes-requested` is the review
        # doing its job, not the ADW failing to do its own.
        return run.finish(accepted=True, reason="")
    finally:
        if clean and not keep_worktree:
            remove_worktree(work_root, repo_root=repo_root)
