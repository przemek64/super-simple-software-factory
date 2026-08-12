"""Low-level git operations for code phases. All low-level logic lives in adw_modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(*, cwd: Path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


def is_repo(*, cwd: Path) -> bool:
    result = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=cwd,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode == 0


def repo_root(*, cwd: Path) -> Path:
    """Return the canonical checkout root containing ``cwd``.

    In a linked worktree, ``--show-toplevel`` names that disposable worktree.
    The common git directory instead remains ``<canonical checkout>/.git``, so
    its parent is the stable home for runtime state shared by every worktree.
    Outside git, preserve the existing behaviour and use the supplied path.
    """
    if not is_repo(cwd=cwd):
        return cwd.resolve()
    common_dir = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir",
                            cwd=cwd)).resolve()
    return common_dir.parent if common_dir.name == ".git" else common_dir


def commit_all(message: str, *, cwd: Path) -> str:
    """Stage the working tree and commit it. Returns the new short sha."""
    if not is_repo(cwd=cwd):
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the "
            "repo root (and make a first commit) before running an ADW that commits.")
    _git("add", "-A", cwd=cwd)
    if not _git("status", "--porcelain", cwd=cwd):
        raise RuntimeError("nothing to commit — the preceding phases changed no files")
    _git("commit", "-m", message, cwd=cwd)
    return _git("rev-parse", "--short", "HEAD", cwd=cwd)


def changed_files(*, cwd: Path) -> list[str]:
    out = _git("status", "--porcelain", cwd=cwd)
    return [line[3:] for line in out.splitlines() if line]


# ── diff plumbing (composed into a ChangeSet by documentation.py) ────────────

def ref_exists(ref: str, *, cwd: Path) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode == 0


def rev(ref: str = "HEAD", *, cwd: Path) -> str:
    return _git("rev-parse", ref, cwd=cwd)


def short_sha(ref: str = "HEAD", *, cwd: Path) -> str:
    return _git("rev-parse", "--short", ref, cwd=cwd)


def merge_base(ref: str, other: str = "HEAD", *, cwd: Path) -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no ADW has to branch on it.
    """
    return _git("merge-base", ref, other, cwd=cwd)


def is_dirty(*, cwd: Path) -> bool:
    return bool(_git("status", "--porcelain", cwd=cwd))


def untracked_files(*, cwd: Path) -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard", cwd=cwd)
    return [line for line in out.splitlines() if line]


def diff_files(base: str, *, cwd: Path) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git("diff", "--name-only", base, cwd=cwd)
    return [line for line in out.splitlines() if line]


def diff_stat(base: str, *, cwd: Path) -> str:
    return _git("diff", "--stat", base, cwd=cwd)


def diff_counts(base: str, *, cwd: Path) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git("diff", "--numstat", base, cwd=cwd).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(base: str, *, cwd: Path) -> str:
    return _git("diff", base, cwd=cwd)
