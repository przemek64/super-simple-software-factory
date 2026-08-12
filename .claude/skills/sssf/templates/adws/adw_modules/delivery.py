"""Deliver verified run branches to GitHub without touching failed runs."""

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
               title: str, body: str) -> PullRequest:
    result = _run(
        [
            "gh", "pr", "create", "--base", base, "--head", branch,
            "--title", title, "--body", body,
        ],
        cwd=cwd,
        action=f"open PR from {branch!r} to {base!r}",
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


def deliver(run, *, verified: bool,
            document: DocumentOutput | None,
            issue_number: int | None = None) -> PullRequest | None:
    """Push and open/reuse a PR only after deterministic verification succeeded.

    Calling this for an unverified run is deliberately a strict no-op: no git,
    no GitHub request, and no worktree cleanup. On a rerun, pushing is harmless
    and an existing PR for the exact head/base pair is reused rather than
    duplicated.
    """
    if not verified:
        return None

    branch = str(getattr(run, "branch", "") or "").strip()
    base = str(getattr(run, "base_branch", "") or "").strip()
    work_root = Path(getattr(run, "work_root", "")).resolve()
    if not branch or not base:
        raise DeliveryError(
            "Verified delivery requires the run's recorded branch and base branch."
        )
    if document is None or document.status != "success":
        raise DeliveryError("Verified delivery requires a successful documenter output.")
    body = document.summary.strip()
    if issue_number is not None:
        if issue_number < 1:
            raise DeliveryError("Issue-driven delivery requires a positive issue number.")
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
    )
