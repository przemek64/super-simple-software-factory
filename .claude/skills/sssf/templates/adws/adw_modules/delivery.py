"""Deliver run branches to GitHub: verified ones as pull requests, nearly-
finished ones as drafts, and genuinely unfinished ones not at all."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .data_types import DocumentOutput

_COMMAND_TIMEOUT_SECONDS = 120


class DeliveryError(RuntimeError):
    """A push or pull-request failure with enough context for an operator."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str
    created: bool


def _run(argv: list[str], *, cwd: Path, action: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeliveryError(f"Failed to {action}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise DeliveryError(f"Failed to {action}: {detail}")
    return result


def _existing_pr(*, cwd: Path, branch: str, base: str) -> PullRequest | None:
    result = _run(
        [
            "gh", "pr", "list", "--head", branch, "--base", base,
            "--state", "all", "--limit", "100", "--json", "number,url,state",
        ],
        cwd=cwd,
        action=f"check for an existing PR from {branch!r} to {base!r}",
    )
    try:
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise TypeError("expected a JSON list")
        if len(records) > 1:
            raise DeliveryError(
                f"Found multiple PRs from {branch!r} to {base!r}; refusing to guess: "
                f"{records!r}"
            )
        if not records:
            return None
        record = records[0]
        return PullRequest(
            number=int(record["number"]),
            url=str(record["url"]),
            state=str(record["state"]),
            created=False,
        )
    except DeliveryError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise DeliveryError(
            f"GitHub returned invalid PR data for branch {branch!r}: {result.stdout!r}"
        ) from error


def _create_pr(*, cwd: Path, branch: str, base: str,
               title: str, body: str, draft: bool = False) -> PullRequest:
    argv = [
        "gh", "pr", "create", "--base", base, "--head", branch,
        "--title", title, "--body", body,
    ]
    if draft:
        # A draft is what makes an unapproved delivery safe. The factory's
        # decider returns `held` for any draft pull request, so review stages
        # can work on the branch while merging stays impossible until a person
        # marks it ready.
        argv.append("--draft")
    result = _run(
        argv,
        cwd=cwd,
        action=f"open {'draft ' if draft else ''}PR from {branch!r} to {base!r}",
    )
    url = next(
        (line.strip() for line in result.stdout.splitlines()
         if line.strip().startswith(("https://", "http://"))),
        "",
    )
    match = re.search(r"/pull/(\d+)(?:\D|$)", url)
    if not url or match is None:
        raise DeliveryError(
            f"GitHub reported PR creation success for branch {branch!r} but returned "
            f"no recognizable PR URL: {result.stdout!r}"
        )
    return PullRequest(
        number=int(match.group(1)), url=url, state="OPEN", created=True,
    )


DRAFT_MIN_MET_RATIO = 0.90
DRAFT_MAX_BLOCKING = 3


@dataclass(frozen=True)
class Readiness:
    """How close an unapproved review came, in numbers rather than opinion."""

    met: int
    total: int
    blocking: int
    deliverable: bool
    reason: str

    @property
    def ratio(self) -> float:
        return self.met / self.total if self.total else 0.0


def grade(review, *, min_ratio: float = DRAFT_MIN_MET_RATIO,
          max_blocking: int = DRAFT_MAX_BLOCKING) -> Readiness:
    """Is an unapproved build near enough to finished to hand to the reviewers?

    Deliberately arithmetic over what the reviewer already recorded -- how many
    requirements it marked met, and how many items it called blocking. It does
    NOT read a severity, and no severity field should be added for it to read:
    this codebase has twice been burned by trusting a model's own grading (the
    decider ignores LOW/MEDIUM because that is where false findings cluster, and
    a fabricated HIGH on an `.archive` file blocked PR #94 for a day).

    This is a COST filter, not a safety gate. A ratio cannot see importance --
    run 577358e2 sat at 20 of 21 with the missing one being the exactly-once
    audit guarantee, which is the most important requirement it had. What makes
    the delivery safe is that it goes out as a DRAFT and the decider refuses to
    merge a draft. The grade only keeps a half-finished build from spending a
    review cycle.
    """
    findings = list(getattr(review, "findings", []) or [])
    blocking = list(getattr(review, "blocking", []) or [])
    total = len(findings)
    met = sum(1 for finding in findings if getattr(finding, "met", False))

    if total == 0:
        return Readiness(met, total, len(blocking), False,
                         "the review recorded no requirements to judge")
    ratio = met / total
    if ratio < min_ratio:
        return Readiness(met, total, len(blocking), False,
                         f"{met} of {total} requirements met "
                         f"({ratio:.0%} < {min_ratio:.0%})")
    if len(blocking) > max_blocking:
        return Readiness(met, total, len(blocking), False,
                         f"{len(blocking)} blocking item(s) > {max_blocking}")
    return Readiness(met, total, len(blocking), True,
                     f"{met} of {total} requirements met ({ratio:.0%}), "
                     f"{len(blocking)} blocking item(s)")


def draft_body(review, readiness: Readiness, issue_number: int | None) -> str:
    """The PR body for a draft: say plainly that it is unfinished, and why."""
    lines = [
        "**Draft: the suite is green but the reviewer did not approve this build.**",
        "",
        f"Readiness: {readiness.reason}.",
        "",
        "Delivered as a draft so the review stages can work on real code rather "
        "than have it rebuilt from scratch. It cannot be merged while it is a "
        "draft. Do not mark it ready until the items below are closed.",
        "",
        "## Still open",
    ]
    blocking = list(getattr(review, "blocking", []) or [])
    lines += [f"- {item}" for item in blocking] or ["- (none recorded)"]

    unmet = [getattr(f, "requirement", "") for f in getattr(review, "findings", []) or []
             if not getattr(f, "met", False)]
    if unmet:
        lines += ["", "## Requirements not met"]
        lines += [f"- {item}" for item in unmet]
    if issue_number is not None:
        # Deliberately not "Closes #n": this build does not close anything yet.
        lines += ["", f"Refs #{issue_number}"]
    return chr(10).join(lines)


def deliver(run, *, verified: bool,
            document: DocumentOutput | None,
            issue_number: int | None = None,
            review=None, readiness: Readiness | None = None) -> PullRequest | None:
    """Push and open/reuse a PR for a verified run, or a draft for a near-miss.

    A verified run delivers as it always has. An unverified one delivers only
    when the caller passes a `readiness` that is deliverable, and then only as a
    draft -- which is what keeps an unapproved build out of `main` while still
    letting p2 and p3 see it. Without that, an unverified run remains a strict
    no-op: no git, no GitHub request, and no worktree cleanup.

    On a rerun, pushing is harmless and an existing PR for the exact head/base
    pair is reused rather than duplicated.
    """
    as_draft = False
    if not verified:
        if readiness is None or not readiness.deliverable or review is None:
            return None
        as_draft = True

    branch = str(getattr(run, "branch", "") or "").strip()
    base = str(getattr(run, "base_branch", "") or "").strip()
    work_root = Path(getattr(run, "work_root", "")).resolve()
    if not branch or not base:
        raise DeliveryError(
            "Delivery requires the run's recorded branch and base branch."
        )
    if issue_number is not None and issue_number < 1:
        raise DeliveryError("Issue-driven delivery requires a positive issue number.")

    if as_draft:
        # No documenter ran -- the documenter is downstream of approval -- so
        # the title comes from the branch and the body from the review itself.
        stem = str(getattr(run, "branch", "") or "unapproved build").split("/")[-1]
        title = f"[draft] {stem}"
        body = draft_body(review, readiness, issue_number)
    else:
        if document is None or document.status != "success":
            raise DeliveryError("Verified delivery requires a successful documenter output.")
        body = document.summary.strip()
        if issue_number is not None:
            body = f"{body}\n\nCloses #{issue_number}"
        title = (document.commit_message or document.summary).strip()
        if not body or not title:
            raise DeliveryError(
                "Verified delivery requires a non-empty documenter summary and PR title."
            )

    current = _run(
        ["git", "branch", "--show-current"],
        cwd=work_root,
        action=f"confirm expected run branch {branch!r}",
    ).stdout.strip()
    if current != branch:
        raise DeliveryError(
            f"Refusing delivery from the wrong checkout: expected run branch "
            f"{branch!r}, found {current!r}."
        )

    _run(
        ["git", "push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=work_root,
        action=f"push branch {branch!r} to origin",
    )
    existing = _existing_pr(cwd=work_root, branch=branch, base=base)
    if existing is not None:
        return existing
    return _create_pr(
        cwd=work_root,
        branch=branch,
        base=base,
        title=title,
        body=body,
        draft=as_draft,
    )
