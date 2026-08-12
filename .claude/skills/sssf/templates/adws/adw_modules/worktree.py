"""Worktree naming, base resolution, creation, and branch adoption."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from .data_types import WorktreeConfig

_COMMAND_TIMEOUT_SECONDS = 60
_DEFAULT_SETTINGS = WorktreeConfig()


class WorktreeError(RuntimeError):
    """An expected worktree decision failure with an operator-facing message."""


@dataclass(frozen=True)
class RegisteredWorktree:
    """A checkout reported by git, including non-branch/detached entries."""

    path: Path
    branch_ref: str | None


@dataclass(frozen=True)
class Checkout:
    """A selected checkout and whether this initialization owns its artifacts."""

    path: Path
    branch: str
    source: Literal["new", "adopted", "recorded"]

    @property
    def newly_created(self) -> bool:
        return self.source == "new"


def slugify(text: str, max_len: int = 50) -> str:
    """Return a stable, git-safe ASCII slug capped for short Windows paths."""
    if max_len < 1:
        raise ValueError("max_len must be at least 1")
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "task"[:max_len]


def branch_name(prompt: str, adw_id: str, *,
                settings: WorktreeConfig | None = None) -> str:
    """Name a machine branch from readable prompt text and the unique run id."""
    config = settings or _DEFAULT_SETTINGS
    prefix = config.branch_prefix.rstrip("/")
    namespace = f"{prefix}/" if prefix else ""
    return f"{namespace}{slugify(prompt)}-{adw_id}"


def classify_error(error: BaseException) -> str | NoReturn:
    """Map known git failures to actionable text; re-raise unknown failures."""
    text = " ".join(
        part for part in (
            str(error),
            getattr(error, "stderr", None),
            getattr(error, "stdout", None),
        ) if part
    ).casefold()

    if isinstance(error, subprocess.TimeoutExpired) or "timed out" in text or "timeout" in text:
        return "Git timed out. Check the network connection and retry."
    if "permission denied" in text or "access is denied" in text:
        return "Git was denied permission. Check repository and worktree directory permissions."
    if "no space left" in text or "not enough space" in text or "disk full" in text:
        return "Git ran out of disk space. Free space on the worktree drive and retry."
    if "not a git repository" in text:
        return "The configured codebase is not a git repository. Run the factory from a valid checkout."
    if any(marker in text for marker in (
        "branch not found",
        "couldn't find remote ref",
        "could not find remote ref",
        "invalid reference",
        "not a valid object name",
        "unknown revision",
    )):
        return "Git could not find the branch. Verify that the base branch exists on the remote."
    if "already exists" in text and "branch" in text:
        return "The worktree branch already exists but is not attached to a worktree. Remove or rename it and retry."
    if "already checked out" in text:
        return "The branch is already checked out, but its worktree could not be adopted. Check git worktree list."
    raise error


def _raise_classified(error: BaseException) -> NoReturn:
    message = classify_error(error)  # unknown errors are re-raised by classify_error
    raise WorktreeError(message) from error


def _run(argv: list[str], *, cwd: Path, classify_git_errors: bool) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        if classify_git_errors:
            _raise_classified(error)
        raise

    if result.returncode == 0:
        return result

    error = subprocess.CalledProcessError(
        result.returncode,
        argv,
        output=result.stdout,
        stderr=result.stderr,
    )
    if classify_git_errors:
        _raise_classified(error)
    raise error


def _issue_labels(issue_number: int, *, repo_root: Path) -> list[str]:
    result = _run(
        ["gh", "issue", "view", str(issue_number), "--json", "labels"],
        cwd=repo_root,
        classify_git_errors=False,
    )
    try:
        payload = json.loads(result.stdout)
        return [label["name"] for label in payload.get("labels", [])]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise WorktreeError(
            f"GitHub returned invalid label data for issue #{issue_number}."
        ) from error


def map_target_label(labels: list[str], settings: WorktreeConfig) -> str:
    target_labels = [
        label for label in labels if label.startswith(settings.target_label_prefix)
    ]
    if len(target_labels) != 1:
        raise WorktreeError(
            f"Expected exactly one {settings.target_label_prefix!r} label; "
            f"found target labels {target_labels!r} among issue labels {labels!r}."
        )

    target = target_labels[0][len(settings.target_label_prefix):]
    template = settings.target_branch_map.get(target)
    if template is None:
        template = settings.target_branch_map.get("*")
    if template is None:
        raise WorktreeError(
            f"Target label {target_labels[0]!r} has no branch mapping."
        )
    return template.format(target=target)


def _validate_remote_branch(branch: str, *, repo_root: Path, remote: str) -> None:
    result = _run(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        cwd=repo_root,
        classify_git_errors=True,
    )
    expected_ref = f"refs/heads/{branch}"
    found = any(
        line.split("\t", 1)[-1] == expected_ref
        for line in result.stdout.splitlines()
        if line.strip()
    )
    if not found:
        raise WorktreeError(
            f"Base branch {branch!r} does not exist on remote {remote!r}; "
            "create it manually before starting the run."
        )


def registered_worktrees(*, repo_root: Path) -> list[RegisteredWorktree]:
    """Return git's registered worktrees; git is the source of truth."""
    result = _run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=repo_root,
        classify_git_errors=True,
    )
    records: list[RegisteredWorktree] = []
    fields: dict[str, str] = {}
    for token in result.stdout.split("\0"):
        if not token:
            candidate = fields.get("worktree")
            if candidate:
                records.append(RegisteredWorktree(
                    path=Path(candidate).resolve(),
                    branch_ref=fields.get("branch"),
                ))
            fields = {}
            continue
        key, _, value = token.partition(" ")
        fields[key] = value
    return records


def _registered_worktrees(*, repo_root: Path) -> list[tuple[Path, str | None]]:
    """Compatibility tuple view used by the creation/adoption code."""
    return [
        (record.path, record.branch_ref)
        for record in registered_worktrees(repo_root=repo_root)
    ]


def find_existing(branch: str, *, repo_root: Path) -> Path | None:
    """Return the registered worktree attached to ``branch``, if it is live."""
    wanted_ref = f"refs/heads/{branch}"
    for path, branch_ref in _registered_worktrees(repo_root=repo_root):
        if branch_ref == wanted_ref and path.is_dir() and (path / ".git").exists():
            return path
    return None


def _safe_copy_path(relative: str, *, root: Path, role: str) -> Path:
    configured = Path(relative)
    if configured.is_absolute():
        raise WorktreeError(f"copy_files path {relative!r} must be relative to the repository root.")
    resolved = (root / configured).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise WorktreeError(
            f"copy_files path {relative!r} escapes the {role} repository root."
        ) from error
    return resolved


def _copy_configured_files(run, destination: Path) -> None:
    entries = run.cfg.defaults.worktree.copy_files
    for relative in entries:
        source = _safe_copy_path(relative, root=run.repo_root, role="source")
        target = _safe_copy_path(relative, root=destination, role="destination")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists():
                shutil.copy2(source, target)
        except OSError as error:
            raise WorktreeError(
                f"Failed to copy configured path {relative!r} into the worktree: {error}"
            ) from error

    # Keep this separate from the copy operation. A no-op or partial copy must
    # be detected even when inherited environment variables mask the omission.
    for relative in entries:
        target = _safe_copy_path(relative, root=destination, role="destination")
        if not target.exists():
            raise WorktreeError(
                f"Configured copy_files path {relative!r} is missing from the new worktree."
            )


def _remove_stale_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _cleanup_failed_creation(*, repo_root: Path, destination: Path,
                             branch: str) -> None:
    """Best-effort removal of artifacts made by a failed create attempt."""
    def run_cleanup(argv: list[str]) -> None:
        try:
            subprocess.run(
                argv, cwd=repo_root, capture_output=True, text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Preserve the original creation error; cleanup is best effort.
            pass

    run_cleanup(["git", "worktree", "remove", "--force", str(destination)])
    try:
        _remove_stale_target(destination)
    except OSError:
        pass
    # If remove failed but the filesystem fallback succeeded, prune the stale
    # registration before deleting the branch so a retry is not blocked.
    run_cleanup(["git", "worktree", "prune"])
    run_cleanup(["git", "branch", "-D", branch])


def reuse_recorded(run, recorded_path: str | Path) -> Checkout:
    """Reuse a session's canonical checkout identity, or reject stale drift."""
    branch = run.branch
    destination = Path(recorded_path).expanduser().resolve()
    registered = _registered_worktrees(repo_root=run.repo_root)
    wanted_ref = f"refs/heads/{branch}"
    branch_paths = [path for path, branch_ref in registered if branch_ref == wanted_ref]
    path_branches = [branch_ref for path, branch_ref in registered if path == destination]

    if destination not in branch_paths:
        detail = (
            f"branch is registered at {branch_paths!r}"
            if branch_paths else "branch is not registered to a live worktree"
        )
        if path_branches:
            detail += f"; recorded path contains {path_branches!r}"
        raise WorktreeError(
            f"Recorded checkout mismatch for session {run.adw_id!r}: branch {branch!r}, "
            f"path {str(destination)!r}; {detail}. Refusing to create a second worktree."
        )
    if not destination.is_dir() or not (destination / ".git").exists():
        raise WorktreeError(
            f"Recorded worktree {str(destination)!r} for session {run.adw_id!r} is missing. "
            "Refusing to create a replacement with a different identity."
        )
    _copy_configured_files(run, destination)
    return Checkout(path=destination, branch=branch, source="recorded")


def create(run, base_branch: str, *, remote: str = "origin") -> Checkout:
    """Adopt ``run.branch`` or create its computed worktree from the remote base."""
    branch = run.branch
    registered = _registered_worktrees(repo_root=run.repo_root)
    wanted_ref = f"refs/heads/{branch}"
    for path, branch_ref in registered:
        if branch_ref == wanted_ref and path.is_dir() and (path / ".git").exists():
            _copy_configured_files(run, path)
            return Checkout(path=path, branch=branch, source="adopted")

    if run.worktree_root is None:
        raise WorktreeError("No worktree_root is configured; cannot compute a worktree path.")
    destination = (run.worktree_root / branch).resolve()
    try:
        destination.relative_to(run.worktree_root.resolve())
    except ValueError as error:
        raise WorktreeError(f"Branch {branch!r} produces an invalid worktree path.") from error

    if destination in {path for path, _ in registered}:
        raise WorktreeError(
            f"Computed worktree path {destination} is already registered to another branch."
        )
    _remove_stale_target(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "fetch", remote, base_branch],
        cwd=run.repo_root,
        classify_git_errors=True,
    )
    _run(
        ["git", "worktree", "add", str(destination), "-b", branch,
         f"{remote}/{base_branch}"],
        cwd=run.repo_root,
        classify_git_errors=True,
    )
    try:
        _copy_configured_files(run, destination)
    except BaseException:
        # The caller's DB reservation rolls back separately. Remove the tree
        # and branch made by this attempt so a retry can make a clean decision.
        _cleanup_failed_creation(
            repo_root=run.repo_root, destination=destination, branch=branch,
        )
        raise
    return Checkout(path=destination, branch=branch, source="new")


def cleanup_uncommitted(checkout: Checkout, *, repo_root: Path) -> None:
    """Remove only artifacts created by an identity transaction that rolled back."""
    if not checkout.newly_created:
        return
    _cleanup_failed_creation(
        repo_root=repo_root,
        destination=checkout.path,
        branch=checkout.branch,
    )


def resolve_base_branch(issue_number: int | None = None,
                        base_flag: str | None = None, *,
                        settings: WorktreeConfig | None = None,
                        repo_root: Path,
                        remote: str = "origin") -> str:
    """Resolve exactly one base source and verify its branch exists remotely.

    Issue targets are decoded through the configured label map. An explicit
    ``base_flag`` is intentionally returned verbatim after validation.
    """
    supplied = int(issue_number is not None) + int(base_flag is not None)
    if supplied != 1:
        raise WorktreeError(
            "Provide exactly one base source: an issue number or --base, but not both."
        )

    config = settings or _DEFAULT_SETTINGS
    if issue_number is not None:
        labels = _issue_labels(issue_number, repo_root=repo_root)
        branch = map_target_label(labels, config)
    else:
        branch = base_flag  # type: ignore[assignment]  # exactly-one check proves str

    _validate_remote_branch(branch, repo_root=repo_root, remote=remote)
    return branch
