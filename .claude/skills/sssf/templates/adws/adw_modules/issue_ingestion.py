"""Read one GitHub issue, enforce dispatch gates, and build its request."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .data_types import WorktreeConfig
from .worktree import WorktreeError, map_target_label

_COMMAND_TIMEOUT_SECONDS = 60
_APPROVAL_LABEL = "approved-for-dev"
_ON_HOLD_LABEL = "on-hold"
_TRACKER_LABEL = "tracker"


class IssueIngestionError(RuntimeError):
    """An issue entry failure with an actionable operator-facing message."""


@dataclass(frozen=True)
class IssueRequest:
    number: int
    prompt: str
    base_branch: str


def _read_issue(issue_number: int, *, repo_root: Path) -> dict:
    argv = [
        "gh", "issue", "view", str(issue_number),
        "--json", "title,body,comments,labels",
    ]
    try:
        result = subprocess.run(
            argv, cwd=repo_root, capture_output=True, text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IssueIngestionError(
            f"Could not read issue #{issue_number} from GitHub: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise IssueIngestionError(
            f"Could not read issue #{issue_number} from GitHub: {detail}"
        )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise TypeError("expected a JSON object")
        if not isinstance(payload["title"], str):
            raise TypeError("title must be text")
        if payload.get("body") is not None and not isinstance(payload["body"], str):
            raise TypeError("body must be text or null")
        if not isinstance(payload["comments"], list) or not isinstance(payload["labels"], list):
            raise TypeError("comments and labels must be lists")
        return payload
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise IssueIngestionError(
            f"GitHub returned invalid data for issue #{issue_number}."
        ) from error


def _label_names(payload: dict, *, issue_number: int) -> list[str]:
    try:
        return [str(label["name"]) for label in payload["labels"]]
    except (KeyError, TypeError) as error:
        raise IssueIngestionError(
            f"GitHub returned invalid label data for issue #{issue_number}."
        ) from error


def _gate(issue_number: int, labels: list[str], settings: WorktreeConfig) -> None:
    if _APPROVAL_LABEL not in labels:
        raise IssueIngestionError(
            f"Issue #{issue_number} is missing label '{_APPROVAL_LABEL}'. "
            f"Complete triage and add '{_APPROVAL_LABEL}' before retrying."
        )

    targets = [
        label for label in labels if label.startswith(settings.target_label_prefix)
    ]
    if len(targets) != 1:
        raise IssueIngestionError(
            f"Issue #{issue_number} must have exactly one target label with prefix "
            f"{settings.target_label_prefix!r}; found target labels: {targets!r}. "
            "Add one target label (or remove the extras) before retrying."
        )

    if _ON_HOLD_LABEL in labels:
        raise IssueIngestionError(
            f"Issue #{issue_number} has blocking label '{_ON_HOLD_LABEL}'. "
            f"Remove '{_ON_HOLD_LABEL}' before retrying."
        )

    if _TRACKER_LABEL in labels:
        raise IssueIngestionError(
            f"Issue #{issue_number} has label '{_TRACKER_LABEL}'. "
            "Tracker issues are never dispatched; choose an implementation issue instead."
        )


def _comment_key(comment: dict) -> tuple[datetime, str]:
    created = comment.get("createdAt")
    if not isinstance(created, str):
        raise TypeError("comment createdAt must be text")
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as error:
        raise TypeError("comment createdAt must be an ISO timestamp") from error
    return stamp, created


def _prompt(payload: dict, *, issue_number: int) -> str:
    try:
        comments = sorted(payload["comments"], key=_comment_key)
        sections = [
            f"--- BEGIN ISSUE #{issue_number} TITLE ---\n"
            f"{payload['title']}\n"
            f"--- END ISSUE #{issue_number} TITLE ---",
            f"--- BEGIN ISSUE #{issue_number} BODY ---\n"
            f"{payload.get('body') or '(no body provided)'}\n"
            f"--- END ISSUE #{issue_number} BODY ---",
        ]
        for index, comment in enumerate(comments, 1):
            body = comment["body"]
            created = comment["createdAt"]
            author_data = comment.get("author") or {}
            author = author_data.get("login") or "unknown"
            if not isinstance(body, str):
                raise TypeError("comment body must be text")
            sections.append(
                f"--- BEGIN ISSUE #{issue_number} COMMENT {index} "
                f"({author}, {created}) ---\n{body}\n"
                f"--- END ISSUE #{issue_number} COMMENT {index} ---"
            )
        return "\n\n".join(sections)
    except (KeyError, TypeError) as error:
        raise IssueIngestionError(
            f"GitHub returned invalid comment data for issue #{issue_number}."
        ) from error


def ingest(issue_number: int, *, repo_root: Path,
           settings: WorktreeConfig) -> IssueRequest:
    """Fetch once, apply all gates, then assemble the issue-driven request."""
    if issue_number < 1:
        raise IssueIngestionError("--issue must be a positive issue number.")
    payload = _read_issue(issue_number, repo_root=repo_root)
    labels = _label_names(payload, issue_number=issue_number)
    _gate(issue_number, labels, settings)
    try:
        base_branch = map_target_label(labels, settings)
    except WorktreeError as error:
        raise IssueIngestionError(str(error)) from error
    return IssueRequest(
        number=issue_number,
        prompt=_prompt(payload, issue_number=issue_number),
        base_branch=base_branch,
    )
