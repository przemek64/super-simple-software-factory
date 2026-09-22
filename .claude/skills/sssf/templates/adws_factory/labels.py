"""The label projection.

Labels are output, not input, with three exceptions: the entry ticket, the hold,
and the target branch. Everything else the factory writes is a projection of
what the artifacts already say (ADR-0003), recomputed each tick. Nothing reads
a projected label to make a decision, so a wrong one is cosmetic rather than a
second, competing source of truth.

One hard rule: every label the factory writes must exist before it writes it.
A missing label crashed an entire tick on the predecessor project, taking down
scheduling for every item rather than just the one being labelled.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import FactoryConfig

__version__ = "0.1.1"

# Colours are cosmetic but stable, so a person scanning the issue list can read
# position and trouble at a glance.
_LABEL_COLOURS = {
    "stage": "1d76db",
    "blocked": "b60205",
    "done": "0e8a16",
}


class LabelError(RuntimeError):
    """Label work failed. Never fatal to a tick -- projections are cosmetic."""


def _gh(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
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
        raise LabelError(f"gh {' '.join(args)}: {error}") from error
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def existing_labels(repo: str, cwd: Path) -> set[str]:
    code, out, err = _gh(
        ["label", "list", "--repo", repo, "--limit", "200", "--json", "name"], cwd
    )
    if code != 0:
        raise LabelError(f"cannot list labels: {err.strip()[:200]}")
    try:
        return {entry["name"] for entry in json.loads(out or "[]")}
    except (json.JSONDecodeError, KeyError) as error:
        raise LabelError(f"unparseable label list: {error}") from error


def _colour_for(name: str, config: FactoryConfig) -> str:
    if name == config.labels.done:
        return _LABEL_COLOURS["done"]
    if name.startswith(config.labels.blocked_prefix):
        return _LABEL_COLOURS["blocked"]
    return _LABEL_COLOURS["stage"]


def ensure_labels(config: FactoryConfig, cwd: Path) -> list[str]:
    """Create every label the factory may write. Returns the ones created.

    Done once at the start of a tick rather than lazily at each write, so the
    failure surfaces before anything has been half-labelled.
    """
    managed = config.labels.all_managed(config.stages)
    present = existing_labels(config.repo, cwd)
    created = []
    for name in managed:
        if name in present:
            continue
        code, _, err = _gh(
            [
                "label", "create", name, "--repo", config.repo,
                "--color", _colour_for(name, config),
                "--description", "managed by adws_factory",
            ],
            cwd,
        )
        if code != 0:
            if "already exists" not in err.lower():
                raise LabelError(f"cannot create {name!r}: {err.strip()[:200]}")
            continue        # raced with another writer; it exists, we did not make it
        created.append(name)
    return created


def issue_labels(repo: str, item: int, cwd: Path) -> set[str]:
    code, out, err = _gh(
        ["issue", "view", str(item), "--repo", repo, "--json", "labels"], cwd
    )
    if code != 0:
        raise LabelError(f"cannot read labels on #{item}: {err.strip()[:200]}")
    try:
        payload = json.loads(out or "{}")
        return {entry["name"] for entry in payload.get("labels", [])}
    except (json.JSONDecodeError, KeyError) as error:
        raise LabelError(f"unparseable issue labels: {error}") from error


def project_stage(
    config: FactoryConfig, item: int, stage_name: str | None, cwd: Path
) -> None:
    """Make the item's stage label match where it actually is.

    Removes any other stage label first, so an item never carries two positions.
    """
    current = issue_labels(config.repo, item, cwd)
    wanted = config.labels.stage(stage_name) if stage_name else None

    stale = {
        name
        for name in current
        if name.startswith(config.labels.stage_prefix) and name != wanted
    }
    for name in stale:
        _gh(["issue", "edit", str(item), "--repo", config.repo,
             "--remove-label", name], cwd)

    if wanted and wanted not in current:
        _gh(["issue", "edit", str(item), "--repo", config.repo,
             "--add-label", wanted], cwd)


def mark_blocked(config: FactoryConfig, item: int, stage_name: str, cwd: Path) -> None:
    """Park an item. Cleared only by a person deleting the label."""
    _gh(["issue", "edit", str(item), "--repo", config.repo,
         "--add-label", config.labels.blocked(stage_name)], cwd)


def mark_held(config: FactoryConfig, item: int, cwd: Path) -> None:
    """Park an item behind the person's own hold label, checked.

    Unlike the other two `mark_*` writers this one verifies the edit
    succeeded: it exists for the supervisor's `hold_item` action, whose
    entire armed effect is one label -- a failed `gh issue edit` silently
    swallowed there would report an item held that is not, and a person
    would trust the record.

    The label itself is created up front by `ensure_labels`, because
    `Labels.all_managed` includes `config.labels.hold`: a clean repository
    has the label before any armed action writes it.
    """
    code, _, err = _gh(
        ["issue", "edit", str(item), "--repo", config.repo,
         "--add-label", config.labels.hold],
        cwd,
    )
    if code != 0:
        raise LabelError(f"cannot hold #{item}: {err.strip()[:200]}")


def mark_done(config: FactoryConfig, item: int, cwd: Path) -> None:
    _gh(["issue", "edit", str(item), "--repo", config.repo,
         "--add-label", config.labels.done], cwd)


def is_held(config: FactoryConfig, item: int, cwd: Path) -> bool:
    """A person's instruction to skip this item entirely."""
    return config.labels.hold in issue_labels(config.repo, item, cwd)


def blocked_stage(config: FactoryConfig, item: int, cwd: Path) -> str | None:
    """Which stage this item is parked at, read from the label a person clears.

    Deliberately read from GitHub rather than from state: a person clearing the
    label is how blocked is lifted, and they clear it there.
    """
    for name in issue_labels(config.repo, item, cwd):
        if name.startswith(config.labels.blocked_prefix):
            return name[len(config.labels.blocked_prefix):]
    return None


def eligible_items(config: FactoryConfig, cwd: Path) -> list[int]:
    """Open issues carrying the entry ticket, oldest first (FIFO).

    Held and finished items are filtered here; blocked ones are not, because
    the tick reports them and a person may have just cleared the label.
    """
    code, out, err = _gh(
        [
            "issue", "list", "--repo", config.repo, "--state", "open",
            "--label", config.labels.entry, "--limit", "100",
            "--json", "number,labels", "--search", "sort:created-asc",
        ],
        cwd,
    )
    if code != 0:
        raise LabelError(f"cannot list eligible issues: {err.strip()[:200]}")
    try:
        entries = json.loads(out or "[]")
    except json.JSONDecodeError as error:
        raise LabelError(f"unparseable issue list: {error}") from error

    items = []
    for entry in entries:
        names = {label["name"] for label in entry.get("labels", [])}
        if config.labels.hold in names or config.labels.done in names:
            continue
        items.append(entry["number"])
    return items
