"""Starting a run and letting go of it.

A tick is short-lived: it starts what the limits allow and exits. So a launched
run must outlive its launcher, which means detached, with its own log file and
an id the factory chose in advance.

Choosing the id up front is the important part. Every workflow accepts
`--adw-id`, so the factory pins it rather than parsing stdout to discover what
the run called itself. A tick that crashes between spawning and recording still
leaves a run whose id was already written to state -- the alternative loses
track of a live writer run, which is the one thing that must never happen twice
on a branch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Stage

__version__ = "0.1.0"

# The supervisor's one-shot diagnosis entrypoint. Deliberately not a
# configured p1/p2/p3 stage: it takes no issue or PR argument, owns no
# artifact probe, and never joins the stage sequence -- pretending it was a
# stage would let a later config edit aim the factory's launch/budget
# machinery at a reader that must never touch any of it.
DIAGNOSIS_WORKFLOW = "adws/supervisor_diagnose.py"


class LaunchError(RuntimeError):
    """A run could not be started. Infrastructure, never work."""


@dataclass(frozen=True)
class Launch:
    adw_id: str
    pid: int
    log_file: Path
    argv: list[str]


def new_adw_id() -> str:
    """An 8-character id, matching the format the workflows already produce."""
    return uuid.uuid4().hex[:8]


def _python() -> str:
    """Resolve the interpreter explicitly.

    `sys.executable` is the interpreter running the tick, which is the one whose
    environment the workflows expect. Falling back to a bare `python` would be
    resolved by Windows against the *parent's* PATH regardless of any env we
    pass, which is the same class of bug that made the quality gate run in the
    wrong interpreter for months.
    """
    if sys.executable:
        return sys.executable
    found = shutil.which("python")
    if not found:
        raise LaunchError("no python interpreter found for the child run")
    return found


def build_argv(stage: Stage, item_or_pr: int, adw_id: str, config_rel: str) -> list[str]:
    """The command for one stage. `arg` decides whether it takes an issue or a PR."""
    flag = "--issue" if stage.arg == "issue" else "--pr"
    return [
        _python(),
        stage.workflow,
        flag,
        str(item_or_pr),
        "--adw-id",
        adw_id,
        "--config",
        config_rel,
    ]


def _spawn_detached(argv: list[str], root: Path, log_file: Path) -> subprocess.Popen:
    """Start one detached child, stdout and stderr merged into `log_file`.

    The child gets its own process group so a signal to the launcher never
    reaches a run mid-phase: interrupting an agent halfway leaves the
    permission guard holding a snapshot it never gets to release.
    """
    creation_flags = 0
    start_new_session = False
    if os.name == "nt":
        # CREATE_NO_WINDOW, deliberately NOT DETACHED_PROCESS. Both detach the
        # run from this process's console, but DETACHED_PROCESS leaves the child
        # with no console at all -- so every `bash` and `node` the agent spawns
        # for a tool call finds nothing to inherit and allocates its own, which
        # means a console window flashing on the desktop for every tool call.
        # One run produced 39 of them. CREATE_NO_WINDOW gives the child a real
        # but invisible console that its own children inherit quietly.
        # NEW_PROCESS_GROUP keeps Ctrl+C to the launcher from reaching the run.
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True

    try:
        handle = open(log_file, "ab", buffering=0)
    except OSError as error:
        raise LaunchError(f"cannot open log {log_file}: {error}") from error

    try:
        process = subprocess.Popen(
            argv,
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=start_new_session,
            close_fds=True,
        )
    except OSError as error:
        handle.close()
        raise LaunchError(f"cannot start {' '.join(argv)}: {error}") from error
    finally:
        # The child holds its own duplicate of the handle.
        handle.close()
    return process


def launch(
    stage: Stage,
    item_or_pr: int,
    root: Path,
    log_dir: Path,
    config_rel: str,
    adw_id: str | None = None,
) -> Launch:
    """Start a stage run detached and return immediately."""
    adw_id = adw_id or new_adw_id()
    argv = build_argv(stage, item_or_pr, adw_id, config_rel)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{stage.name}_{item_or_pr}_{adw_id}.log"

    process = _spawn_detached(argv, root, log_file)
    return Launch(adw_id=adw_id, pid=process.pid, log_file=log_file, argv=argv)


def build_diagnosis_argv(
    kind: str,
    item: int | None,
    signature: str,
    reason: str,
    observed_at: str,
    adw_id: str,
    config_rel: str,
) -> list[str]:
    """The command for one diagnosis agent: the incident, whole, as flags.

    Every fact the agent is given rides the command line so the run log and
    the session trace carry the same incident the supervisor signed -- no
    second source of truth to drift from it.
    """
    argv = [_python(), DIAGNOSIS_WORKFLOW, "--kind", kind]
    if item is not None:
        argv += ["--item", str(item)]
    argv += [
        "--signature", signature,
        "--reason", reason,
        "--observed-at", observed_at,
        "--adw-id", adw_id,
        "--config", config_rel,
    ]
    return argv


def diagnosis_log_name(
    kind: str, item: int | None, moment: datetime, adw_id: str
) -> str:
    """One unambiguous Windows-safe name per launch.

    Signal kind, item, the UTC launch moment and the pinned run id -- the
    same identity the supervisor records -- so no two launches on one
    signal share a log, and a person can tell at a glance which signal a
    log belongs to and when it woke.
    """
    parts = [kind]
    if item is not None:
        parts.append(str(item))
    parts.append(moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parts.append(adw_id)
    return "_".join(parts) + ".log"


def launch_diagnosis(
    *,
    kind: str,
    item: int | None,
    signature: str,
    reason: str,
    observed_at: str,
    root: Path,
    log_dir: Path,
    config_rel: str,
    adw_id: str | None = None,
    moment: datetime | None = None,
) -> Launch:
    """Start one read-only diagnosis agent detached and return immediately.

    Same contract as `launch` -- detached lifetime, invisible on Windows,
    stdout and stderr merged into one log the supervisor names -- but for a
    reader that takes an incident, not a stage, and must never be counted,
    budgeted or reaped as factory work.
    """
    adw_id = adw_id or new_adw_id()
    moment = moment or datetime.now(timezone.utc)
    argv = build_diagnosis_argv(kind, item, signature, reason, observed_at, adw_id, config_rel)

    # Every synchronous setup failure arrives as the ONE type the caller
    # catches: a raw OSError from the directory setup would escape the
    # supervisor's launch-failure handling, leave its reservation standing,
    # and abort the status persistence around it.
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LaunchError(
            f"cannot create the diagnosis log directory {log_dir}: {error}"
        ) from error
    log_file = log_dir / diagnosis_log_name(kind, item, moment, adw_id)

    process = _spawn_detached(argv, root, log_file)
    return Launch(adw_id=adw_id, pid=process.pid, log_file=log_file, argv=argv)


def is_alive(pid: int | None) -> bool:
    """Is this process still running?

    Never `os.kill(pid, 0)`. On Windows CPython maps signal 0 to
    TerminateProcess, so the liveness probe kills the thing it is asking about.
    """
    if not pid:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True  # cannot probe != dead; never reclaim a live writer's lock
        if completed.returncode != 0 or not (completed.stdout or "").strip():
            return True
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
