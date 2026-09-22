"""What proves a stage happened.

The core of ADR-0003: a stage is done when its artifact exists *and belongs to
the current revision*, never when its run ended happily. Run status answers "did
the process exit cleanly", which is a different question from "did the work
land", and the two have disagreed in both directions here -- a run that pushed
its fixes then failed a health check, and a run that ended clean having quietly
reverted the one file it was asked to change.

Every probe is revision-pinned. An unpinned artifact means a review of older
code keeps a stage looking finished after the code moved on, which is how a
stale approval turns into false progress. The revision is the pull request's
head commit; when a fix lands, the head moves, and every review pinned to the
old head stops counting -- which is what pulls an item back a stage and makes
the round-limit necessary.

All GitHub reads go through `gh`, which owns authentication. Subprocess calls
here pin encoding explicitly: on Windows the default is cp1252, and a single
em dash in a PR body is enough to crash the child with a decode error.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.1.3"

# The 2axis review is published from the operator's own account, while the
# external reviewer posts as a bot. The two are different stages and must not be
# confused for one another, so each probe names the identity it accepts.
CODERABBIT_LOGIN = "coderabbitai[bot]"

LEDGER_REL = "adws/adw_runtime/coderabbit_processed.json"

# What p3 counts as a review worth consuming. The reviewer also posts summary
# and walkthrough reviews with no findings; taking one of those as "the review"
# points the factory at something p3 will never process. Must stay in step with
# `adws/adw_modules/coderabbit.py:MARKER_ACTIONABLE`.
ACTIONABLE_MARKER = "Actionable comments posted:"


class GhError(RuntimeError):
    """A `gh` call failed. Callers treat this as infrastructure, never as work."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    head_ref: str
    head_oid: str
    is_draft: bool
    merged_at: str | None

    @property
    def revision(self) -> str:
        """The commit every artifact for this item is pinned against."""
        return self.head_oid

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @property
    def is_merged(self) -> bool:
        return self.merged_at is not None


@dataclass(frozen=True)
class Review:
    review_id: int
    author: str
    state: str
    commit_id: str
    submitted_at: str
    body: str = ""

    def pins(self, revision: str) -> bool:
        return bool(self.commit_id) and self.commit_id == revision

    @property
    def is_actionable(self) -> bool:
        """Would p3 accept this as the review to consume?

        p3 requires the marker (`coderabbit._is_rabbit_review_of`), because the
        reviewer also posts summary and walkthrough reviews carrying no findings
        at all. The factory did not, and the two then pointed at different
        reviews of the SAME head: the probe at the newest, p3 at the newest
        ACTIONABLE one. p3 exited "already processed" in 0.0s, the probe still
        read the stage open, and the tick relaunched it eight times in a row
        until the launch ceiling stopped it. Observed on #12 / PR #101,
        2026-08-22, where 4998784813 carries no marker and 4998776984 does.
        """
        return ACTIONABLE_MARKER in (self.body or "")


@dataclass(frozen=True)
class ReviewComment:
    """One inline comment anchored to a file and line.

    The external reviewer puts its substantive findings here, not in the review
    body -- a body can summarise six findings in a sentence while the findings
    themselves live in twenty thousand characters of inline comments. Anything
    judging a pull request has to read both.
    """

    comment_id: int
    review_id: int
    author: str
    commit_id: str
    original_commit_id: str
    path: str
    line: int | None
    body: str
    in_reply_to: int | None
    created_at: str = ""

    def pins(self, revision: str) -> bool:
        """Was this comment WRITTEN against `revision`?

        Deliberately `original_commit_id`, not `commit_id`. GitHub re-pins an
        unresolved inline comment to each new head as long as its line still
        exists, so `commit_id` tracks the branch and is no evidence of when the
        objection was raised. A review's own commit_id is frozen at submission
        and does not have this problem, which is why only comments need the
        distinction.

        Observed on PR #92: findings written against d404e31, fixed two commits
        later, kept reporting the current head and were counted as live
        blockers on every tick -- an item that could never clear the decider.
        """
        pinned = self.original_commit_id or self.commit_id
        return bool(pinned) and pinned == revision

    @property
    def is_reply(self) -> bool:
        return self.in_reply_to is not None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass(frozen=True)
class IssueComment:
    """One conversation comment on a pull request, anchored to nothing.

    Carries no commit id, which is exactly why it needed a type of its own: it
    is evidence about WHEN something was said, never about which code it was
    said against.
    """

    comment_id: int
    author: str
    body: str
    created_at: str


def _gh(args: list[str], cwd: Path, timeout: int = 60) -> str:
    """Run `gh` and return stdout, or raise GhError.

    `encoding="utf-8"` is not optional. With `text=True` alone Python uses the
    Windows ANSI code page, and one em dash in a PR body raises a decode error
    that looks nothing like its cause.
    """
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
        raise GhError(f"gh {' '.join(args)}: {error}") from error

    if completed.returncode != 0:
        raise GhError(f"gh {' '.join(args)} exited {completed.returncode}: "
                      f"{(completed.stderr or '').strip()[:300]}")
    return completed.stdout


def _branch_names_issue(branch: str, item: int) -> bool:
    """Does this branch belong to the given issue?

    Workflows name branches `sssf/begin-issue-<N>-<title>-<adw_id>`. Matching on
    a bounded `issue-<N>` token rather than a substring keeps issue 4 from
    matching issue 40.
    """
    return re.search(rf"issue-{item}(?!\d)", branch) is not None


# ---------------------------------------------------------------- p1 artifact

def open_pull_request(repo: str, item: int, cwd: Path) -> PullRequest | None:
    """The p1 artifact: an open pull request on the item's own branch.

    Looks at open pull requests and matches on the branch naming convention
    rather than trusting a stored branch name, so an item stays recognisable
    across a lost or reset state file.
    """
    raw = _gh(
        [
            "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
            "--json", "number,state,headRefName,headRefOid,isDraft,mergedAt",
        ],
        cwd,
    )
    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise GhError(f"unparseable pr list: {error}") from error

    for entry in entries:
        if _branch_names_issue(entry.get("headRefName") or "", item):
            return PullRequest(
                number=entry["number"],
                state=entry.get("state") or "",
                head_ref=entry.get("headRefName") or "",
                head_oid=entry.get("headRefOid") or "",
                is_draft=bool(entry.get("isDraft")),
                merged_at=entry.get("mergedAt"),
            )
    return None


# Written by the migration tooling under an <!-- archon-deps --> marker. The
# bold form is what every migrated issue carries; the bare form is accepted so a
# hand-written dependency is not silently ignored.
_DEPENDS_ON = re.compile(r"^\s*(?:\*\*)?Depends-on:?(?:\*\*)?:?\s*(.+)$",
                         re.MULTILINE | re.IGNORECASE)


def issue_body(repo: str, item: int, cwd: Path) -> str:
    """The issue text, which for a spec-axis finding IS the specification.

    A spec-axis finding claims the code disagrees with the issue it was built
    from. Ruling on that needs both sides, and the decider has only ever had
    the code -- so every such finding failed closed and woke a person. Measured
    on PR #100, where the agent named the right load-bearing fact ("do these
    files fall outside the declared change list?") and could not settle it,
    because the list was in the issue and the issue was never passed.
    """
    raw = _gh(["issue", "view", str(item), "--repo", repo, "--json", "body"], cwd)
    try:
        return (json.loads(raw or "{}") or {}).get("body") or ""
    except json.JSONDecodeError as error:
        raise GhError(f"unparseable issue body: {error}") from error


def declared_dependencies(repo: str, item: int, cwd: Path) -> list[int]:
    """Issues this one says it is built on top of, in declaration order."""
    body = issue_body(repo, item, cwd)

    found: list[int] = []
    for match in _DEPENDS_ON.finditer(body):
        for number in re.findall(r"#(\d+)", match.group(1)):
            if int(number) != item and int(number) not in found:
                found.append(int(number))
    return found


def unmet_dependencies(repo: str, item: int, cwd: Path) -> list[int]:
    """Declared dependencies that are still open.

    The work in these issues is not on `main` yet, so a branch cut from main
    cannot build on it. Launching anyway produces an agent fighting absent
    code and a retest failure that looks like its own fault -- the same
    misattribution a stale base causes, with a worse cause.
    """
    unmet = []
    for dependency in declared_dependencies(repo, item, cwd):
        try:
            raw = _gh(
                ["issue", "view", str(dependency), "--repo", repo, "--json", "state"],
                cwd,
            )
            state = (json.loads(raw or "{}") or {}).get("state") or ""
        except (GhError, json.JSONDecodeError):
            # Unreadable is not proof of done. Fail closed: hold the item.
            unmet.append(dependency)
            continue
        if state.upper() != "CLOSED":
            unmet.append(dependency)
    return unmet


def pull_request(repo: str, number: int, cwd: Path) -> PullRequest | None:
    """One pull request by number, in any state."""
    try:
        raw = _gh(
            [
                "pr", "view", str(number), "--repo", repo,
                "--json", "number,state,headRefName,headRefOid,isDraft,mergedAt",
            ],
            cwd,
        )
    except GhError:
        return None
    entry = json.loads(raw)
    return PullRequest(
        number=entry["number"],
        state=entry.get("state") or "",
        head_ref=entry.get("headRefName") or "",
        head_oid=entry.get("headRefOid") or "",
        is_draft=bool(entry.get("isDraft")),
        merged_at=entry.get("mergedAt"),
    )


# ---------------------------------------------------------------- reviews

def reviews(repo: str, number: int, cwd: Path) -> list[Review]:
    """Every review on a pull request, newest last."""
    raw = _gh(["api", f"repos/{repo}/pulls/{number}/reviews", "--paginate"], cwd)
    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise GhError(f"unparseable reviews: {error}") from error

    found = []
    for entry in entries:
        found.append(
            Review(
                review_id=entry.get("id", 0),
                author=((entry.get("user") or {}).get("login") or ""),
                state=entry.get("state") or "",
                commit_id=entry.get("commit_id") or "",
                submitted_at=entry.get("submitted_at") or "",
                # Already in the response; discarding it cost the factory the
                # ability to tell an actionable review from a walkthrough.
                body=entry.get("body") or "",
            )
        )
    return found


def review_comments(repo: str, number: int, cwd: Path) -> list[ReviewComment]:
    """Every inline review comment on a pull request.

    Paginated: a thorough review can leave dozens, and a truncated read would
    silently drop findings -- the failure mode being guarded against here is a
    judgement made on a partial view of the objections.
    """
    raw = _gh(["api", f"repos/{repo}/pulls/{number}/comments", "--paginate"], cwd)
    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise GhError(f"unparseable review comments: {error}") from error

    found = []
    for entry in entries:
        found.append(
            ReviewComment(
                comment_id=entry.get("id", 0),
                review_id=entry.get("pull_request_review_id") or 0,
                author=((entry.get("user") or {}).get("login") or ""),
                commit_id=entry.get("commit_id") or "",
                original_commit_id=entry.get("original_commit_id") or "",
                path=entry.get("path") or "",
                line=entry.get("line"),
                body=entry.get("body") or "",
                in_reply_to=entry.get("in_reply_to_id"),
                created_at=entry.get("created_at") or "",
            )
        )
    return found


# ---------------------------------------------------------------- p2 artifact

def pinned_review(
    repo: str, pr: PullRequest, cwd: Path, exclude_author: str = CODERABBIT_LOGIN
) -> Review | None:
    """The p2 artifact: a published review pinned to the current head.

    Excludes the external bot, whose review is p3's input rather than p2's
    output. A review pinned to an older commit is deliberately not returned:
    the code moved on, so that review no longer describes it.
    """
    candidates = [
        review
        for review in reviews(repo, pr.number, cwd)
        if review.author != exclude_author and review.pins(pr.revision)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda review: review.submitted_at)


def latest_external_review(
    repo: str, pr: PullRequest, cwd: Path, author: str = CODERABBIT_LOGIN
) -> Review | None:
    """The newest external review on this pull request, pinned or not.

    Separate from `external_review` on purpose. That one answers "has the
    current head been reviewed"; this one answers "what is the most recent
    thing the reviewer looked at", which is what a staleness comparison needs.
    """
    candidates = [
        review for review in reviews(repo, pr.number, cwd) if review.author == author
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda review: review.submitted_at)


def issue_comments(repo: str, number: int, cwd: Path) -> list[IssueComment]:
    """Every conversation comment on a pull request. Not the inline ones.

    A different endpoint from `review_comments`, and both are needed. Inline
    comments carry findings; THIS is where the external reviewer answers a
    review request -- including "Already reviewed", which is the only signal it
    ever gives that a head it declined to review is nonetheless covered.
    """
    raw = _gh(["api", f"repos/{repo}/issues/{number}/comments", "--paginate"], cwd)
    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise GhError(f"unparseable issue comments: {error}") from error

    return [
        IssueComment(
            comment_id=entry.get("id", 0),
            author=((entry.get("user") or {}).get("login") or ""),
            body=entry.get("body") or "",
            created_at=entry.get("created_at") or "",
        )
        for entry in entries
    ]


FULL_REVIEW_MARKER = "<!-- factory: full-review-requested {revision} -->"


def full_review_requested_at(
    repo: str, number: int, revision: str, cwd: Path
) -> str | None:
    """When the factory last asked for a full review OF THIS HEAD. ISO 8601, or None.

    The request is its own record. Storing it in the pull request rather than in
    factory state means a reader of the PR can see why a re-review happened, and
    a lost or rolled-back state file cannot cause the same request to be posted
    twice.
    """
    marker = FULL_REVIEW_MARKER.format(revision=revision)
    stamps = [
        comment.created_at
        for comment in issue_comments(repo, number, cwd)
        if marker in comment.body and comment.created_at
    ]
    return max(stamps) if stamps else None


def request_full_review(repo: str, number: int, revision: str, cwd: Path) -> None:
    """Ask the external reviewer to re-examine the whole pull request.

    `@coderabbitai review` is INCREMENTAL: it looks only at what arrived since
    it last spoke, so it cannot say whether an earlier finding was fixed. Only
    `full review` re-analyses every file, which is the question being asked here.

    Never `@coderabbitai approve` or `resolve`. Both write a verdict, and this
    module holds and escalates rather than ruling -- an automated gate that
    enacted its own judgement once destroyed a sound pull request.
    """
    marker = FULL_REVIEW_MARKER.format(revision=revision)
    body = (
        "@coderabbitai full review\n\n"
        f"Re-check the whole pull request at `{revision[:8]}`, including whether "
        "the findings in your earlier review are still present.\n\n"
        f"{marker}"
    )
    _gh(
        ["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"],
        cwd,
    )


def latest_external_activity(
    repo: str, pr: PullRequest, cwd: Path, author: str = CODERABBIT_LOGIN
) -> str | None:
    """When did the external reviewer last say ANYTHING here? ISO 8601, or None.

    Deliberately not "last review". The reviewer is incremental: for a head it
    considers already covered it writes no review at all, only a comment saying
    so. Reading reviews alone therefore cannot distinguish "has not looked yet"
    from "looked and had nothing to add" -- and treating the second as the first
    is what hangs an item forever.

    Anything it posted counts as evidence it woke up for this pull request. What
    that evidence MEANS is the caller's question, answered by comparing this
    against the head commit's own timestamp.
    """
    stamps = [
        review.submitted_at
        for review in reviews(repo, pr.number, cwd)
        if review.author == author and review.submitted_at
    ]
    stamps += [
        comment.created_at
        for comment in review_comments(repo, pr.number, cwd)
        if comment.author == author and comment.created_at
    ]
    stamps += [
        comment.created_at
        for comment in issue_comments(repo, pr.number, cwd)
        if comment.author == author and comment.created_at
        and not is_pending_acknowledgement(comment.body)
    ]
    return max(stamps) if stamps else None


# The reviewer answers a `full review` request twice: first a promise -- "I will
# perform a complete review at <sha>" -- posted within seconds, then the review
# itself minutes later, with the SAME comment edited to report the action
# performed. Counting the promise as coverage is what let PR #95 be judged, and
# a merge attempted, while the review it had just asked for was still running.
# On a mergeable pull request that merges past a review nobody has read.
_PENDING_ACK = re.compile(r"CodeRabbit review command invocation", re.IGNORECASE)
# Both ways the reviewer can RESOLVE a command, not just the happy one. It
# answers a request either "Action performed / Full review finished" or
# "Action not completed" -- rate limited, or "Already reviewed", which is its
# incremental rule declining to look at a commit it considers covered.
#
# Matching only the happy form made every refusal read as a promise still
# outstanding. The comment was then dropped from `latest_external_activity`,
# coverage could never advance, and `_await_full_review` held forever with
# `awaiting_reviewer=True` -- the one hold `_judge` refuses to escalate. Silent
# permanent stall, nobody paged. Two real instances in this repository: PR #92
# "Review rate limited" and PR #94 "Already reviewed".
#
# It also contradicted `reviewer_covered_head`, which says in as many words that
# "posting findings and posting 'Already reviewed' are both evidence".
_ACK_COMPLETED = re.compile(
    r"(action performed|action not completed|review finished|review completed"
    r"|already reviewed|rate limited)",
    re.IGNORECASE,
)


# "Action performed" is not one thing. For `resolve` it means the work is done;
# for `full review` the SAME phrase means the review has just been kicked off --
# "Action performed / Full review triggered". Reading the second as completion
# said the reviewer had answered when it had barely started, and the decider
# then found no review, concluded none was coming, and parked the item within a
# minute of asking. Observed on PR #101 and #102 on 2026-08-22, immediately
# after the Pro seat was assigned and the reviewer finally started working.
#
# So a comment announcing that a review has STARTED is a promise, whatever else
# it says, and this is checked before the completed forms rather than after.
_ACK_STARTED = re.compile(
    r"(review triggered|review started|review is in progress|reviewing now)",
    re.IGNORECASE,
)


def is_pending_acknowledgement(body: str) -> bool:
    """True for a reviewer comment that PROMISES a review it has not delivered.

    A promise is not evidence that the reviewer looked -- it is evidence that it
    is about to. Only the completed form counts, and the reviewer marks
    completion by editing the same comment, so the two are told apart by content
    rather than by waiting a fixed time for the second one.
    """
    if not body or not _PENDING_ACK.search(body):
        return False
    if _ACK_STARTED.search(body):
        # Started, so certainly not finished -- even though the same comment
        # also says "Action performed".
        return True
    return not _ACK_COMPLETED.search(body)


def pull_request_commits(repo: str, number: int, cwd: Path) -> set[str]:
    """Every commit sha on the pull request, per GitHub.

    Read from the API rather than the local checkout for the same reason as
    `commit_committed_at`: heads are pushed from run worktrees this checkout may
    never have fetched, so `git` here cannot answer reliably.

    Returns an empty set on any failure, which callers must treat as "cannot
    confirm" rather than "not present".
    """
    try:
        raw = _gh(["api", f"repos/{repo}/pulls/{number}/commits",
                   "--paginate", "--jq", ".[].sha"], cwd)
    except GhError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def commit_committed_at(repo: str, revision: str, cwd: Path) -> str | None:
    """When the given commit landed, per GitHub. ISO 8601, or None.

    Read from the API rather than the local checkout on purpose: heads are
    pushed from run worktrees this checkout may never have fetched, and a
    staleness comparison that silently fails closed on an unfetched commit
    would hold every item it could not resolve.
    """
    if not revision:
        return None
    try:
        raw = _gh(["api", f"repos/{repo}/commits/{revision}",
                   "--jq", ".commit.committer.date"], cwd)
    except GhError:
        return None
    return raw.strip() or None


def external_review(
    repo: str, pr: PullRequest, cwd: Path, author: str = CODERABBIT_LOGIN
) -> Review | None:
    """The newest external review pinned to the current head, if any.

    This is p3's *input*, not an artifact of any stage. p3 cannot run without
    one, and it must be pinned: consuming a review of superseded code is how a
    fix run ends up re-fixing something already fixed.
    """
    candidates = [
        review
        for review in reviews(repo, pr.number, cwd)
        if review.author == author and review.pins(pr.revision)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda review: review.submitted_at)


def actionable_external_review(
    repo: str, pr: PullRequest, cwd: Path, author: str = CODERABBIT_LOGIN
) -> Review | None:
    """The newest review of this head that p3 would actually consume.

    `external_review` answers "has the head been reviewed", which is the
    decider's question and correctly counts a review that found nothing. This
    answers the p3 artifact's question -- "which review is p3 working on" --
    and it has to pick the same one p3 picks or the stage never reads done.
    """
    candidates = [
        review
        for review in reviews(repo, pr.number, cwd)
        if review.author == author and review.pins(pr.revision) and review.is_actionable
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda review: review.submitted_at)


# ---------------------------------------------------------------- p3 artifact

def _ledger_key(repo: str, pr_number: int, review_id: int) -> str:
    return f"{repo}#{pr_number}#{review_id}"


def ledger_entry(
    root: Path, repo: str, pr_number: int, review_id: int
) -> dict | None:
    """The p3 artifact: a ledger entry naming the review it consumed.

    Keyed by the review it processed, not by the pull request, so a second
    review on the same pull request is correctly seen as unconsumed work rather
    than as already done.
    """
    ledger_file = root / LEDGER_REL
    if not ledger_file.is_file():
        return None
    try:
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # An unreadable ledger must not read as "already processed" -- that
        # would skip a fix stage silently.
        return None
    return ledger.get(_ledger_key(repo, pr_number, review_id))
