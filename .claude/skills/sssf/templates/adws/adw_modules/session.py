"""Session lifecycle: pin-or-create an adw_id, build the Run object.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from . import git_helper, procinfo, worktree
from .data_types import SSSFConfig
from .runner import Run
from .tracer import Tracer
from .utils import engineer_name, new_id, now_iso


# Resolve from the installed factory code, not the process cwd. This remains
# the canonical checkout even when the command is launched elsewhere; in a
# linked worktree git_helper.repo_root follows the shared .git directory home.
_CODEBASE_DIR = Path(__file__).resolve().parents[2]


def _state_path(repo_root: Path, configured: str | Path) -> Path:
    """Anchor configured runtime state to the canonical checkout."""
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()



def state_path(repo_root: Path, configured: str | Path) -> Path:
    """Public form of _state_path, for callers outside this module.

    A resume has to locate the tracer db the same way `ensure` does; without
    this it would have to reach into a private name or re-derive the rule and
    quietly disagree with it.
    """
    return _state_path(repo_root, configured)

def _reconcile_dead_runs(tracer: Tracer, current_adw_id: str) -> None:
    """Mark 'running' sessions whose workflow process no longer exists.

    A hard kill never reaches _finalize_when_killed, so the session row claims
    work is in flight that is already dead — forever, since nothing else ever
    revisits it. On Windows that is EVERY stop: there is no SIGTERM, so
    `Stop-Process`/`taskkill` end the process without running the handler, and
    two such corpses sat reading 'running' for days.

    Every new run sweeps, so the truth does not depend on anyone remembering to
    run a cleanup script. A session is dead when its recorded `adw` pid is gone
    or recycled. Genuinely live runs are untouched — the OS says so, not a
    timeout — and procinfo fails safe, reporting a pid it cannot probe as
    alive, so an unreadable process list leaves a stale row rather than
    finalizing a run that is still working.
    """
    rows = tracer.conn.execute(
        "SELECT s.adw_id, p.pid, p.command FROM sessions s"
        " JOIN processes p ON p.adw_id = s.adw_id AND p.kind = 'adw'"
        " WHERE s.status = 'running' AND s.adw_id != ?",
        (current_adw_id,)).fetchall()
    if not rows:
        return
    live = procinfo.live_commands(pid for _, pid, _ in rows if pid is not None)
    # A resumed session records an `adw` process per invocation, so one dead pid
    # does not make the session dead: the original can be gone while its resume
    # is still working. Every recorded process must be gone before we finalize.
    by_session: dict[str, bool] = {}
    for adw_id, pid, command in rows:
        actual = live.get(int(pid)) if pid is not None else None
        running = actual is not None and procinfo.is_same_process(command, actual)
        by_session[adw_id] = by_session.get(adw_id, False) or running
    for adw_id, any_alive in by_session.items():
        if any_alive:
            continue
        ts = now_iso()
        tracer.conn.execute(
            "UPDATE sessions SET status='fail', ended_at=?"
            " WHERE adw_id=? AND status='running'", (ts, adw_id))
        tracer.conn.execute(
            "UPDATE phases SET status='fail', ended_at=?,"
            " error='process died without finalizing (hard kill or crash)'"
            " WHERE adw_id=? AND status='running'", (ts, adw_id))
        tracer.processes_end_all(adw_id)


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so `just kill`
    (or any `kill <pid>`) would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.

    Windows delivers no SIGTERM: `taskkill /F` and `Stop-Process` terminate the
    process outright and this handler never runs. SIGBREAK (Ctrl-Break, and
    `taskkill` without /F on a console process) is the closest equivalent, so it
    is registered where it exists — but the real safety net there is
    _reconcile_dead_runs, which does not need the dying process to cooperate.
    """
    def handler(signum, _frame):
        run.tracer.session_finish(run.adw_id, ok=False)   # also closes process rows
        raise SystemExit(128 + signum)

    handled = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        handled.append(signal.SIGBREAK)
    for sig in handled:
        signal.signal(sig, handler)


def codebase_dir() -> Path:
    """Return the factory code directory without invoking git."""
    return _CODEBASE_DIR


def bootstrap(config: str, required_agents: list[str]) -> tuple[SSSFConfig, Path]:
    """Resolve the checkout once, then load and validate config against it."""
    # Import locally to avoid session <-> agents initialization coupling.
    from . import agents

    repo_root = git_helper.repo_root(cwd=codebase_dir())
    cfg = agents.load_config(config, repo_root=repo_root)
    agents.validate(cfg, required_agents)
    return cfg, repo_root


def require_phase_a_base(base: str | None) -> str:
    """Reject an un-targeted Phase-A entry before bootstrap performs any git call."""
    if not base:
        raise ValueError(
            "A Phase-A run requires --base <branch>. "
            "Issue-derived bases via --issue are not available until Phase B."
        )
    return base


def ensure(cfg: SSSFConfig, adw_id: str | None = None, *, repo_root: Path,
           prompt: str = "", base: str | None = None) -> Run:
    """Create or join a run, creating/adopting its worktree before any phase."""
    adw_id = adw_id or new_id(8)
    # The caller resolved this once before config validation; never re-derive it here.
    data_dir = _state_path(repo_root, cfg.defaults.data_dir)
    observability_db = _state_path(repo_root, cfg.observability.db)
    tracer = Tracer(observability_db,
                    data_dir / "sessions" / adw_id / "events.jsonl")
    run: Run | None = None
    identity_committed = False
    try:
        # Hold one cross-process SQLite reservation from identity lookup until
        # both the worktree and its DB identity exist. Without this boundary,
        # same-ID processes can both observe no row and derive different
        # prompt-based branches before either calls session_start.
        with tracer.serialize_checkout_initialization():
            # Existing checkouts do not depend on the original remote base
            # remaining reachable. Their base is identity too: reject a rerun
            # that names another base before reusing or changing any worktree.
            recorded_identity = tracer.session_checkout(adw_id)
            recorded_checkout = (
                recorded_identity[:2] if recorded_identity is not None else None
            )
            base_branch = None
            if cfg.defaults.worktree.enabled:
                required_base = require_phase_a_base(base)
                recorded_base = (
                    recorded_identity[2] if recorded_identity is not None else None
                )
                if recorded_base is not None and recorded_base != required_base:
                    raise ValueError(
                        f"Session {adw_id!r} was started from base "
                        f"{recorded_base!r}; refusing mismatched --base "
                        f"{required_base!r}. Reuse the original base."
                    )
                base_branch = (
                    (recorded_base or required_base)
                    if recorded_checkout is not None and any(recorded_checkout)
                    else worktree.resolve_base_branch(
                        base_flag=required_base,
                        settings=cfg.defaults.worktree,
                        repo_root=repo_root,
                    )
                )
            run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer=engineer_name(),
                      repo_root=repo_root, data_dir=data_dir,
                      observability_db=observability_db, prompt=prompt,
                      base_branch=base_branch, recorded_checkout=recorded_checkout)
            tracer.session_start(
                adw_id, run.engineer, adw_name=Path(sys.argv[0]).stem,
                branch=run.branch, worktree_path=str(run.work_root),
                base_branch=run.base_branch,
            )
        identity_committed = True
    except BaseException:
        # A transaction failure after `worktree add` rolls its DB row back but
        # not its filesystem/git side effects. Only the checkout explicitly
        # marked new belongs to this attempt; adopted and recorded trees are
        # never cleanup candidates.
        if not identity_committed and run is not None and run.checkout is not None:
            # Reacquire the same reservation before inspecting committed state.
            # Otherwise a waiting same-ID retry can adopt this tree after our
            # read but before cleanup, then lose the checkout it just recorded.
            # A matching committed identity owns the tree now; an absent or
            # different identity permits cleanup only of artifacts this failed
            # attempt actually created.
            try:
                with tracer.serialize_checkout_initialization():
                    recorded_after_failure = tracer.session_checkout(adw_id)
                    recorded_checkout_after_failure = (
                        recorded_after_failure[:2]
                        if recorded_after_failure is not None else None
                    )
                    failed_identity = (
                        run.checkout.branch,
                        str(run.checkout.path),
                    )
                    if (recorded_checkout_after_failure != failed_identity
                            and run.checkout.newly_created):
                        worktree.cleanup_uncommitted(
                            run.checkout, repo_root=repo_root,
                        )
            except BaseException:
                # Preserve the initialization error. If reservation, identity
                # lookup, or best-effort cleanup fails, leaving artifacts is
                # safer than deleting a checkout whose ownership is unknown.
                pass
        tracer.conn.close()
        raise
    _reconcile_dead_runs(tracer, adw_id)   # hard-killed runs stop reading 'running'
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by adw_id.
    tracer.process_start(adw_id, "adw", "", os.getpid(),
                         " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
    _finalize_when_killed(run)
    run.console.session_started(adw_id, run.engineer)
    return run
