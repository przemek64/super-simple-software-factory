"""Reading a CodeRabbit review off a pull request, as a frozen list of findings.

CodeRabbit is not a deterministic reviewer. Two reviews of the SAME commits on
the same branch (PR #87 and PR #89, `b2b0e23..0b96275`) returned 8 findings and
4 findings, agreed on only two of them, and graded the one real bug 🟠 Major on
one pass and 🟡 Minor on the other. Everything in this module follows from that:

  * A review is CAPTURED ONCE and then treated as a contract. Re-reading is not
    a refresh, it is a different opinion. A fix loop that runs until CodeRabbit
    is satisfied never converges, because each pass retires old nitpicks and
    invents new ones.
  * Severity is recorded but never acted on. It is not stable enough to filter
    with -- dropping "Minor" would have dropped the decode bug on PR #89.
  * Nitpicks are findings. The tautological-version-test finding arrived as an
    actionable inline comment on #87 and as a body nitpick on #89. Parse only
    the inline comments and real bugs go missing.

Findings arrive in two shapes and this module returns both as one flat list:

  inline   -- /pulls/N/comments, anchored to a file and line
  nitpick  -- inside the review body, under "🧹 Nitpick comments (N)"

The issue comment (/issues/N/comments) carries the walkthrough and the run
config. It is read for STATUS only: it is where "Currently processing" and
"Review skipped" appear. It never carries `Actionable comments posted:` -- that
lives in the review body, which is why a poller watching the issue comment for
that string waits forever.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

BOT_LOGIN = "coderabbitai[bot]"

# Body markers. Matched on CONTENT, never on "the bot commented", because the
# bot comments within ~40s with a placeholder that contains no findings at all.
MARKER_ACTIONABLE = "Actionable comments posted:"
MARKER_IN_PROGRESS = "Currently processing"
MARKER_SKIPPED = "Review skipped"

# The two-axis review (adw_pr_review_2axis_*.py) posts a body opening with this
# line. Matched on the MARKER, never on the author or the model: the M3 and codex
# variants post identical headers under the operator's own account, so a gate
# that waited for "the M3 review" would have to change every time a seat does.
MARKER_TWO_AXIS = "# PR Two-Axis Review"

# `374-394`: _📐 Maintainability & Code Quality_ | _🔵 Trivial_ | _💤 Low value_
RE_NITPICK_ENTRY = re.compile(r"^`(?P<lines>[\d-]+)`:\s*(?P<meta>.*)$")
# <summary>config_reader.py (2)</summary>
RE_NITPICK_FILE = re.compile(r"^<summary>(?P<path>[^<]+?)\s*\(\d+\)</summary>")
RE_NITPICK_HEADING = re.compile(r"Nitpick comments \((?P<count>\d+)\)")
RE_ACTIONABLE_COUNT = re.compile(r"Actionable comments posted:\s*(?P<count>\d+)")
RE_TITLE = re.compile(r"^\*\*(?P<title>.+?)\*\*\s*$")
# _🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_
RE_META = re.compile(r"^_(?P<category>[^_]+)_\s*\|\s*_(?P<severity>[^_]+)_\s*\|\s*_(?P<effort>[^_]+)_")

FindingSource = Literal["inline", "nitpick"]
ReviewStatus = Literal["ready", "in_progress", "skipped", "absent"]


class CodeRabbitError(RuntimeError):
    """A rabbit read was asked for and cannot be honoured. Never a silent fallback."""


class RabbitFinding(BaseModel):
    """One thing CodeRabbit reported, flattened out of whichever shape it arrived in."""

    finding_id: str                 # stable across re-reads of the SAME review
    source: FindingSource
    path: str = ""
    line: str = ""                  # "483" for inline, "374-394" for a nitpick range
    category: str = ""              # e.g. "🩺 Stability & Availability"
    severity: str = ""              # RECORDED, NOT ACTED ON -- see module docstring
    effort: str = ""
    title: str = ""
    detail: str = ""
    agent_prompt: str = ""          # CodeRabbit's own "🤖 Prompt for AI Agents" block

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.path else "(no file)"


class RabbitReview(BaseModel):
    """One captured review. The contract a fix run is measured against.

    `review_id` is the identity used to guarantee a review is fixed exactly
    once. It is GitHub's own review id, so it survives a rerun, a new process,
    and a machine reboot -- unlike anything derived from the findings text.
    """

    pr: int
    review_id: int
    submitted_at: str = ""
    actionable_count: int = 0
    findings: list[RabbitFinding] = Field(default_factory=list)
    head_sha: str = ""

    @property
    def empty(self) -> bool:
        return not self.findings


def _gh(*args: str, cwd: Path) -> str:
    """One `gh` call, decoded as utf-8.

    Explicit encoding, not bare `text=True`: on Windows that decodes as cp1252
    and a review body full of emoji severity markers kills the reader thread,
    leaving stdout None. This module reads nothing BUT emoji-laden text.
    """
    result = subprocess.run(["gh", *args], cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise CodeRabbitError(f"gh {' '.join(args)} failed: {(result.stderr or '').strip()}")
    return result.stdout or ""


def _api(path: str, *, cwd: Path) -> list[dict]:
    raw = _gh("api", path, "--paginate", cwd=cwd).strip()
    if not raw:
        return []
    # --paginate concatenates arrays; ask gh to merge them rather than parsing
    # a stream of separate JSON documents.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        merged: list[dict] = []
        for chunk in re.findall(r"\[.*?\]", raw, flags=re.S):
            merged.extend(json.loads(chunk))
        return merged
    return parsed if isinstance(parsed, list) else [parsed]


def _finding_id(source: str, path: str, line: str, title: str) -> str:
    """Stable id for one finding within one review.

    Deliberately content-derived rather than positional: CodeRabbit reorders
    nitpicks between renders of the same review, so an index would drift.
    """
    raw = f"{source}|{path}|{line}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _extract_agent_prompt(block: str) -> str:
    """The '🤖 Prompt for AI Agents' fenced block, if the finding carries one.

    CodeRabbit writes an instruction addressed to a coding agent. It is the most
    directly usable part of a finding, so it travels with it rather than being
    re-derived from the prose.
    """
    if "Prompt for AI Agents" not in block:
        return ""
    tail = block.split("Prompt for AI Agents", 1)[1]
    fenced = re.search(r"```(?:\w+)?\n(?P<body>.*?)```", tail, flags=re.S)
    return fenced.group("body").strip() if fenced else ""


def _title_of(lines: list[str]) -> str:
    """The finding's headline.

    Usually a bold line. Not always: some findings open straight into a
    <details> block with no heading at all, and an untitled finding reads as
    noise in the triage handoff, so fall back to the first line of real prose.
    """
    bold = next((m.group("title") for m in
                 (RE_TITLE.match(candidate) for candidate in lines) if m), "")
    if bold:
        return bold

    # Fall back to the first prose line OUTSIDE any <details>. Those blocks hold
    # CodeRabbit's verification transcript ("🏁 Script executed:", shell, output
    # lengths); taking a line from in there names the investigation rather than
    # the finding.
    depth = 0
    for candidate in lines:
        text = candidate.strip()
        if text.startswith("<details"):
            depth += 1
            continue
        if text.startswith("</details"):
            depth = max(0, depth - 1)
            continue
        if depth or not text or text.startswith(("<", ">", "```", "_", "|", "-", "!")):
            continue
        return text[:120]
    return ""


def _split_meta(line: str) -> tuple[str, str, str]:
    match = RE_META.match(line.strip())
    if not match:
        return "", "", ""
    return (match.group("category").strip(), match.group("severity").strip(),
            match.group("effort").strip())


def parse_inline_comments(comments: list[dict]) -> list[RabbitFinding]:
    """Findings from /pulls/N/comments -- the ones anchored to a file and line."""
    findings: list[RabbitFinding] = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != BOT_LOGIN:
            continue
        body = comment.get("body") or ""
        lines = body.split("\n")
        category, severity, effort = _split_meta(lines[0] if lines else "")
        title = _title_of(lines)
        path = comment.get("path") or ""
        # `line` is null on an outdated comment; original_line still locates it.
        line = str(comment.get("line") or comment.get("original_line") or "")
        findings.append(RabbitFinding(
            finding_id=_finding_id("inline", path, line, title),
            source="inline", path=path, line=line,
            category=category, severity=severity, effort=effort,
            title=title, detail=body, agent_prompt=_extract_agent_prompt(body),
        ))
    return findings


def parse_nitpicks(review_body: str) -> list[RabbitFinding]:
    """Findings from the collapsed 'Nitpick comments' section of the review body.

    These are findings, not decoration. See the module docstring: the same
    observation lands here on one run and as an actionable inline comment on the
    next, so dropping this section loses real bugs at random.
    """
    if not RE_NITPICK_HEADING.search(review_body):
        return []

    section = review_body.split("Nitpick comments", 1)[1]
    # Stop before the trailing run-config blocks, which reuse <summary> markup
    # and would otherwise be read as further file sections.
    for terminator in ("Prompt for all review comments", "Review info", "Run configuration"):
        section = section.split(terminator, 1)[0]

    findings: list[RabbitFinding] = []
    current_path = ""
    pending: Optional[dict] = None
    buffer: list[str] = []

    def flush() -> None:
        if pending is None:
            return
        block = "\n".join(buffer)
        title = _title_of(buffer)
        category, severity, effort = _split_meta(pending["meta"])
        findings.append(RabbitFinding(
            finding_id=_finding_id("nitpick", pending["path"], pending["lines"], title),
            source="nitpick", path=pending["path"], line=pending["lines"],
            category=category, severity=severity, effort=effort,
            title=title, detail=block.strip(), agent_prompt=_extract_agent_prompt(block),
        ))

    for raw_line in section.split("\n"):
        file_match = RE_NITPICK_FILE.match(raw_line.strip())
        if file_match:
            flush()
            pending, buffer = None, []
            current_path = file_match.group("path").strip()
            continue

        entry_match = RE_NITPICK_ENTRY.match(raw_line.strip())
        if entry_match:
            flush()
            pending = {"path": current_path, "lines": entry_match.group("lines"),
                       "meta": entry_match.group("meta")}
            buffer = []
            continue

        if pending is not None:
            buffer.append(raw_line)

    flush()
    return findings


def status(pr: int, *, repo: str, cwd: Path) -> ReviewStatus:
    """Where this PR's review stands, judged on CONTENT rather than existence.

    "the bot has commented" is true ~40 seconds after the PR opens, on a
    placeholder holding zero findings. A poller that stops there hands a builder
    the words "please wait" as its review.
    """
    issue_comments = _api(f"repos/{repo}/issues/{pr}/comments", cwd=cwd)
    bodies = [c.get("body") or "" for c in issue_comments
              if (c.get("user") or {}).get("login") == BOT_LOGIN]

    if any(MARKER_SKIPPED in body for body in bodies):
        return "skipped"

    reviews = _api(f"repos/{repo}/pulls/{pr}/reviews", cwd=cwd)
    if any(MARKER_ACTIONABLE in (r.get("body") or "") for r in reviews
           if (r.get("user") or {}).get("login") == BOT_LOGIN):
        return "ready"

    if any(MARKER_IN_PROGRESS in body for body in bodies):
        return "in_progress"
    return "absent"


class TwoAxisReview(BaseModel):
    """One captured two-axis review, frozen the same way a rabbit review is."""

    pr: int
    review_id: int
    submitted_at: str = ""
    body: str = ""


def two_axis_ready(pr: int, *, repo: str, cwd: Path) -> bool:
    """Has a two-axis review landed on this PR?"""
    reviews = _api(f"repos/{repo}/pulls/{pr}/reviews", cwd=cwd)
    return any(MARKER_TWO_AXIS in (r.get("body") or "") for r in reviews)


def capture_two_axis(pr: int, *, repo: str, cwd: Path) -> TwoAxisReview:
    """Freeze the newest two-axis review. Raises if none has landed."""
    reviews = [r for r in _api(f"repos/{repo}/pulls/{pr}/reviews", cwd=cwd)
               if MARKER_TWO_AXIS in (r.get("body") or "")]
    if not reviews:
        raise CodeRabbitError(f"no two-axis review on PR #{pr}")
    newest = max(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
    return TwoAxisReview(pr=pr, review_id=int(newest["id"]),
                         submitted_at=newest.get("submitted_at") or "",
                         body=newest.get("body") or "")


def await_reviews(pr: int, *, repo: str, cwd: Path, timeout_seconds: float = 900.0,
                  interval_seconds: float = 30.0, on_poll=None
                  ) -> tuple[Optional[RabbitReview], Optional[TwoAxisReview]]:
    """Wait for BOTH reviewers, then freeze both as one contract.

    Both or nothing, deliberately. The two reviewers ask different questions --
    the rabbit asks "is this code correct", the two-axis pair asks "is it written
    the way this repo writes code" and "is it what the issue asked for" -- so
    triaging one without the other produces a fix run that answers half the
    question and then reports itself finished. A partial contract is worse than
    no contract: the ledger would record the PR as handled.

    Returns (None, None) on timeout or on a rabbit skip, and the caller ends the
    run cleanly. A reviewer that never speaks is not a defect in the PR.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        rabbit_state = status(pr, repo=repo, cwd=cwd)
        axis_state = "ready" if two_axis_ready(pr, repo=repo, cwd=cwd) else "absent"
        if on_poll:
            on_poll(rabbit_state, axis_state, max(0.0, deadline - time.monotonic()))
        if rabbit_state == "skipped":
            return None, None
        if rabbit_state == "ready" and axis_state == "ready":
            return (capture(pr, repo=repo, cwd=cwd),
                    capture_two_axis(pr, repo=repo, cwd=cwd))
        if time.monotonic() >= deadline:
            return None, None
        time.sleep(min(interval_seconds, max(1.0, deadline - time.monotonic())))


def write_two_axis_findings(review: TwoAxisReview, *, run) -> str:
    """The two-axis review as its own handoff file, verbatim.

    Verbatim rather than reformatted: the axes already emit structured findings
    with severities and locations, and a triager judging them needs the text the
    reviewer actually published, not this module's paraphrase of it.
    """
    path = _handoff_dir(run) / "two_axis_findings.md"
    path.write_text(review.body, encoding="utf-8")
    return str(path)


def capture(pr: int, *, repo: str, cwd: Path) -> RabbitReview:
    """Freeze the latest finished review into a contract. Raises if none is finished."""
    reviews = [r for r in _api(f"repos/{repo}/pulls/{pr}/reviews", cwd=cwd)
               if (r.get("user") or {}).get("login") == BOT_LOGIN
               and MARKER_ACTIONABLE in (r.get("body") or "")]
    if not reviews:
        raise CodeRabbitError(
            f"PR #{pr} has no finished CodeRabbit review to capture "
            f"(status: {status(pr, repo=repo, cwd=cwd)}).")

    review = reviews[-1]
    body = review.get("body") or ""
    inline = _api(f"repos/{repo}/pulls/{pr}/comments", cwd=cwd)
    count_match = RE_ACTIONABLE_COUNT.search(body)

    return RabbitReview(
        pr=pr,
        review_id=int(review["id"]),
        submitted_at=review.get("submitted_at") or "",
        actionable_count=int(count_match.group("count")) if count_match else 0,
        head_sha=review.get("commit_id") or "",
        findings=parse_inline_comments(inline) + parse_nitpicks(body),
    )


def await_review(pr: int, *, repo: str, cwd: Path, timeout_seconds: float = 900.0,
                 interval_seconds: float = 30.0, on_poll=None) -> Optional[RabbitReview]:
    """Poll until a review is finished. Returns None on timeout or skip.

    None rather than an exception on purpose: a missing rabbit review is not a
    defect in the pull request. The PR is already tested and reviewed by the
    factory's own chain, so the caller ends the run cleanly instead of marking
    good work failed.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = status(pr, repo=repo, cwd=cwd)
        if on_poll:
            on_poll(state, max(0.0, deadline - time.monotonic()))
        if state == "ready":
            return capture(pr, repo=repo, cwd=cwd)
        if state == "skipped":
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(interval_seconds, max(1.0, deadline - time.monotonic())))


# ── Handing a captured review to an agent ────────────────────────────────────

def _handoff_dir(run) -> Path:
    path = Path(run.data_dir) / "sessions" / run.adw_id / "context_handoff"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_findings(review: RabbitReview) -> str:
    """The captured review as markdown, one section per finding.

    Written to a file rather than inlined into a prompt: findings carry whole
    diffs and CodeRabbit's own agent prompts, and a triager that has to judge
    them needs the text intact, not summarised by whoever built the prompt.
    """
    lines = [f"# CodeRabbit review — PR #{review.pr} (review {review.review_id})", ""]
    lines += [f"Submitted: {review.submitted_at or 'unknown'}",
              f"Findings: {len(review.findings)} "
              f"({review.actionable_count} actionable, "
              f"{sum(1 for f in review.findings if f.source == 'nitpick')} nitpick)", ""]
    lines += ["Severity labels are recorded but NOT reliable — the same bug was graded",
              "Major on one review and Minor on another. Judge each finding on its own terms.", ""]

    for finding in review.findings:
        lines += [f"## [{finding.finding_id}] {finding.title or '(untitled)'}", "",
                  f"- location: `{finding.location}`",
                  f"- source: {finding.source}",
                  f"- category: {finding.category or 'n/a'}",
                  f"- severity (unreliable): {finding.severity or 'n/a'}", ""]
        if finding.agent_prompt:
            lines += ["### CodeRabbit's instruction", "", "```", finding.agent_prompt, "```", ""]
        lines += ["### Full text", "", finding.detail.strip(), ""]
    return "\n".join(lines)


def write_findings(review: RabbitReview, *, run) -> str:
    path = _handoff_dir(run) / "coderabbit_findings.md"
    path.write_text(render_findings(review), encoding="utf-8")
    return str(path)


def write_triage(review: RabbitReview, triage, *, run) -> str:
    """The accepted/rejected split, as the builder's work order.

    The builder is handed decisions, never the raw review: it must not be in a
    position to re-litigate a finding triage already refused.
    """
    by_id = {f.finding_id: f for f in review.findings}
    accepted = [v for v in triage.verdicts if v.accepted]
    rejected = [v for v in triage.verdicts if not v.accepted]

    lines = [f"# Triage — PR #{review.pr} (review {review.review_id})", "",
             f"Accepted: {len(accepted)} · Rejected: {len(rejected)}", "",
             "## Fix these — and nothing else", ""]
    for verdict in accepted:
        finding = by_id.get(verdict.finding_id)
        if finding is None:
            continue
        lines += [f"### [{finding.finding_id}] {finding.title or '(untitled)'}", "",
                  f"- location: `{finding.location}`", ""]
        if verdict.reason:
            lines += [f"Triage note: {verdict.reason}", ""]
        if finding.agent_prompt:
            lines += ["```", finding.agent_prompt, "```", ""]
        lines += [finding.detail.strip(), ""]

    lines += ["## Refused — do not touch these", ""]
    for verdict in rejected:
        finding = by_id.get(verdict.finding_id)
        location = finding.location if finding else "?"
        title = (finding.title if finding else "") or "(untitled)"
        lines += [f"- [{verdict.finding_id}] `{location}` {title} — {verdict.reason}"]
    lines.append("")

    path = _handoff_dir(run) / "coderabbit_triage.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


# ── The "fix each review exactly once" ledger ────────────────────────────────

def ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / "coderabbit_processed.json"


def _load_ledger(data_dir: Path) -> dict:
    path = ledger_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt ledger must not silently re-authorise fixing a review that
        # was already fixed -- that is the one failure this file exists to stop.
        raise CodeRabbitError(
            f"{path} is not valid JSON. It records which reviews were already "
            f"fixed; repair or delete it deliberately before running again.")


def ledger_key(repo: str, review: RabbitReview) -> str:
    return f"{repo}#{review.pr}#{review.review_id}"


def already_processed(review: RabbitReview, *, repo: str, data_dir: Path) -> Optional[dict]:
    """The record of a previous fix run for this exact review, or None."""
    return _load_ledger(data_dir).get(ledger_key(repo, review))


def mark_processed(review: RabbitReview, *, repo: str, data_dir: Path,
                   adw_id: str, outcome: str) -> None:
    ledger = _load_ledger(data_dir)
    ledger[ledger_key(repo, review)] = {
        "adw_id": adw_id,
        "outcome": outcome,
        "findings": len(review.findings),
        "submitted_at": review.submitted_at,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
