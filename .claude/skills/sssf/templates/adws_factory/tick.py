"""One pass of the orchestration layer.

    reap      for each tracked run: artifact present? -> done
                                    terminal, no artifact? -> classify the failure
                                    still going? -> leave it
    supervise any decision point for the active item -> wake the decider
    select    eligible (item, next stage) pairs, filtered and ordered
    launch    up to the limits, detached, then record and exit

A tick holds no state of its own between passes. Everything it concludes is
either re-derived from artifacts next time or written to the state file, so a
tick killed at any point costs at most one pass.

The ordering is not arbitrary. Reaping first means the selection step sees an
accurate picture of what is in flight; selecting before reaping would launch a
second writer against a branch whose first writer just finished.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, escalate, labels, launcher
from .artifacts import GhError, PullRequest
from .config import FactoryConfig, Stage
from .decider import Verdict, _fingerprint, _moment, decide
from .gates import check_disk, check_credits
from .paths import FactoryPaths
from .run_records import classify_failure, read_run
from .state import FactoryState, ItemBudget, TrackedRun

__version__ = "0.1.6"


@dataclass
class TickReport:
    """What one pass did. Printed by the CLI and written to the tick log."""

    started_at: str
    reaped: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"tick {self.started_at}"]
        for label, entries in (
            ("reaped", self.reaped),
            ("launched", self.launched),
            ("skipped", self.skipped),
            ("notes", self.notes),
            ("errors", self.errors),
        ):
            for entry in entries:
                parts.append(f"  {label}: {entry}")
        return "\n".join(parts)


class TickLockHeld(RuntimeError):
    """Another tick holds the lock. The only failure `cmd_loop` may shrug off.

    Its own type because `except RuntimeError` there also caught `GhError`,
    which subclasses RuntimeError: a real repeating fault printed "tick skipped"
    every 30 seconds forever and a dead factory looked busy.
    """


# Diagnostic age for abandoned PID markers. Age never permits stealing an
# OS lock or a marker naming a live writer. Dead-owner markers are reclaimed
# inside the persistent OS guard shared by ticks and supervisor recovery.
TICK_LOCK_MAX_AGE_SECONDS = 3600


class TickLock:
    """Keeps a manual run from colliding with the loop.

    A stale lock is honoured only while its owner is alive; a tick killed mid-
    pass must not wedge the factory until someone notices.
    """

    def __init__(self, path: Path, max_age_seconds: int = TICK_LOCK_MAX_AGE_SECONDS):
        self.path = path
        self.max_age_seconds = max_age_seconds
        self.acquired = False
        self.guard = None

    def __enter__(self) -> "TickLock":
        # Persistent OS lock closes the old exists/unlink/write race. Import
        # lazily because supervisor also imports the tick's public helpers.
        from .supervisor import SupervisorPollLock, SupervisorPollLockHeld
        self.guard = SupervisorPollLock(self.path.with_suffix(".guard"))
        try:
            self.guard.__enter__()
        except SupervisorPollLockHeld as error:
            raise TickLockHeld(f"another tick holds {self.path}") from error
        try:
            if self.path.exists() and self._holder_alive():
                raise TickLockHeld(f"another tick holds {self.path}")
            self.path.write_text(str(os.getpid()), encoding="utf-8")
            self.acquired = True
            return self
        except BaseException:
            self.guard.__exit__(None, None, None)
            raise

    def __exit__(self, *exc_info) -> None:
        try:
            if self.acquired:
                self.path.unlink(missing_ok=True)
        finally:
            if self.guard:
                self.guard.__exit__(*exc_info)

    def _holder_alive(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        # Age alone never grants permission to race a live writer.
        return launcher.is_alive(pid)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prune_raw_output(paths: FactoryPaths, adw_id: str) -> int:
    """Delete a finished run's raw agent streams. Returns bytes reclaimed.

    Nobody reads the raw stream of a stage that worked, and it is by far the
    largest thing a run leaves behind -- single files of 1.4 GB and 2.9 GB have
    been observed.

    This is about DISK, not about the guard's snapshot, and the difference cost
    an investigation. The run store does sit inside the checkout, but
    `safety_snapshot` excludes `defaults.data_dir` from the canonical root, so
    pruning here does not shrink a snapshot: 1163 MB was deleted by hand and the
    snapshot moved by 27 KB. What grows the snapshot is stale sibling worktrees,
    which get no exclusions at all -- see `prune_worktree`.

    Everything durable is kept. events.jsonl, the envelopes, the prompts and the
    context handoff are what the dashboard and any later reader use; together
    they are a few megabytes. Only raw_output.jsonl goes.
    """
    session = paths.session_dir(adw_id)
    if not session.is_dir():
        return 0

    reclaimed = 0
    for raw in session.rglob("raw_output.jsonl"):
        try:
            size = raw.stat().st_size
            raw.unlink()
            reclaimed += size
        except OSError:
            # A file we cannot remove is not worth failing a tick over; the
            # disk floor will notice if this becomes a real problem.
            continue
    return reclaimed


def prune_worktree(paths: FactoryPaths, adw_id: str) -> str:
    """Remove a finished run's worktree, unless it still holds work. Note or "".

    This is the accumulation that actually breaks the guard, and it took a while
    to see because the obvious suspect was innocent. `safety_snapshot` walks the
    canonical checkout AND every registered worktree, and applies the run-store
    exclusion only to canonical -- so siblings are copied whole, every agent
    phase. Ten stale worktrees at ~370 MB each is 3.7 GB before the run's own
    work is counted, and the 4 GiB budget then fails every later run as
    infrastructure before it starts. Nothing ever removed them.

    (Pruning the run store instead frees nothing: it is already excluded. 1163 MB
    of it was deleted by hand and the snapshot shrank by 27 KB.)

    Refuses to remove a worktree with uncommitted changes, and that refusal is
    not theoretical: a p3 run passed every gate, was failed by an unrelated
    harness fault, and its finished fix sat uncommitted in exactly such a
    worktree for hours before being recovered from it. Cleanup must never be the
    thing that destroys work the factory failed to commit.
    """
    try:
        listing = subprocess.run(["git", "worktree", "list", "--porcelain"],
                                 cwd=str(paths.root), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if listing.returncode != 0:
        return ""

    targets = [line.split(" ", 1)[1].strip()
               for line in (listing.stdout or "").splitlines()
               if line.startswith("worktree ") and adw_id in line]
    for path in targets:
        try:
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=path,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if dirty.returncode == 0 and (dirty.stdout or "").strip():
            n = len((dirty.stdout or "").strip().splitlines())
            return f" (kept worktree: {n} uncommitted file(s))"
        try:
            removed = subprocess.run(["git", "worktree", "remove", "--force", path],
                                     cwd=str(paths.root), capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError):
            continue
        if removed.returncode == 0:
            return " (worktree removed)"
    return ""


def review_is_pending(
    config: FactoryConfig, paths: FactoryPaths, pr: PullRequest
) -> bool:
    """Has a full review of this head been asked for, and not yet timed out?

    The signal is the request comment the factory posts on the pull request
    (ADR-0005), so it survives a lost state file and is visible to a person
    reading the PR.

    Bounded on purpose. The reviewer sometimes answers without pinning a review
    to the head, in which case no request is ever satisfied by this test and an
    unbounded version would relaunch the fix stage until the launch ceiling, 15
    minutes at a time. Past the grace window this returns False, the stage reads
    done, and the decider takes over -- it asks whether the reviewer has SPOKEN
    since the head landed, which is the question that has an answer in that case.

    Fails CLOSED, in the sense that costs least: an unreadable timestamp means
    not pending, so the item proceeds to the decider rather than parking a run
    on a wait that cannot be reasoned about.
    """
    try:
        requested_at = artifacts.full_review_requested_at(
            config.repo, pr.number, pr.revision, paths.root
        )
    except GhError:
        return False
    if requested_at is None:
        return False
    moment = _moment(requested_at)
    if moment is None:
        return False
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    return age < config.limits.external_review_grace_seconds


def stage_is_done(
    config: FactoryConfig,
    paths: FactoryPaths,
    stage: Stage,
    item: int,
    pr: PullRequest | None,
) -> bool:
    """Is this stage's artifact present and pinned to the current revision?

    The single question the whole design turns on (ADR-0003). Run status is not
    consulted here at all -- only whether the durable thing exists.
    """
    if stage.artifact == "open_pull_request":
        return pr is not None

    if pr is None:
        return False

    if stage.artifact == "pinned_review":
        return artifacts.pinned_review(config.repo, pr, paths.root) is not None

    if stage.artifact == "ledger_entry":
        # The ACTIONABLE one: this must name the same review p3 will consume,
        # or the two disagree forever and the tick relaunches p3 every tick.
        external = artifacts.actionable_external_review(config.repo, pr, paths.root)
        if external is None:
            # No review pinned to this head. Two very different situations, and
            # collapsing them into "done" is what kept the review and fix stages
            # serialised even after they could be selected as a pair: right
            # after a fix pushes, the reviewer has not answered yet, so the fix
            # stage read as done and only the reviewer was launched.
            #
            # If a full review of THIS head was requested and is still within
            # the grace window, the work is coming. Report the stage unfinished
            # so it launches alongside the reviewer and spends the wait in its
            # own await step, which is what makes the overlap real.
            #
            # Otherwise nothing is coming and the stage is done -- the original
            # rule, and still the one that prevents waiting forever for a review
            # the reviewer has decided not to write.
            return not review_is_pending(config, paths, pr)
        return artifacts.ledger_entry(
            paths.root, config.repo, pr.number, external.review_id
        ) is not None

    raise ValueError(f"unknown artifact probe {stage.artifact!r}")


def unsatisfied_stages(
    config: FactoryConfig, paths: FactoryPaths, item: int
) -> tuple[list[Stage], PullRequest | None]:
    """Every stage whose artifact is missing, in sequence order, and the item's PR.

    Walking forward from the start each tick -- rather than remembering a
    position -- is what makes a fix pull the item back a stage automatically:
    the new head invalidates the earlier review, so that stage stops being done.

    Every unsatisfied stage is reported, not just the first, because the review
    and fix stages are meant to be in flight together (orchestration.md,
    "Concurrency"). The fix workflow's first action is to wait for the reviews,
    so starting it beside the reviewer turns that wait into overlap. Reporting
    only the first stage is what made the pair impossible to select.

    A stage that needs a pull request is omitted while there is none: it cannot
    be launched, and listing it would only produce an error in `launch_stage`.
    """
    pr = artifacts.open_pull_request(config.repo, item, paths.root)
    pending = [
        stage
        for stage in config.stages
        if not stage_is_done(config, paths, stage, item, pr)
        and not (stage.arg == "pr" and pr is None)
    ]
    return pending, pr


def current_position(
    config: FactoryConfig, paths: FactoryPaths, item: int
) -> tuple[Stage | None, PullRequest | None]:
    """The first stage whose artifact is missing, and the item's pull request.

    The single-stage view of `unsatisfied_stages`, kept for the callers that
    genuinely want one name for where an item sits -- the blocked label and the
    status projection -- rather than the set of what can run.
    """
    pending, pr = unsatisfied_stages(config, paths, item)
    return (pending[0] if pending else None), pr


def request_review_if_head_moved(
    config: FactoryConfig,
    paths: FactoryPaths,
    stage: Stage,
    run: TrackedRun,
    pr: PullRequest | None,
    report: TickReport,
    dry_run: bool = False,
) -> str:
    """Ask the external reviewer to look again, the moment a writer moves the head.

    The reviewer is incremental and frequently says nothing at all about a
    pushed commit (ADR-0005), so the factory has to ask. Asking here rather than
    in the decider is what lets the reviewer's turnaround overlap the review
    stage instead of following it -- and once the review and fix stages run as a
    pair, it is not an optimisation but a precondition: the fix stage waits for
    a review that nobody would otherwise have requested, and times out.

    Deduplicated on the revision by `full_review_requested_at`, so the decider
    asking later for the same head is a no-op rather than a second comment. A
    failure to ask is not fatal: the decider still checks coverage before it
    rules, so the worst case is the slower path, not an unreviewed merge.
    """
    if not stage.is_writer or pr is None:
        return ""
    if not run.base_revision or run.base_revision == pr.revision:
        return ""  # the writer landed no new commit; the reviewer has nothing new

    try:
        if artifacts.full_review_requested_at(
            config.repo, pr.number, pr.revision, paths.root
        ) is not None:
            return ""
        if dry_run:
            return f" (would request a full review of {pr.revision[:8]})"
        artifacts.request_full_review(config.repo, pr.number, pr.revision, paths.root)
    except GhError as error:
        report.notes.append(
            f"#{run.item}: could not request a review of {pr.revision[:8]}: {error}"
        )
        return ""
    return f" (requested a full review of {pr.revision[:8]})"


def _streak_note(config: FactoryConfig, streak: int) -> str:
    """The repetition, said out loud in the reap line a person reads.

    Ten reap lines in a row said "unknown error" and nothing said they were
    the same error. The count is what turns a log into a signal.
    """
    if streak < 2:
        return ""
    return f" [same failure {streak}x/{config.limits.repeat_failure_ceiling}]"


def reap(
    config: FactoryConfig, paths: FactoryPaths, state: FactoryState, report: TickReport,
    dry_run: bool = False,
    only_ids: set[str] | None = None,
) -> None:
    """Judge every tracked run: done, failed, or still going.

    Under `dry_run` this reports and touches nothing outside memory. Untracking
    is safe either way -- a dry run never saves state -- but deleting raw output
    is not, and a `plan` pass once removed 306 MB it was only asked to describe.
    """
    for run in list(state.tracked):
        if only_ids is not None and run.adw_id not in only_ids:
            continue
        record = read_run(paths.tracer_db, run.adw_id)
        alive = launcher.is_alive(run.pid)

        if record is None and alive:
            continue  # started, has not registered itself yet
        if record is not None and not record.is_terminal and alive:
            continue  # still working

        stage = config.stage_by_name(run.stage)
        if stage is None:
            state.untrack(run.adw_id)
            report.notes.append(f"{run.adw_id} referenced unknown stage {run.stage}")
            continue

        try:
            pr = artifacts.open_pull_request(config.repo, run.item, paths.root)
            done = stage_is_done(config, paths, stage, run.item, pr)
        except GhError as error:
            report.errors.append(f"{run.adw_id}: cannot read artifacts: {error}")
            continue

        budget = state.budget(run.item)
        # The run has stopped, so its worktree is dead weight that every LATER
        # run pays to snapshot. Removed here rather than on a schedule, because
        # this is the moment the factory knows the run is over -- and a worktree
        # pruned while a run is live has killed a healthy phase before.
        worktree_note = "" if dry_run else prune_worktree(paths, run.adw_id)
        if done:
            # The artifact landed. How the run ended is not consulted -- a run
            # that pushed its work and then failed a health check still did it.
            asked = request_review_if_head_moved(
                config, paths, stage, run, pr, report, dry_run=dry_run
            )
            budget.record_progress()
            if stage.name == config.stages[-1].name:
                # Completing the last stage is what closes a review-then-fix
                # round, so it is counted here, against the artifact -- the same
                # rule every other conclusion in this module follows.
                budget.rounds += 1
            state.untrack(run.adw_id)
            reclaimed = 0 if dry_run else prune_raw_output(paths, run.adw_id)
            note = f" (freed {reclaimed / 1024**2:.0f} MB)" if reclaimed else ""
            if dry_run:
                note = " (would prune raw output)"
            report.reaped.append(
                f"{run.adw_id} {run.stage} on #{run.item}: artifact present"
                f"{note}{worktree_note}{asked}"
            )
            continue

        if record is None:
            # No record means the run died before it reached a phase, so it
            # produced no work to judge. Charging the work budget for it broke
            # this module's own rule that only judged work spends that budget,
            # and parked healthy items on crashes nobody's agent caused. The
            # launch ceiling still terminates a run that dies every time.
            state.untrack(run.adw_id)
            streak = budget.record_failure(f"{run.stage}:no-record")
            report.reaped.append(
                f"{run.adw_id} {run.stage} on #{run.item}: infrastructure -- "
                f"vanished with no record{worktree_note}{_streak_note(config, streak)}"
            )
            continue

        if record.succeeded:
            # The run was fine; the stage re-opened under it -- a new external
            # review landing seconds after it finished is the usual cause.
            # Describing this as a failure with nothing recorded reads as a bug
            # in the factory and costs someone a debugging session.
            state.untrack(run.adw_id)
            budget.record_progress()
            report.reaped.append(
                f"{run.adw_id} {run.stage} on #{run.item}: run succeeded but the "
                f"stage is open again -- new work arrived after it finished"
            )
            continue

        failure_class, detail = classify_failure(record)
        state.untrack(run.adw_id)
        # Counted for EVERY class, before the classes diverge. The work budget
        # is deliberately spared by two of the three, which is why an item can
        # fail identically ten times with that budget at 1 (issue #281).
        streak = budget.record_failure(f"{run.stage}:{failure_class}:{detail}")
        repeats = _streak_note(config, streak)
        if failure_class == "work":
            budget.spend_work_attempt()
            report.reaped.append(
                f"{run.adw_id} {run.stage} on #{run.item}: work failure "
                f"({budget.work_attempts}/{config.limits.work_attempts}) -- {detail}"
                f"{worktree_note}{repeats}"
            )
        else:
            # Infrastructure and unknown both spare the work budget. Unknown
            # errs this way on purpose: the launch ceiling still terminates a
            # wedged environment, and parking a healthy item is the worse harm.
            report.reaped.append(
                f"{run.adw_id} {run.stage} on #{run.item}: {failure_class} -- "
                f"{detail}{worktree_note}{repeats}"
            )


def _escalate_stuck(
    config: FactoryConfig,
    paths: FactoryPaths,
    item: int,
    verdict: Verdict,
    budget: ItemBudget,
    report: TickReport,
    dry_run: bool,
) -> Verdict:
    """Turn a terminal hold into an escalation, carrying the hold's own reason.

    The item is parked by the caller (`_judge`) once this returns, the same as
    every other escalated verdict -- this function only decides whether one is
    warranted and sends it.
    """
    reason = f"stuck after {budget.rounds} round(s): {verdict.detail}"
    fingerprint = _fingerprint(reason)
    stuck = Verdict("escalated", reason, findings=verdict.findings,
                    fingerprint=fingerprint)
    if dry_run:
        report.notes.append(f"would park #{item} blocked at {config.stages[-1].name}")
        return stuck

    if fingerprint == budget.escalated_fingerprint:
        # Reported already and nothing has changed. Staying quiet is the point:
        # the state repeats every tick, the news does not.
        stuck.escalated = True
        stuck.detail += " (already escalated; not repeating)"
        return stuck

    result = escalate.escalate_item(config.repo, item, reason)
    stuck.escalated = result.delivered
    if not result.delivered:
        stuck.detail += f" (escalation FAILED: {result.detail})"
    # Parking happens once, in _judge, for every escalated verdict alike --
    # not just this one. See _judge for why.
    return stuck


def _judge(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    item: int,
    why: str,
    report: TickReport,
    dry_run: bool,
) -> None:
    """Send one item to the decider and record what came back.

    Shared by the two ways an item can run out of road, so neither can quietly
    grow its own handling: every stage done, and rounds exhausted.
    """
    budget = state.budget(item)
    state.active_item = item
    verdict = decide(config, paths, item, dry_run=dry_run,
                     escalation_seen=budget.escalated_fingerprint)

    if (verdict.action == "held" and not verdict.awaiting_reviewer
            and not verdict.transient
            and (verdict.terminal
                 or budget.rounds >= config.limits.max_rounds)):
        # A hold means "waiting for the next review". With the rounds spent
        # there is no next review coming from the factory, so the wait has no
        # end the factory can reach -- it re-checks forever and says so only in
        # a log nobody reads. Same hole that swallowed issue #4 before the
        # rounds-exhausted path started judging instead of skipping.
        #
        # Deliberately no timeout: how long the external reviewer takes is not
        # knowable, so the trigger is the terminal state, not a clock.
        #
        # A `transient` hold is not terminal either, and for the same reason
        # reap refuses to spend the work budget on an infrastructure failure:
        # one `gh` call erroring says nothing about whether the work is done.
        # Parking on it retired an item permanently on a network blip, and
        # blocked is only ever cleared by a person.
        #
        # `verdict.terminal` escalates on its own, without waiting for the
        # rounds. Rounds only advance when a stage RUNS, and an item whose
        # stages all read done never runs one -- so an item the reviewer has
        # abandoned sits at rounds 0 forever and this condition never fires.
        # #12/PR #101 held silently for eight hours that way.
        #
        # `awaiting_reviewer` is the one hold that is NOT terminal: the decider
        # has just asked the reviewer for a ruling, and asking is a move. Parking
        # on it would post the request and stop the item in the same tick, so the
        # answer could never be acted on.
        verdict = _escalate_stuck(config, paths, item, verdict, budget,
                                  report, dry_run)

    if verdict.action == "escalated":
        # Escalating and continuing are alternatives, not companions: an item
        # handed to a person must stop, or the decision they were asked to make
        # gets made without them. Applies to EVERY escalated verdict -- a
        # blocking finding straight out of `decide()` no less than a hold
        # converted above -- because both mean the same thing to the operator.
        stage_name = config.stages[-1].name
        if dry_run:
            verdict.detail += f" (would park at {stage_name})"
        else:
            if not verdict.escalated:
                report.errors.append(f"#{item}: escalation did not reach anyone")
            else:
                budget.escalated_fingerprint = verdict.fingerprint
            # Park regardless of delivery. A message that did not arrive does
            # not make the item any less stuck, and the operator checks the
            # machine directly often enough that the label alone is sufficient.
            budget.block(stage_name, verdict.detail)
            try:
                labels.mark_blocked(config, item, stage_name, paths.root)
            except labels.LabelError as error:
                report.errors.append(f"#{item}: cannot mark blocked: {error}")
            verdict.detail += f" (parked at {stage_name})"

    report.notes.append(f"#{item} ({why}): decider {verdict.action} -- {verdict.detail}")

    if verdict.action == "merged" and not dry_run:
        state.clear_budget(item)
        state.active_item = None


def select(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    report: TickReport,
    dry_run: bool = False,
) -> list[tuple[int, Stage, PullRequest | None]]:
    """The (item, stage) pairs to launch this tick. Empty when there is nothing.

    One item at a time: the historical cause of lost work was several items in
    flight sharing branches and worktrees. Within that item, every stage that
    can run does -- in practice the reader and the writer as a pair, which is
    what the class limits in `Limits` are sized for. `launch_stage` enforces
    those limits per launch, so this function may propose more than will start.
    """
    try:
        candidates = labels.eligible_items(config, paths.root)
    except labels.LabelError as error:
        report.errors.append(f"cannot list eligible items: {error}")
        return []

    if not candidates:
        report.notes.append("no eligible items")
        return []

    active = state.active_item
    if active is not None and active in candidates:
        candidates = [active] + [item for item in candidates if item != active]

    for item in candidates:
        budget = state.budget(item)

        try:
            parked = labels.blocked_stage(config, item, paths.root)
        except labels.LabelError as error:
            report.errors.append(f"#{item}: cannot read labels: {error}")
            continue
        if parked:
            report.skipped.append(f"#{item}: blocked at {parked}")
            continue
        if budget.is_blocked:
            # The label is gone but the state still says blocked, which means a
            # person just cleared it -- the documented way to unblock, and the
            # only one: `labels.blocked_stage` reads GitHub precisely so that
            # "a person clearing the label is how blocked is lifted".
            #
            # Without this the human action had nothing listening for it. The
            # tick fell through to the state gate and reported the same block
            # forever, and the only way out was editing state.json by hand.
            # The predecessor orchestrator got this right -- deleting the label
            # reset the attempt baseline -- and this inherited its label
            # semantics without the reconcile.
            #
            # Clearing the whole budget, not just the block: an item parked at
            # rounds 3/3 would re-block on its next verdict, so resetting the
            # flag alone unblocks nothing. This is the case clear_budget's
            # docstring names -- "Only a person unblocking should cause this".
            report.notes.append(
                f"#{item}: unblocked by a person (blocked label cleared at "
                f"{budget.blocked_stage}); budget reset"
            )
            state.clear_budget(item)
            budget = state.budget(item)

        # Before anything else about this item: is the work it builds on
        # actually on main? These issues chain (#4 -> #5 -> #6 -> #7), and FIFO
        # alone would start a child the moment its parent had a branch, not a
        # merge.
        try:
            waiting = artifacts.unmet_dependencies(config.repo, item, paths.root)
        except GhError as error:
            report.errors.append(f"#{item}: cannot read dependencies: {error}")
            continue
        if waiting:
            report.skipped.append(
                f"#{item}: waiting on " + ", ".join(f"#{n}" for n in waiting)
            )
            continue

        if budget.launches >= config.limits.launch_ceiling:
            report.skipped.append(
                f"#{item}: launch ceiling {config.limits.launch_ceiling} reached"
            )
            continue

        if state.runs_for(item):
            # Still item-level, deliberately. The pair this function exists to
            # select is launched from an idle item in one tick, so it never
            # needs to join a run already going -- and letting it would put a
            # reader against a head an in-flight writer is still moving.
            report.skipped.append(f"#{item}: already has a run in flight")
            continue

        try:
            pending, pr = unsatisfied_stages(config, paths, item)
        except GhError as error:
            report.errors.append(f"#{item}: cannot read artifacts: {error}")
            continue

        if not pending:
            _judge(config, paths, state, item, "all stages done", report, dry_run)
            continue

        stage = pending[0]
        if budget.rounds >= config.limits.max_rounds and stage.name != config.stages[0].name:
            # Rounds exhausted is a TERMINAL state, not a reason to skip. This
            # path used to `continue` while reporting "-> decider", so an item
            # that reached the cap was never launched and never judged -- it
            # dropped out of the factory silently and stayed there. Issue #4 sat
            # in that hole with a legitimate next stage waiting to run.
            _judge(config, paths, state, item,
                   f"{budget.rounds} rounds spent", report, dry_run)
            continue

        # The ceiling was checked once above, but this returns up to one launch
        # per stage and each spends one. Trim to what the budget actually has,
        # or a pair walks the item one launch past its hard ceiling.
        headroom = config.limits.launch_ceiling - budget.launches
        state.active_item = item
        return [(item, stage, pr) for stage in pending[:max(0, headroom)]]

    return []


def launch_stage(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    item: int,
    stage: Stage,
    pr: PullRequest | None,
    report: TickReport,
) -> None:
    """Run the gates, then start the stage detached and record it."""
    if stage.is_writer and state.writer_runs():
        report.skipped.append(f"#{item} {stage.name}: a writer run is already in flight")
        return
    if not stage.is_writer and len(state.reader_runs()) >= config.limits.max_reader_runs:
        report.skipped.append(f"#{item} {stage.name}: reader limit reached")
        return

    for gate in (check_disk(config, paths.root), check_credits(config, stage, paths.root)):
        if not gate.passed:
            # A refused launch is infrastructure: it spends no budget at all,
            # not even the launch ceiling, because nothing was started.
            report.skipped.append(f"#{item} {stage.name}: {gate.gate} gate -- {gate.detail}")
            return

    target = item if stage.arg == "issue" else (pr.number if pr else None)
    if target is None:
        report.errors.append(f"#{item} {stage.name}: needs a pull request and none exists")
        return

    try:
        started = launcher.launch(
            stage=stage,
            item_or_pr=target,
            root=paths.root,
            log_dir=paths.factory_dir / "logs",
            config_rel=config.sssf_config,
        )
    except launcher.LaunchError as error:
        report.errors.append(f"#{item} {stage.name}: {error}")
        return

    budget = state.budget(item)
    budget.spend_launch()
    # A round closes when the last stage LANDS its artifact, which is reap's
    # job, not this one. Counting it here counted launches: a run that died on
    # startup still spent a round, and two of those sent an item to the decider
    # having never completed a single review-then-fix cycle.

    state.track(
        TrackedRun(
            adw_id=started.adw_id,
            item=item,
            stage=stage.name,
            run_class=stage.run_class,
            pid=started.pid,
            base_revision=pr.revision if pr else None,
        )
    )
    report.launched.append(
        f"#{item} {stage.name} {stage.workflow} adw_id={started.adw_id} pid={started.pid}"
    )


def enforce_budgets(
    config: FactoryConfig, paths: FactoryPaths, state: FactoryState, report: TickReport,
    dry_run: bool = False,
) -> None:
    """Park items whose budget is spent or that keep failing the same way.

    Two independent reasons, one pass. The streak is not a second work budget:
    it stops an item that repeats ITSELF, whatever class its failures are, and
    the reason it records is the failure's own text -- so a person reading
    `state.json` afterwards is told what kept happening rather than finding
    `blocked_reason: null` and a spent launch count.
    """
    for key, budget in state.budgets.items():
        if budget.is_blocked:
            continue
        spent = budget.work_attempts >= config.limits.work_attempts
        repeating = budget.failure_streak >= config.limits.repeat_failure_ceiling
        at_ceiling = budget.launches >= config.limits.launch_ceiling
        if not (spent or repeating or at_ceiling):
            continue
        if at_ceiling and not (spent or repeating):
            # The ceiling stops launches by itself, in `select`, but it wrote
            # nothing down: #281 sat there with `blocked_reason: null`, so
            # neither a person nor a later tick could say what had stopped it.
            reason = (f"launch ceiling {config.limits.launch_ceiling} reached; "
                      f"last failure: {budget.failure_signature or 'not recorded'}")
        elif repeating:
            reason = (f"{budget.failure_streak} identical failures: "
                      f"{budget.failure_signature}")
        else:
            reason = f"{budget.work_attempts} work failures"
        item = int(key)
        try:
            stage, _ = current_position(config, paths, item)
        except GhError:
            continue
        stage_name = stage.name if stage else config.stages[-1].name
        if dry_run:
            report.notes.append(f"would park #{item} blocked at {stage_name}: {reason}")
            continue
        budget.block(stage_name, reason)
        try:
            labels.mark_blocked(config, item, stage_name, paths.root)
        except labels.LabelError as error:
            report.errors.append(f"#{item}: cannot mark blocked: {error}")
        report.notes.append(f"#{item}: parked blocked at {stage_name}: {reason}")


def run_tick(config: FactoryConfig, paths: FactoryPaths, dry_run: bool = False) -> TickReport:
    """One pass. Returns what it did."""
    report = TickReport(started_at=_now())
    paths.ensure_state_dir()

    with TickLock(paths.tick_lock):
        from .recovery import paused
        if paused(paths) and not dry_run:
            report.notes.append("maintenance pause: no state changes or launches")
            return report
        state = FactoryState.load(paths.state_file)

        try:
            if dry_run:
                # A dry run must not touch the world it is reporting on. Report
                # what would be created instead of creating it.
                present = labels.existing_labels(config.repo, paths.root)
                missing = [
                    name
                    for name in config.labels.all_managed(config.stages)
                    if name not in present
                ]
                if missing:
                    report.notes.append(f"would create labels: {', '.join(missing)}")
            else:
                created = labels.ensure_labels(config, paths.root)
                if created:
                    report.notes.append(f"created labels: {', '.join(created)}")
        except labels.LabelError as error:
            # Every label must exist before anything is written. Refusing the
            # whole tick is deliberate: a missing label previously crashed a
            # tick partway through, leaving items half-labelled.
            report.errors.append(f"label setup failed, tick aborted: {error}")
            return report

        reap(config, paths, state, report, dry_run=dry_run)
        enforce_budgets(config, paths, state, report, dry_run=dry_run)

        for item, stage, pr in select(config, paths, state, report, dry_run=dry_run):
            if dry_run:
                report.notes.append(f"would launch #{item} {stage.name} {stage.workflow}")
            else:
                launch_stage(config, paths, state, item, stage, pr, report)

        if not dry_run:
            state.save(paths.state_file)

    try:
        with open(paths.tick_log, "a", encoding="utf-8") as log:
            log.write(report.line() + "\n")
    except OSError:
        pass

    return report
