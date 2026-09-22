"""The judgement layer: merge the work, close the item, or ask for a person.

ADR-0002 gives this real authority. It verifies what the reviews claim against
the code on the branch, merges when nothing survives that check, closes the
item, and moves on. It asks for a person only when it is genuinely stuck, or
when it would be undoing its own work a second time.

Two rules shape everything here.

*Reviews return findings, not verdicts.* A meaningful share of findings are
wrong -- a reviewer calling a deliberate out-of-scope omission a critical
regression, or demanding a confirmation the diff already contains. So a finding
is a claim to be checked, never an instruction to obey.

*Never discard work on a severity judgement.* An automated gate that enacted its
own rewind once destroyed a pull request that had met most of its requirements
and had no real defects, on five findings that were all false. This module holds
and escalates; it does not rewind on its own opinion.

*A blocking finding is ruled on, not obeyed.* `verify.py` reads the file each
blocking finding cites at the judged revision and rules whether the claim is
true of that code. Only what survives reaches a person; anything that could not
be checked survives as well, so the fail-safe direction is unchanged. This is
the part of ADR-0002 that dismisses false blockers, and it is worth what it
costs: PR #99 was parked on three Major findings, all three checked by hand
afterwards, none of which justified the park.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, escalate, gates, labels, verify
from .artifacts import GhError, PullRequest
from .config import FactoryConfig
from .paths import FactoryPaths

__version__ = "0.1.4"

# A finding is blocking at these severities. LOW and MEDIUM are recorded and
# ignored: they are the band where false findings cluster, and holding a good
# pull request on them is the failure this design is built to avoid.
_BLOCKING_SEVERITIES = {"HIGH", "CRITICAL", "BLOCKER"}

_SEVERITY_PATTERN = re.compile(
    r"\*\*severity:?\*\*\s*:?\s*([A-Za-z]+)", re.IGNORECASE
)
_BLOCKING_COUNT_PATTERN = re.compile(
    r"(\d+)\s+blocking", re.IGNORECASE
)
_EXPLICIT_BLOCKING = re.compile(
    r"\b(request(?:ed)?[ _-]changes|blocking finding|must fix before merge)\b",
    re.IGNORECASE,
)

# The external reviewer does not emit `**Severity:**` lines at all -- it labels
# findings with its own vocabulary. Scanning only for the in-house marker read
# a 12,000-character review as containing nothing, which would have merged past
# every finding it raised. Each phrase maps to the severity band it belongs in.
_VOCABULARY = {
    "potential issue": "HIGH",
    "major": "HIGH",
    "critical": "CRITICAL",
    "refactor suggestion": "LOW",
    "nitpick": "LOW",
    "minor": "LOW",
    "outside diff": "LOW",
}
# Deliberately not \b. The external reviewer emits its labels as markdown
# emphasis -- `_⚠️ Potential issue_ | _🔴 Major_` -- and underscore is a word
# character, so \b never fires against it. That silently read a review carrying
# a Major finding as carrying nothing at all, which is a merge-past-a-blocker
# bug rather than a cosmetic miss. Letter-only lookarounds bound the phrase
# without treating the surrounding emphasis as part of the word.
_VOCABULARY_PATTERN = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(phrase) for phrase in _VOCABULARY) + r")(?![A-Za-z])",
    re.IGNORECASE,
)

# Reviews quote the code they are talking about. Scanning that quoted code as
# if it were the reviewer's own prose manufactured blocking findings out of
# fixtures -- an HTML doctype fragment and a severity chip that appeared inside
# a quoted review body both parked a healthy item on #8. Blanking the quoted
# regions before the scan removes the false findings without touching the
# vocabulary, so the module keeps erring toward seeing a real blocker.
_FENCED_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
# HTML comments are machine-readable metadata, never the reviewer's prose. The
# two-axis review ends with an `<!-- sssf-review {...} -->` block whose JSON
# carries `"by_severity": {"CRITICAL": 0, "HIGH": 0, ...}`. Scanning it read the
# FIELD NAMES as the reviewer's vocabulary, so a review reporting ZERO critical
# findings produced two CRITICAL ones -- which then escalated as unverified,
# because a JSON blob names no file the agent could check. A clean review
# manufactured its own blockers. Observed on #12 / PR #101, review 4998786308.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _blank_code_regions(body: str) -> str:
    """Replace fenced blocks and inline code spans with blank lines of the same shape.

    Length and line count are preserved so that every offset the callers use for
    excerpt windows still points at the same place in the original text.
    """
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _INLINE_CODE.sub(
        blank, _FENCED_BLOCK.sub(blank, _HTML_COMMENT.sub(blank, body)))


# Findings are listed one after another, so a fixed window BACKWARDS from the
# severity marker lands in the middle of the PREVIOUS finding. On PR #94 that
# reported a HIGH `.archive/` blocker using the quoted HTML fixture from the LOW
# duplicated-code finding above it -- the operator read the excerpt, saw markup,
# and had to open the review by hand to find out what was actually blocking.
# Anchor on the finding's own heading instead.
_FINDING_HEADING = re.compile(r"^#{1,6} .+$", re.MULTILINE)


def _finding_start(body: str, marker: int, lookback: int = 600,
                   scanned: str | None = None) -> int:
    """Where the finding containing `marker` begins.

    Its own heading when there is one, else the start of its paragraph, else a
    bounded window. Never crosses into the finding before it.
    """
    window = max(0, marker - lookback)
    heading = None
    # Scan the blanked text: a `#` line inside a quoted code fence is not a
    # finding heading, and anchoring on one pulls the fence back into the
    # excerpt -- the same fence noise the blanking exists to remove. Offsets are
    # preserved by the blanking, so the excerpt is still cut from `body`.
    for match in _FINDING_HEADING.finditer(scanned or body, window, marker):
        heading = match.start()
    if heading is not None:
        return heading
    paragraph = body.rfind("\n\n", window, marker)
    return paragraph + 2 if paragraph != -1 else window


def _excerpt(body: str, marker: int, end: int, trailing: int = 300,
             scanned: str | None = None) -> str:
    """One finding, read from its start, flattened to a single line."""
    start = _finding_start(body, marker, scanned=scanned)
    return " ".join(body[start:end + trailing].split())[:400]


@dataclass
class Finding:
    severity: str
    source: str          # which review it came from
    excerpt: str

    @property
    def is_blocking(self) -> bool:
        return self.severity.upper() in _BLOCKING_SEVERITIES


@dataclass
class Verdict:
    """What the decider concluded and what it did about it."""

    action: str                      # merged | escalated | held | no-op
    detail: str
    findings: list[Finding] = field(default_factory=list)
    escalated: bool = False
    # Set when action == "escalated": identifies WHAT was escalated, so the
    # caller can persist it and recognise the same unresolved blocker next tick.
    fingerprint: str | None = None
    # Set on a hold caused by the machinery failing, not by the work: a `gh`
    # call that errored, a request that could not be posted. `_judge` must not
    # convert one of these into a permanent park -- reap already refuses to
    # spend the work budget on infrastructure, and a hold is no different.
    transient: bool = False
    # Set on a hold that waiting cannot end. The factory has made every move it
    # has: it asked, and the grace it allows for an answer has passed. Escalated
    # regardless of rounds, because the rounds counter only advances when a stage
    # RUNS, and an item whose stages all read done never runs one -- so a hold
    # here would repeat every tick forever and say so only in a log nobody reads.
    # Measured on #12/PR #101: the external reviewer posted its walkthrough and
    # then never reviewed, and the item held silently for eight hours.
    terminal: bool = False
    # Set on a hold that is waiting for an answer the factory has already asked
    # for. Such a hold is NOT terminal even with the rounds spent: asking is a
    # move, and the answer usually lands within a minute or two. Without this
    # the two features collide -- the decider posts a full-review request and
    # the caller parks the item in the same tick, before the reviewer can reply.
    awaiting_reviewer: bool = False

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.is_blocking]


def _gh(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", str(error)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _review_body(repo: str, pr_number: int, review_id: int, cwd: Path) -> str:
    code, out, _ = _gh(
        ["api", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}", "--jq", ".body"],
        cwd,
    )
    return out if code == 0 else ""


_RETRACTED = re.compile(
    r"(?:✅|:white_check_mark:)\s*Addressed in commit\s+([0-9a-fA-F]{7,40})",
    re.IGNORECASE,
)


def _reviewer_retracted(body: str, pr_commits: set[str]) -> bool:
    """Has the reviewer marked this finding repaired by a commit on this PR?

    Verified, not taken on trust: the named commit must actually be one of the
    pull request's own commits. A marker citing anything else -- a commit from
    another branch, a typo, a sha this pull request never carried -- is ignored
    and the finding still counts.

    Fails CLOSED in both directions that matter. An unreadable marker counts the
    finding; an empty `pr_commits` (the API call failed) counts every finding,
    because "cannot confirm" must never read as "confirmed repaired". The cost of
    that is an escalation, which is the safe error here.
    """
    if not body or not pr_commits:
        return False
    for match in _RETRACTED.finditer(body):
        named = match.group(1).lower()
        if any(sha.lower().startswith(named) for sha in pr_commits):
            return True
    return False


def extract_findings(body: str, source: str) -> list[Finding]:
    """Pull severity-tagged findings out of a review body.

    Reviews are prose, so this is a scan for the severity markers the review
    prompts are written to emit, not a parse of a structured document. It errs
    toward *seeing* a blocker: a review that says "REQUEST_CHANGES" or "2
    blocking" registers even when no severity line was matched, because missing
    a real blocker is far worse than escalating on a false one.
    """
    findings: list[Finding] = []
    # Scan the reviewer's prose only. Offsets are preserved by the blanking, so
    # excerpts are still cut from the original body and read normally.
    scanned = _blank_code_regions(body)

    for match in _SEVERITY_PATTERN.finditer(scanned):
        findings.append(
            Finding(
                severity=match.group(1).upper(),
                source=source,
                excerpt=_excerpt(body, match.start(), match.end(), trailing=200,
                                 scanned=scanned),
            )
        )

    if not findings:
        # Only fall back to the external reviewer's vocabulary when no explicit
        # severity line was found. Running both over one body double-counts: an
        # in-house finding that happens to contain the word "minor" would
        # register twice.
        for match in _VOCABULARY_PATTERN.finditer(scanned):
            findings.append(
                Finding(
                    severity=_VOCABULARY[match.group(1).lower()],
                    source=source,
                    excerpt=_excerpt(body, match.start(), match.end(),
                                     scanned=scanned),
                )
            )

    # A stated blocking count that no severity line accounts for means the scan
    # missed something. Register it rather than assume the body was clean.
    declared = 0
    for match in _BLOCKING_COUNT_PATTERN.finditer(scanned):
        declared = max(declared, int(match.group(1)))
    seen_blocking = sum(1 for finding in findings if finding.is_blocking)
    if declared > seen_blocking:
        findings.append(
            Finding(
                severity="HIGH",
                source=source,
                excerpt=f"review declares {declared} blocking finding(s) the scan did not resolve",
            )
        )

    if _EXPLICIT_BLOCKING.search(scanned) and not any(f.is_blocking for f in findings):
        findings.append(
            Finding(
                severity="HIGH",
                source=source,
                excerpt="review requests changes without an itemised severity",
            )
        )

    return findings


def collect_findings(
    config: FactoryConfig, paths: FactoryPaths, pr: PullRequest,
    revision: str | None = None,
) -> list[Finding]:
    """Every finding from every review pinned to `revision` (default: the head).

    Reviews on older commits are ignored: they describe code that no longer
    exists. That is the same staleness rule the stage probes use, applied to
    judgement instead of progress.

    `revision` is overridden when the head is only cosmetically ahead of the
    last reviewed commit. Counting findings against the head would then find
    none and merge past every objection the covering review raised -- the
    findings must come from the commit that was actually reviewed.
    """
    revision = revision or pr.revision
    findings: list[Finding] = []

    for review in artifacts.reviews(config.repo, pr.number, paths.root):
        if not review.pins(revision):
            continue
        body = _review_body(config.repo, pr.number, review.review_id, paths.root)
        if not body:
            continue
        findings.extend(extract_findings(body, f"{review.author}#{review.review_id}"))

    # Inline comments carry the findings the body only summarises. Replies are
    # skipped: a reply is conversation about a finding, not a new one, and
    # counting it again would inflate a single objection into several.
    #
    # So is a finding the reviewer has itself marked repaired. Costed on PR #99:
    # the reviewer appended "Addressed in commit db3a781" to its own comment, the
    # fix was present in the file at the judged head, the triager had refused the
    # finding as well -- and this module counted it as a blocker and parked the
    # item for a person. Nothing here reads code, so the reviewer's own retraction
    # is the best evidence available that the objection is spent.
    addressed_in = artifacts.pull_request_commits(config.repo, pr.number, paths.root)
    for comment in artifacts.review_comments(config.repo, pr.number, paths.root):
        if not comment.pins(revision) or comment.is_reply:
            continue
        if _reviewer_retracted(comment.body, addressed_in):
            continue
        findings.extend(
            extract_findings(comment.body, f"{comment.author}@{comment.location}")
        )

    return findings


def merge_and_close(
    config: FactoryConfig, paths: FactoryPaths, pr: PullRequest, item: int
) -> tuple[bool, str]:
    """Merge the pull request and close the item.

    Squash-merge with branch deletion: the work branch is disposable, and
    leaving it behind is how worktree and branch clutter accumulated on the
    predecessor project until the disk filled.
    """
    code, _, err = _gh(
        ["pr", "merge", str(pr.number), "--repo", config.repo, "--squash", "--delete-branch"],
        paths.root,
    )
    if code != 0:
        return False, f"merge failed: {err.strip()[:300]}"

    code, _, err = _gh(
        ["issue", "close", str(item), "--repo", config.repo,
         "--comment", f"Merged in #{pr.number} by adws_factory."],
        paths.root,
    )
    if code != 0:
        # The merge landed; the close did not. Report it rather than retrying
        # the merge, which would now fail confusingly against a merged PR.
        return True, f"merged #{pr.number}, but closing #{item} failed: {err.strip()[:200]}"

    try:
        labels.mark_done(config, item, paths.root)
    except labels.LabelError:
        pass  # the projection is cosmetic; the work is done either way

    return True, f"merged #{pr.number} and closed #{item}"


def reviewer_covered_head(spoke_at: str | None, pushed_at: str | None) -> bool:
    """Has the external reviewer said anything since the head commit landed?

    Replaces an earlier attempt that inferred coverage from the SHAPE of the
    diff -- accept the previous review when everything added since it was blank
    or a comment. That was a workaround for a wrong diagnosis. The reviewer is
    incremental and declines to re-review any commit it considers covered, in
    which case no review ever carries that commit's SHA and the gate waits for
    something that is never coming. Whether the diff looks cosmetic to US has
    nothing to do with it; PR #94 hung on four added JSON strings.

    So stop inferring from the diff and ask a question the reviewer answers just
    by acting: it runs on every push, so if it has spoken since this commit
    landed, it looked. Posting findings and posting "Already reviewed" are both
    evidence; declining to speak at all is not.

    Fails CLOSED. A missing timestamp on either side means not covered, so a
    reviewer that is down or slow holds the item rather than waving it through
    -- and a hold is a state somebody is told about, unlike a silent deadlock.
    """
    if not spoke_at or not pushed_at:
        return False
    spoke, pushed = _moment(spoke_at), _moment(pushed_at)
    if spoke is None or pushed is None:
        return False
    return spoke >= pushed


def _moment(stamp: str) -> datetime | None:
    """Parse a GitHub ISO 8601 timestamp. Anything unreadable is None, not now."""
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds_since(stamp: str | None) -> float | None:
    """How long ago `stamp` was, or None if it cannot be read.

    None on purpose rather than 0 or now: an unreadable timestamp must not read
    as "waited long enough", which would turn a parsing bug into an escalation.
    """
    if not stamp:
        return None
    moment = _moment(stamp)
    if moment is None:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _fingerprint(reason: str) -> str:
    return hashlib.sha256(reason.encode("utf-8", "replace")).hexdigest()[:16]


def _await_full_review(
    config: FactoryConfig,
    paths: FactoryPaths,
    pr: PullRequest,
    dry_run: bool,
    stale_findings_from: str | None = None,
) -> Verdict | None:
    """Get the current head ruled on, asking for it instead of waiting on it.

    Returns a `held` verdict while the answer is outstanding, or None when the
    reviewer has already answered and the PR must be judged as it stands.

    Two callers, one mechanism. Blocking findings pinned to an older commit
    (`stale_findings_from` set) need the current head re-checked before those
    findings can be trusted as still live. A push with no review at all
    covering it yet (`stale_findings_from` None) needs exactly the same thing:
    a ruling on the current head. Waiting passively for CodeRabbit's own
    push-trigger works most of the time, but it declines to speak at all on a
    commit it considers already covered -- issue #8's `7a279c18` sat silent for
    over six hours because nothing ever asked. Asking once turns that silence
    into an answer in minutes, the same way it did for #94's earlier commit at
    14:12.

    The wait ends on evidence, not on a tick count: the reviewer answering is it
    speaking after the request was posted. If it spoke and still wrote no review
    pinned to the head, it has said everything it intends to, and holding longer
    would deadlock exactly as the SHA-pinned gate this module replaced did.
    """
    try:
        requested_at = artifacts.full_review_requested_at(
            config.repo, pr.number, pr.revision, paths.root
        )
    except GhError as error:
        return Verdict("held", f"cannot read pull request comments: {error}",
                       transient=True)

    context = (
        f"; the blocking findings are from {stale_findings_from[:8]} and may "
        "already be fixed"
        if stale_findings_from else ""
    )

    if requested_at is None:
        if dry_run:
            return Verdict(
                "held",
                f"PR #{pr.number}: would request a full re-review of "
                f"{pr.revision[:8]}{context}",
                awaiting_reviewer=True,
            )
        try:
            artifacts.request_full_review(
                config.repo, pr.number, pr.revision, paths.root
            )
        except GhError as error:
            # Could not ask. Falling through was safe for the stale-findings
            # caller, which only reaches here holding blocking findings -- but
            # the coverage caller has none, so it went on to judge the OLDER
            # review, found nothing blocking, and squash-merged a head nothing
            # had reviewed. A transient POST failure must not be a merge.
            return Verdict(
                "held",
                f"PR #{pr.number}: cannot request a review of "
                f"{pr.revision[:8]}: {error}",
                transient=True,
            )
        return Verdict(
            "held",
            f"PR #{pr.number}: requested a full re-review of "
            f"{pr.revision[:8]}{context}",
            awaiting_reviewer=True,
        )

    try:
        spoke_at = artifacts.latest_external_activity(config.repo, pr, paths.root)
    except GhError as error:
        return Verdict("held", f"cannot read reviews: {error}")

    answered = reviewer_covered_head(spoke_at, requested_at)
    if not answered:
        # Waiting is only a move while an answer is still plausible. Past the
        # grace the factory allows a requested review, silence is the answer:
        # the reviewer has decided not to speak, and holding longer is the
        # deadlock this whole module was written to avoid. Terminal, not merged
        # -- nobody looked, so nothing may merge on it.
        waited = _seconds_since(requested_at)
        grace = config.limits.external_review_grace_seconds
        if waited is not None and waited > grace:
            return Verdict(
                "held",
                f"PR #{pr.number}: the external reviewer was asked for a full "
                f"review of {pr.revision[:8]} at {requested_at} and has said "
                f"nothing in {waited / 60:.0f} min (grace {grace / 60:.0f} min); "
                f"it is not coming",
                terminal=True,
            )
        return Verdict(
            "held",
            f"PR #{pr.number}: waiting for the full re-review requested at "
            f"{requested_at} (last spoke {spoke_at or 'never'})",
            awaiting_reviewer=True,
        )
    return None


def _rule_on_blocking(
    config: FactoryConfig,
    paths: FactoryPaths,
    pr: PullRequest,
    blocking: list[Finding],
    dry_run: bool,
    item: int = 0,
) -> list[tuple[Finding, verify.Ruling]]:
    """Rule on every blocking finding against the code at the head.

    Fails closed as a whole and per finding. A refused credit gate, a dry run,
    or any error inside the agent leaves the finding surviving and marked
    unverified, which escalates exactly as it did before this existed. Nothing
    here can merge anything; it can only decline to stop a merge.
    """
    if dry_run:
        # A dry run must not spend metered quota, and it must not report a
        # dismissal it did not actually make.
        return [
            (finding, verify.Ruling(True, "dry run: not ruled on", unverified=True))
            for finding in blocking
        ]

    if not config.verify_blocking_findings:
        return [
            (finding, verify.Ruling(True, "verification disabled", unverified=True))
            for finding in blocking
        ]

    # ADR-0002: "The decider spends metered model quota of its own, so it is
    # subject to the same credit gate as the workflows it supervises." A refused
    # gate escalates the findings rather than merging past them -- being unable
    # to check is the same as not having checked.
    gate = gates.check_model_credits(config, config.verification_model)
    if not gate.passed:
        return [
            (finding,
             verify.Ruling(True, f"not ruled on: {gate.detail}", unverified=True))
            for finding in blocking
        ]

    # Read the issue once, and only when something actually needs it. A
    # spec-axis finding compares the code against the issue, so without it the
    # agent can confirm what the code says and never what was asked for.
    spec: str | None = None
    if any(verify.is_spec_axis(finding.excerpt) for finding in blocking):
        try:
            spec = artifacts.issue_body(config.repo, item, paths.root)
        except GhError:
            # Left as None. `verify_finding` fails those findings closed, which
            # escalates them exactly as they escalated before this existed.
            spec = None

    return [
        (
            finding,
            verify.verify_finding(
                finding.source,
                finding.excerpt,
                pr.revision,
                paths.root,
                model=config.verification_model,
                spec=spec,
            ),
        )
        for finding in blocking
    ]


def _record_rulings(
    config: FactoryConfig,
    paths: FactoryPaths,
    pr: PullRequest,
    dismissed: list[tuple[Finding, "verify.Ruling"]],
) -> None:
    """Write onto the pull request why each blocking finding was dismissed.

    ADR-0002 makes this merge happen with nobody watching, so the durable
    record IS the review. It names the revision judged and the files read, so a
    person can repeat the check rather than take it on trust -- which is what
    makes the agent correctable on the day it is wrong.

    Best effort by design. A comment that cannot be posted must not stop a merge
    the decider has already ruled on, and it must not raise into `decide` and
    unwind the tick.
    """
    lines = [
        "**adws_factory decided this pull request.**",
        "",
        f"{len(dismissed)} blocking finding(s) were checked against the code at "
        f"`{pr.revision[:8]}` and did not survive that check. "
        f"Ruled by `{config.verification_model}`.",
        "",
    ]
    for finding, ruling in dismissed:
        read = ", ".join(f"`{path}`" for path in ruling.read) or "(nothing)"
        lines += [
            f"- **{finding.severity}** from `{finding.source}`",
            f"  - finding: {finding.excerpt[:300]}",
            f"  - ruling: {ruling.reason}",
            f"  - read at `{pr.revision[:8]}`: {read}",
        ]
    lines += [
        "",
        "No branch was changed to reach this ruling. If one of these is wrong, "
        "the finding stands and this merge was a mistake worth reporting.",
    ]

    _gh(
        ["api", f"repos/{config.repo}/issues/{pr.number}/comments",
         "-f", "body=" + "\n".join(lines)],
        paths.root,
    )


def decide(
    config: FactoryConfig,
    paths: FactoryPaths,
    item: int,
    dry_run: bool = False,
    escalation_seen: str | None = None,
) -> Verdict:
    """Rule on one item that has nothing further to run.

    Reached two ways: every stage is done, or the item spent its rounds. Both
    mean no stage can advance it, which is precisely when a person must rule.

    Merging requires a review OF THE CURRENT HEAD that raises no blocker --
    never merely the absence of findings, which is also what an unreviewed
    push looks like.

    `escalation_seen` is the fingerprint of the last escalation delivered for
    this item. An identical one is not sent again -- the operator has already
    been told, and repeating it every tick trains them to ignore it.
    """
    try:
        pr = artifacts.open_pull_request(config.repo, item, paths.root)
    except GhError as error:
        return Verdict("held", f"cannot read pull request: {error}")

    if pr is None:
        return Verdict("no-op", f"#{item} has no open pull request to decide on")

    if pr.is_draft:
        return Verdict("held", f"PR #{pr.number} is still a draft")

    try:
        reviewed = artifacts.external_review(config.repo, pr, paths.root)
    except GhError as error:
        return Verdict("held", f"cannot read reviews: {error}")

    covered_by = None
    if reviewed is None:
        # No findings and no review are the same observation to the code below,
        # and merging on the second one means merging code nobody looked at.
        # A push always lands here first -- the external reviewer takes minutes
        # to catch up -- so without this a fresh head is briefly mergeable no
        # matter what is in it.
        #
        # But the reviewer is incremental and will not re-review a commit it
        # considers already covered, so requiring a review pinned to the head
        # hangs forever on any such commit. Ask instead whether it has spoken
        # since the head landed; if it has, it looked, and the review it did
        # write is what this head is judged on.
        try:
            latest = artifacts.latest_external_review(config.repo, pr, paths.root)
            spoke_at = artifacts.latest_external_activity(config.repo, pr, paths.root)
        except GhError as error:
            return Verdict("held", f"cannot read reviews: {error}",
                           transient=True)

        if latest is None:
            # A pull request the reviewer has never touched. This used to hold
            # and nothing else -- no request was ever posted, so the one case
            # where asking helps most was the one case that never asked, and
            # the item held forever. ASK, then let the grace bound above decide
            # when silence has become the answer.
            asked = _await_full_review(config, paths, pr, dry_run)
            if asked is not None:
                return asked
            # It answered after all; judge on whatever it wrote.
            try:
                latest = artifacts.latest_external_review(
                    config.repo, pr, paths.root
                )
            except GhError as error:
                return Verdict("held", f"cannot read reviews: {error}",
                               transient=True)
            if latest is None:
                return Verdict(
                    "held",
                    f"PR #{pr.number}: the external reviewer answered but wrote "
                    f"no review this pull request could be judged on",
                    terminal=True,
                )

        pushed_at = artifacts.commit_committed_at(config.repo, pr.revision, paths.root)
        if not reviewer_covered_head(spoke_at, pushed_at):
            asked = _await_full_review(config, paths, pr, dry_run)
            if asked is not None:
                return asked
            # The request was answered (or could not be sent, in which case
            # this falls through the same way _await_full_review's other
            # caller does) -- proceed to judge on whatever the reviewer wrote.
            try:
                latest = artifacts.latest_external_review(
                    config.repo, pr, paths.root
                )
            except GhError as error:
                # The identical call 20 lines up is guarded; this one was not,
                # so a GhError here escaped `decide`, unwound the whole tick,
                # and threw away the reap work that tick had already done
                # before `state.save`.
                return Verdict("held", f"cannot read reviews: {error}",
                               transient=True)
        covered_by = latest

    try:
        findings = collect_findings(
            config, paths, pr,
            revision=covered_by.commit_id if covered_by else None,
        )
    except GhError as error:
        return Verdict("held", f"cannot read reviews: {error}",
                       transient=True)

    blocking = [finding for finding in findings if finding.is_blocking]

    if blocking and covered_by is not None:
        # These findings were raised against an OLDER commit, because the
        # reviewer declined to pin a review to this head. Anything since fixed
        # therefore blocks forever. Do not soften the judgement -- ask for a
        # ruling on the current code instead, once per head, and hold until it
        # arrives. A full review is the only form that re-checks earlier
        # findings; the plain incremental one looks only at what came after.
        stale = _await_full_review(config, paths, pr, dry_run,
                                   stale_findings_from=covered_by.commit_id)
        # A transient verdict here means the request could not be SENT. This
        # caller already holds blocking findings, so falling through escalates
        # them -- the conservative outcome, and the one it has always had. Only
        # the coverage caller above needs the hold, because it has no findings
        # to escalate and would otherwise merge an unreviewed head.
        if stale is not None and not stale.transient:
            return stale

    dismissed: list[tuple[Finding, verify.Ruling]] = []

    if blocking:
        # ADR-0002's own words: the decider "verifies each blocking finding
        # against the code on the branch, merges when they do not survive that
        # check". Every finding is ruled on against the file at pr.revision;
        # only what survives reaches a person. Anything that could not be ruled
        # on survives too, so the fail-safe direction is unchanged -- what
        # changes is that a finding checked and found false no longer costs an
        # interruption. Measured on PR #99: three Majors, none of which
        # justified the park they caused.
        rulings = _rule_on_blocking(config, paths, pr, blocking, dry_run, item)
        dismissed = [(f, r) for f, r in rulings if not r.survives]
        survivors = [(f, r) for f, r in rulings if r.survives]
        blocking = [finding for finding, _ in survivors]

    if blocking:
        reason = (
            f"{len(blocking)} blocking finding(s) on PR #{pr.number} need a ruling: "
            + "; ".join(
                f"{finding.excerpt[:120]} [{ruling.outcome}: {ruling.reason[:120]}]"
                for finding, ruling in survivors[:3]
            )
        )
        if dismissed:
            reason += f" ({len(dismissed)} other finding(s) checked and dismissed)"
        fingerprint = _fingerprint(reason)
        verdict = Verdict("escalated", reason, findings=findings,
                          fingerprint=fingerprint)
        if dry_run:
            return verdict
        if fingerprint == escalation_seen:
            # Already reported and still unresolved. Treat it as delivered so
            # the caller does not log an escalation failure for a message it
            # deliberately withheld.
            verdict.escalated = True
            verdict.detail += " (already escalated; not repeating)"
            return verdict
        result = escalate.escalate_item(config.repo, item, reason, pr.number)
        verdict.escalated = result.delivered
        if not result.delivered:
            verdict.detail += f" (escalation FAILED: {result.detail})"
        return verdict

    # Say which review was relied on whenever it was not the head's own, so a
    # merge on "the reviewer looked and stayed silent" is legible in the log
    # rather than looking like the head was reviewed in its own right.
    basis = (
        f" on review {covered_by.review_id} of {covered_by.commit_id[:8]};"
        f" the reviewer saw {pr.revision[:8]} and added nothing"
        if covered_by else ""
    )

    ruled = (
        f"; {len(dismissed)} blocking finding(s) checked against the code at "
        f"{pr.revision[:8]} and dismissed"
        if dismissed else ""
    )

    if dry_run:
        return Verdict(
            "merged",
            f"would merge PR #{pr.number} and close #{item} "
            f"({len(findings)} non-blocking finding(s)){basis}{ruled}",
            findings=findings,
        )

    # Record before merging, not after. ADR-0002 makes this merge unattended, so
    # the reasoning that dismissed a blocker has to be on the pull request
    # before the pull request stops being the place anyone looks -- and if the
    # merge fails, the record of what was ruled is still the useful artifact.
    if dismissed:
        _record_rulings(config, paths, pr, dismissed)

    merged, detail = merge_and_close(config, paths, pr, item)
    return Verdict("merged" if merged else "held", detail + basis + ruled,
                   findings=findings)
