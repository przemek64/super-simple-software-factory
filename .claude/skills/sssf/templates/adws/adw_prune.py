#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Safely list or remove factory worktrees.

Listing is always the default. Removal requires both --yes and either
--branch BRANCH or --all. Dirty or uncertain worktrees are never removed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from adw_modules import agents, git_helper, worktree

_CODEBASE_DIR = Path(__file__).resolve().parents[1]
_LOCAL_BRANCH_PREFIX = "refs/heads/"
_COMMAND_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Candidate:
    path: Path
    branch: str | None
    refusal: str | None = None


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git {' '.join(args)} failed: {error}") from error


def _configured_root(repo_root: Path, configured: str | None) -> Path:
    if not configured:
        raise ValueError("No defaults.worktree_root is configured.")
    rendered = configured.replace("{repo_name}", repo_root.name)
    path = Path(rendered).expanduser()
    return (path if path.is_absolute() else repo_root / path).resolve()


def _default_branch(repo_root: Path, remote: str = "origin") -> str:
    result = _run_git(
        repo_root, "symbolic-ref", "--quiet", "--short",
        f"refs/remotes/{remote}/HEAD",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "remote HEAD is unavailable"
        raise RuntimeError(
            f"Cannot determine the default branch safely: {detail}. Refusing to prune."
        )
    value = result.stdout.strip()
    prefix = f"{remote}/"
    if not value.startswith(prefix) or value == prefix:
        raise RuntimeError(
            f"Cannot determine the default branch safely from {value!r}. Refusing to prune."
        )
    return value[len(prefix):]


def _is_strictly_under(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return bool(relative.parts)


def candidates(repo_root: Path, worktree_root: Path, default_branch: str) -> list[Candidate]:
    """Read candidates only from git, filtered to the configured root."""
    canonical = repo_root.resolve()
    found: list[Candidate] = []
    for record in worktree.registered_worktrees(repo_root=repo_root):
        path = record.path.resolve()
        if not _is_strictly_under(path, worktree_root):
            continue

        branch = None
        refusal = None
        if path == canonical:
            refusal = "canonical checkout"
        elif not record.branch_ref or not record.branch_ref.startswith(_LOCAL_BRANCH_PREFIX):
            refusal = "not attached to a local branch"
        else:
            branch = record.branch_ref[len(_LOCAL_BRANCH_PREFIX):]
            if branch == default_branch:
                refusal = f"default branch {default_branch!r}"
        found.append(Candidate(path=path, branch=branch, refusal=refusal))
    return found


def _cleanliness(candidate: Candidate) -> tuple[bool, str | None]:
    """Fail closed: every status error is treated exactly like a dirty tree."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=candidate.path,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception as error:
        # A cleanup tool must fail closed even for errors we did not predict.
        return False, f"cleanliness check failed ({error})"
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git status exited {result.returncode}"
        return False, f"cleanliness check failed ({detail})"
    if result.stdout:
        return False, "uncommitted changes"
    return True, None


def _remove(candidate: Candidate, repo_root: Path) -> tuple[bool, str | None]:
    assert candidate.branch is not None
    result = _run_git(repo_root, "worktree", "remove", "--", str(candidate.path))
    if result.returncode != 0:
        return False, result.stderr.strip() or "git worktree remove failed"

    try:
        if candidate.path.is_symlink() or candidate.path.is_file():
            candidate.path.unlink()
        elif candidate.path.exists():
            shutil.rmtree(candidate.path)
    except OSError as error:
        return False, f"could not remove surviving directory: {error}"

    result = _run_git(repo_root, "branch", "-D", "--", candidate.branch)
    if result.returncode != 0:
        return False, result.stderr.strip() or "local branch deletion failed"
    return True, None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="adws/adw_sssf_config/sssf.config.yaml",
        help="config path, relative to the canonical checkout",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--branch", help="select one exact local branch")
    selector.add_argument("--all", action="store_true", help="select all candidate worktrees")
    parser.add_argument("--yes", action="store_true", help="confirm removal (requires a selector)")
    return parser


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    canonical = (repo_root or git_helper.repo_root(cwd=_CODEBASE_DIR)).resolve()
    cfg = agents.load_config(args.config, repo_root=canonical)
    root = _configured_root(canonical, cfg.defaults.worktree_root)

    # Confirmation by itself is deliberately a no-op, before candidate discovery.
    if args.yes and not (args.branch or args.all):
        print("No selector supplied; --yes alone removes nothing.")
        return 0

    try:
        default_branch = _default_branch(canonical)
        found = candidates(canonical, root, default_branch)
    except (RuntimeError, worktree.WorktreeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    selected = (
        [item for item in found if item.branch == args.branch]
        if args.branch else found
    )
    if args.branch and not selected:
        print(f"No candidate found for branch {args.branch!r}.")
        return 1
    if not selected:
        print(f"No registered worktrees under {root}.")
        return 0

    remove = args.yes and bool(args.branch or args.all)
    failed = False
    for item in selected:
        label = item.branch or "(detached/non-local)"
        if item.refusal:
            print(f"REFUSED {label}  {item.path}  [{item.refusal}]")
            failed = failed or remove
            continue

        clean, reason = _cleanliness(item)
        if not clean:
            print(f"REFUSED {label}  {item.path}  [{reason}]")
            failed = failed or remove
            continue

        if not remove:
            print(f"WOULD REMOVE {label}  {item.path}")
            continue

        ok, error = _remove(item, canonical)
        if ok:
            print(f"REMOVED {label}  {item.path}")
        else:
            failed = True
            print(f"ERROR {label}  {item.path}  [{error}]", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
