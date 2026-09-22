"""The supervisor: a scheduled poll that answers "is the factory still moving?".

The failure this exists for is the loop being dead while nobody watches. On
2026-08-17 a detached loop ticked for 17 hours while three items waited on a
person, and a handoff written in the middle recorded the factory as stopped --
nobody was lying, nobody could see it. A poll on its own schedule catches the
opposite failure: the loop that STOPPED. It runs as its own Windows Scheduled
Task (`install-supervisor.ps1`), independent of whoever drives the loop, so the
watcher does not die with the watched.

Hard boundaries, all deliberate (docs/supervisor.md):

* The tick owns `state.json`. The supervisor reads it and never saves it
  through the tick's own path: a second writer against factory state is how
  the predecessor project lost a gate approval to a race. The supervisor's
  own memory -- repeat counters and last-fired signatures -- lives in
  `supervisor-state.json`. The ONE deliberate exception is a separately
  armed item action (below), executed under the tick lock, writing through
  `FactoryState.save_action` so it can never masquerade as a factory tick.
* It never rules on a review finding, never merges, never touches a branch,
  never launches a workflow, and spends no model quota. A healthy poll costs
  one status write and nothing else.
* `supervisor-status.json` is replaced, never appended: a status file that
  grows with every poll is a second log nobody reads. The immutable item
  action records live in their own directory and survive that replacement.

Two signals are live here:

* `loop_dead`, the case where the last tick is older than the silence
  window, items are still open, and nothing alive holds the tick lock or a
  driver lease. That combination pages a person through the same transport
  the decider uses.
* `item_stalled`, the case where the active item's artifact-derived
  situation is byte-for-byte unchanged poll after poll while no tracked run
  is alive -- the shape of an item quietly waiting on a person nobody told.
  The foreground driver prints this as a `STALL` line for whoever is
  watching; this is the same observation made when nobody is.

And one that classifies rather than pages on the decider's clock:

* `escalation_unanswered`, the case where the decider asked for a person,
  recorded what it asked, and nobody has come. The decider already pages
  once per distinct blocker and refuses to repeat itself; nothing noticed
  that the page went unanswered. The supervisor watches each unchanged
  escalation subject for a human-response grace window and then reports it
  as its own decision point -- separate from `item_stalled`, because the
  cause is known and a person has already been asked, and an item with a
  known unresolved escalation is never also called stalled.

And three that read the machine itself (issue #111), because "nothing is
happening" has two shapes -- the work being done, and the machine being
unable to run -- and an operator reading "healthy, nothing to act on"
while the disk is full learns nothing:

* `disk_below_floor`, the filesystem holding the factory checkout having
  less free space than the launch floor. A condition to wait out, and
  saying so is the point.
* `provider_credit_exhausted`, every metered provider the active item's
  next launchable stage needs being at or below the credit floor. Also a
  condition to wait out -- but a named one, per provider and stage.
* `run_vanished`, a tracked run whose process is gone with no artifact to
  show for it. Different from the first two: it holds a slot that will
  never free on its own, which is news even when the disk is fine.

All three are read from the same structured readings the launch gates use
(`gates.py` owns the thresholds), never parse gate prose, never classify a
stale or unreadable input as a fault, and deduplicate on what was paged the
way the decider already deduplicates its own escalations.

When a signal fires, one more thing happens: a read-only, one-shot
 diagnosis agent wakes for that signal and item, works out what is
  actually wrong, writes its whole output to a timestamped log under the
  factory directory, and exits. Detection stays cheap and deterministic;
  judgement is paid for only at this decision point -- a healthy poll still
  costs one status write and nothing else. The agent is observational except
  for five narrow item-action tools, each of which only RECORDS the action it
  names (see `item_actions.py`): every flag defaults to dry-run, the record
  is the deliverable, and only a separately armed flag lets a later poll
  perform that one named action under the tick lock. The agent still never
  rules on a review finding, never merges, and never watches or promises to
  check back. Two guards, both learned from the predecessor project: an
  inter-process poll lock (two drivers invoking one poll spawned two agents
  on the same signal and reset a good pull request) and a per-signal launch
  grace window (without one a new agent stacks every tick while the first
  is still working).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":  # pragma: no branch - one branch per platform
    import msvcrt
else:
    import fcntl

from . import artifacts, escalate, gates, item_actions, labels, launcher
from .artifacts import GhError, PullRequest, Review
from .config import FactoryConfig
from .paths import FactoryPaths
from .state import FactoryState, ItemBudget
from .tick import (
    TICK_LOCK_MAX_AGE_SECONDS,
    TickLock,
    TickLockHeld,
    current_position,
)

__version__ = "0.1.1"

SCHEMA_VERSION = 1
LOOP_DEAD = "loop_dead"
ITEM_STALLED = "item_stalled"
ESCALATION_UNANSWERED = "escalation_unanswered"
# The three infrastructure faults (issue #111): machine conditions that stop
# the factory without anything being wrong with the work. Distinct from the
# signals above because each names its own cause and its own fix -- an
# operator reading "healthy, nothing to act on" while the disk is full
# learns nothing.
DISK_BELOW_FLOOR = "disk_below_floor"
PROVIDER_CREDIT_EXHAUSTED = "provider_credit_exhausted"
RUN_VANISHED = "run_vanished"


# ---------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _stamp(moment: datetime) -> str:
    """A full-precision UTC ISO stamp, for clocks the poll itself keeps.

    `_iso` truncates to whole seconds, which is right for a status file a
    person reads and wrong for a grace window: a watch first seen half a
    second into a poll would lose that half second and read overdue at the
    exact grace boundary. A watch keeps every fraction it was given, so
    `elapsed == grace` stays suppressed as specified.
    """
    return moment.astimezone(timezone.utc).isoformat()


def _moment(stamp: str | None) -> datetime | None:
    """Parse a timestamp written by the factory. Naive means UTC. None on failure."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical_tick(stamp: str | None) -> str:
    """One representation per instant, so equivalent stamps sign identically.

    The factory writes UTC with `+00:00`, but a hand-edited `state.json` or a
    future writer may say the same moment as `Z`, as a naive local time, or
    with another offset. The raw string feeds those differences straight into
    the incident signature, letting one incident look like two and page a
    person twice. Unparseable stamps keep their raw text: an unreadable tick
    is a fact of its own, not a fact to normalize away.
    """
    moment = _moment(stamp)
    if moment is None:
        return str(stamp)
    return moment.astimezone(timezone.utc).isoformat()


def _fingerprint(*parts: str) -> str:
    """A stable signature for one incident.

    Built from the signal name and the incident's own facts -- the last tick
    and the open item set -- never from the poll time or the computed age,
    which change every minute and would make every poll a new incident.
    """
    return hashlib.sha256("|".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text`, whole. Never leaves a partial file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        # `newline` pinned so the file is byte-identical everywhere: the status
        # is replaced, not appended, and a test or tool diffing it against the
        # payload must not see platform line endings.
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        with suppress(OSError):
            Path(temp_name).unlink()
        raise


# ---------------------------------------------------------------- liveness

@dataclass(frozen=True)
class PidOwner:
    """What one PID file says. `detail` is written to the status, not parsed."""

    alive: bool
    pid: int | None
    detail: str


def _read_pid_marker(path: Path, max_age_seconds: float | None) -> PidOwner:
    """One PID file read: missing, malformed, stale, dead, or live."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return PidOwner(False, None, f"no {path.name}")
    if not raw:
        return PidOwner(False, None, f"empty {path.name}")
    try:
        pid = int(raw)
    except ValueError:
        return PidOwner(False, None, f"malformed {path.name}: {raw[:40]!r}")
    if (max_age_seconds is not None and time.time() - mtime > max_age_seconds
            and not launcher.is_alive(pid)):
        return PidOwner(
            False,
            pid,
            f"stale {path.name}: pid {pid} has held it for over "
            f"{max_age_seconds:.0f}s and a tick would reclaim it",
        )
    if launcher.is_alive(pid):
        return PidOwner(True, pid, f"{path.name} held by pid {pid}")
    return PidOwner(False, pid, f"{path.name} names dead pid {pid}")


def _moved_aside(path: Path) -> list[Path]:
    """Markers a release has claimed but not yet restored or deleted."""
    try:
        return sorted(path.parent.glob(f"{path.name}.*.releasing"))
    except OSError:
        return []


def read_pid_owner(path: Path, max_age_seconds: float | None = None) -> PidOwner:
    """Distinguish a live owner from a missing, malformed, stale or dead one.

    Liveness comes from `launcher.is_alive`, never from the file's age: a
    process killed mid-pass leaves its file behind, and honouring a dead PID
    would suppress exactly the signal this poll exists to send.

    A driver releasing its lease claims the marker's name with an atomic
    rename and, when the marker it claimed is a successor's, restores it.
    The marker is at exactly one of the two names at every instant -- the
    rename and the restoration are each atomic -- so this read looks at
    both, in an order that survives the restoration landing anywhere inside
    it: the named file, then the `*.releasing` siblings, then the named file
    again. Without that last look, a restoration completing between the
    first read and the sibling scan would leave both reads missing a marker
    that is standing, live, on the named path -- and the poll would report
    `loop_dead` against a loop that is running. Siblings left by a crashed
    releaser name a dead PID and stay harmless for the same reason the
    marker itself does.

    `max_age_seconds` only describes an abandoned tick marker. A live owner
    is always honoured, regardless of age: recovery cannot steal a writer's
    lock. The driver marker has no age bound.
    """
    reading = _read_pid_marker(path, max_age_seconds)
    if reading.alive:
        return reading
    for scratch in _moved_aside(path):
        candidate = _read_pid_marker(scratch, max_age_seconds)
        if candidate.alive:
            return candidate
    # The restoration may have moved the live marker back onto the named path
    # and deleted the scratch while the reads above were happening, leaving
    # the marker standing live at a path this function has already looked at.
    # The marker is never anywhere else, so one more look closes the window.
    recheck = _read_pid_marker(path, max_age_seconds)
    return recheck if recheck.alive else reading


class DriverLeaseError(RuntimeError):
    """The driver could not advertise itself, so it does not start.

    A driver that runs unadvertised is invisible to the supervisor's liveness
    check, and the poll that exists to catch a dead loop pages about a live
    one the moment the silence window passes. The failure that prevented the
    write (a broken runtime directory, a full disk) is also one the loop
    should not paper over by continuing.
    """


class DriverLease:
    """Advertise this process as the loop driver, for the supervisor's benefit.

    `TickLock` keeps two TICKS apart; it says nothing about the driver that
    spends most of its life sleeping between them. This marker answers the
    supervisor's "is anything alive driving the factory?" without scanning
    process command lines from the outside.

    An OS-backed lifetime guard rejects concurrent drivers. Entry replaces
    only a dead driver's marker. Release claims the
    name with an atomic rename before touching it -- a decision made on a
    read only speaks for the instant it happened, and a successor writing
    after that read would have its marker moved or deleted on stale
    evidence. When the marker just claimed turns out to be a successor's,
    it is restored exclusively, never over a newer driver's own marker; and
    while it is moved it stays readable at the claiming name, so the poll
    observes the live driver throughout and cannot manufacture the false
    `loop_dead` page this class exists to prevent. A crash leaves the file
    behind -- and process liveness makes that harmless.

    A write failure raises `DriverLeaseError` rather than being swallowed: a
    driver that cannot be seen is worse than no driver, because it actively
    manufactures false pages.
    """

    def __init__(self, path: Path):
        self.path = path
        self.pid = os.getpid()
        self.acquired = False
        self.guard = None

    def __enter__(self) -> "DriverLease":
        self.guard = SupervisorPollLock(self.path.with_suffix(".guard"))
        try:
            self.guard.__enter__()
        except (OSError, SupervisorPollLockHeld) as error:
            raise DriverLeaseError(f"cannot acquire driver lease {self.path}: {error}") from error
        try:
            if read_pid_owner(self.path).alive:
                raise OSError("another driver is alive")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(self.pid), encoding="utf-8")
        except OSError as error:
            self.guard.__exit__(None, None, None)
            raise DriverLeaseError(
                f"cannot write the driver marker {self.path}: {error}; "
                f"refusing to drive unadvertised -- the supervisor would page "
                f"about this loop as dead"
            ) from error
        self.acquired = True
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            self._release_marker()
        finally:
            if self.guard:
                self.guard.__exit__(*exc_info)

    def _release_marker(self) -> None:
        if not self.acquired:
            return
        # Claim the name before deciding anything about it. Ownership read
        # off the shared name can only speak for the instant it happened: a
        # successor writing between that read and any action taken on it
        # would have its marker moved or deleted by stale evidence, leaving
        # a live driver unadvertised -- the false `loop_dead` page this class
        # exists to prevent. The rename is atomic, so the marker's content is
        # judged where nothing else can touch it, and the shared name is free
        # for a successor the instant it is given up.
        claimed = self.path.with_name(f"{self.path.name}.{self.pid}.releasing")
        try:
            os.replace(self.path, claimed)
        except OSError:
            return  # already gone, or never ours to remove; liveness decides
        try:
            owner = claimed.read_text(encoding="utf-8").strip()
        except OSError:
            owner = ""
        if owner != str(self.pid):
            # The marker just claimed is a successor's: they beat this process
            # to the name while it was winding down. Put it back -- but
            # exclusively, never over a newer driver's own marker. For the
            # instant between the claim and this restoration the marker lives
            # at `claimed`, where `read_pid_owner` still reads it, so the
            # successor stays observable throughout the sequence.
            try:
                with open(self.path, "x", encoding="utf-8") as handle:
                    handle.write(owner)
            except OSError:
                pass  # a newer marker holds the name; it wins
        try:
            claimed.unlink(missing_ok=True)
        except OSError:
            pass  # the leftover going stale is harmless; liveness decides


# ---------------------------------------------------------------- the poll lock


def _lock_is_contention(error: OSError) -> bool:
    """Did the OS say "someone else holds it", rather than "locking broke"?

    Contention is an expected event the caller skips cleanly; every other
    failure -- no space, a bad handle, an unusable filesystem -- is a real
    persistence fault that must reach the caller, not be laundered into a
    harmless skip that exits zero while no poll ran at all.

    Windows: `msvcrt.locking` reports a held region as `PermissionError`
    (errno 13, no winerror; verified on the deployment machine). The handle
    is already open for read/write when this is asked, so a permission
    failure at LOCK time means the byte is held, not the file is untouchable.
    POSIX: `flock` with LOCK_NB reports contention as `BlockingIOError`
    (EWOULDBLOCK/EAGAIN); a `PermissionError` there is a genuine fault.
    """
    if os.name == "nt":  # pragma: no branch - one branch per platform
        return isinstance(error, PermissionError)
    return isinstance(error, BlockingIOError)


class SupervisorPollLockHeld(RuntimeError):
    """Another supervisor poll is inside its transaction right now.

    Harmless by design: the scheduled task fires on its own cadence and a
    person may run the on-demand command, so two polls overlapping is an
    expected event, not a fault. The caller reports a skipped poll and exits
    zero -- this must stay its own type so a broad `except RuntimeError`
    cannot swallow a real persistence failure alongside it.
    """


class SupervisorPollLock:
    """An OS-backed exclusive lock around one poll's whole transaction.

    Why not the `TickLock` recipe (an exists-then-write marker file): two
    supervisor processes can start against an empty directory at the same
    instant, both see no file, and both proceed -- and the launch memory in
    `supervisor-state.json` is then written by two writers at once. The lock
    below is taken on an OPEN HANDLE by the operating system, so whichever
    process opens first wins and the other fails immediately, whatever the
    directory looked like a moment before.

    Held for one poll's transaction only: load state, evaluate signals,
    reserve and spawn a diagnosis run, save state and status. It is released
    on normal return, on any exception, and on process death (the OS reclaims
    it with the handle); it does NOT stay held for the detached diagnosis
    agent's lifetime. The lock FILE persists on purpose: deleting it on
    release would let a late contender lock a file nobody else can see while
    a successor creates a fresh one beside it -- the classic unlink race.
    """

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False
        self._handle = None

    def __enter__(self) -> "SupervisorPollLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `a+b` creates the file on first use and never truncates it after;
        # an OSError here (a full disk, an unwritable directory) propagates
        # as the persistence failure it is.
        handle = open(self.path, "a+b")
        handle.seek(0)
        try:
            if os.name == "nt":  # pragma: no branch - one branch per platform
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if not _lock_is_contention(error):
                # A real locking failure is a persistence failure: re-raise
                # it, so the caller reports a broken poll instead of a skip.
                raise
            raise SupervisorPollLockHeld(
                f"another supervisor poll is in progress ({self.path})"
            ) from error
        self._handle = handle
        self.acquired = True
        return self

    def __exit__(self, *exc_info) -> None:
        if not self.acquired:
            return
        handle, self._handle, self.acquired = self._handle, None, False
        try:
            handle.seek(0)
            if os.name == "nt":  # pragma: no branch - one branch per platform
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # close releases it regardless; process death releases it too
        finally:
            handle.close()


# ---------------------------------------------------------------- state

def _parse_watch(entry: Any) -> dict[str, str] | None:
    """One persisted escalation watch, or None when it cannot be trusted.

    A watch is only useful as a clock: `first_observed` must parse as a
    moment and the fingerprint must be text. Anything else is discarded so
    the item gets a fresh window -- a corrupt clock is later-or-fabricated
    exactly when it is trusted, and both are wrong.
    """
    if not isinstance(entry, dict):
        return None
    fingerprint = entry.get("fingerprint")
    first_observed = entry.get("first_observed")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    if not isinstance(first_observed, str) or _moment(first_observed) is None:
        return None
    return {"fingerprint": fingerprint, "first_observed": first_observed}


def _parse_diagnosis(entry: Any) -> dict[str, Any] | None:
    """One persisted diagnosis launch record, or None when untrustworthy.

    A record is only useful as launch memory if it names a signal kind, an
    incident signature, a run id and a parseable launch moment -- those four
    decide "already ran" and "inside grace". Anything else is discarded so
    the incident becomes launchable again: the fail-safe direction for a
    memory nobody can trust is one extra agent run, never a silenced signal.
    """
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    signature = entry.get("signature")
    adw_id = entry.get("adw_id")
    launched_at = entry.get("launched_at")
    if not (
        isinstance(kind, str)
        and kind
        and isinstance(signature, str)
        and signature
        and isinstance(adw_id, str)
        and adw_id
    ):
        return None
    if not isinstance(launched_at, str) or _moment(launched_at) is None:
        return None
    record: dict[str, Any] = {
        "kind": kind,
        "signature": signature,
        "adw_id": adw_id,
        "launched_at": launched_at,
        "launched": entry.get("launched") is True,
    }
    item = entry.get("item")
    record["item"] = (
        item if isinstance(item, int) and not isinstance(item, bool) else None
    )
    pid = entry.get("pid")
    record["pid"] = (
        pid
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        else None
    )
    log = entry.get("log")
    record["log"] = log if isinstance(log, str) and log else None
    return record


def _parse_action_memory(entry: Any) -> dict[str, Any] | None:
    """One persisted item-action processing memory, or None when untrustworthy.

    The memory is what stops a record being processed twice: its `state` is
    the processing state the poll last persisted for that record id. A
    malformed entry is discarded, which costs at most one re-observation of
    a dry-run record -- the fail-safe direction, never an armed replay,
    because only an entry that explicitly says the record was deferred may
    be re-run, and a discarded entry says nothing.
    """
    if not isinstance(entry, dict):
        return None
    state = entry.get("state")
    if state not in (
        item_actions.STATE_OBSERVED,
        item_actions.STATE_RESERVED,
        item_actions.STATE_ACTED,
        item_actions.STATE_FAILED,
        item_actions.STATE_DEFERRED,
    ):
        return None
    detail = entry.get("detail")
    if not isinstance(detail, str):
        detail = ""
    at = entry.get("at")
    if not isinstance(at, str) or _moment(at) is None:
        return None
    record: dict[str, Any] = {"state": state, "detail": detail, "at": at}
    action = entry.get("action")
    record["action"] = action if isinstance(action, str) and action else None
    item = entry.get("item")
    record["item"] = (
        item if isinstance(item, int) and not isinstance(item, bool) else None
    )
    return record


@dataclass
class SupervisorState:
    """The supervisor's own memory: repeat counters and fired signatures.

    Separate from `state.json` on purpose. The tick owns that file; a second
    writer against it is the race that already cost one gate approval on the
    predecessor project, and this poll runs on its own schedule beside the
    tick rather than inside it.

    `escalations` is the supervisor's own clock on other people's pages: for
    each item whose persisted escalation it is watching, the exact subject
    (the decider's `escalated_fingerprint`) and when that subject was FIRST
    observed. The tick never records when it escalated -- by design, this
    window is the supervisor's fact, not the factory's.

    `diagnoses` is the launch memory for the one-shot diagnosis agents: one
    record per signal kind and item, naming the incident signature last
    launched, the run id, the launch moment, the PID and the log. It exists
    so one incident wakes one agent -- an unchanged signature is already
    diagnosed, a live or fresh run suppresses the next, and only a materially
    changed signature launches again once the guards clear.
    """

    schema: int = SCHEMA_VERSION
    repeats: dict[str, int] = field(default_factory=dict)
    fired: dict[str, str] = field(default_factory=dict)
    # The signature each signal last observed, so consecutive means
    # consecutive observations of the SAME situation. Without it, one
    # incident would inherit the repeat count of whatever incident preceded
    # it, and a signal due in three polls could fire in one.
    observed: dict[str, str] = field(default_factory=dict)
    # item number (as text) -> {"fingerprint": str, "first_observed": str}.
    # The watch is keyed by item and carries the fingerprint it timed, so a
    # new subject on the same item is a new window, not a running total.
    escalations: dict[str, dict[str, str]] = field(default_factory=dict)
    # "<kind>:<item>" -> the launch record for that incident, as reserved by
    # `_dispatch_diagnosis` and completed after a successful spawn.
    diagnoses: dict[str, dict[str, Any]] = field(default_factory=dict)
    # item-action record id -> the processing memory for that record: what
    # the poll last did with it (observed / reserved / acted / failed /
    # deferred), when, and the outcome detail. Additive and per-entry
    # defensive like the sections above: one malformed entry costs that
    # record's memory only, and a whole malformed section costs every
    # entry while preserving the counters, watches and launch memory.
    action_processing: dict[str, dict[str, Any]] = field(default_factory=dict)

    def note_candidate(self, signal: str, signature: str) -> int:
        """Count consecutive observations of one signature, starting at 1.

        A candidate whose signature differs from the last one observed is a
        new situation, not a repeat: it starts at 1 rather than inheriting
        the previous situation's count. For `item_stalled` that is the reset
        rule itself -- the moment anything in the observation moves (item,
        head, position, budget, tracked set), the count starts over. For
        `loop_dead` it keeps "consecutive" meaning what it says.
        """
        if self.observed.get(signal) != signature:
            self.repeats[signal] = 1
        else:
            self.repeats[signal] = self.repeats.get(signal, 0) + 1
        self.observed[signal] = signature
        return self.repeats[signal]

    def has_fired(self, signal: str, signature: str) -> bool:
        return self.fired.get(signal) == signature

    def mark_fired(self, signal: str, signature: str) -> None:
        self.fired[signal] = signature

    def rearm(self, signal: str) -> None:
        """The condition cleared: forget the incident, allow the next one."""
        self.repeats.pop(signal, None)
        self.fired.pop(signal, None)
        self.observed.pop(signal, None)

    def interrupt(self, signal: str) -> None:
        """An unreadable poll: break the count, keep the page memory.

        A poll that could not read its input neither repeats the last incident
        nor confirms it gone. Popping the counter stops the unread poll from
        counting toward "consecutive" -- the threshold must be reached by
        readings, not by guesses. Keeping `fired` is what stops the same
        unresolved incident paging a second time the moment the reads recover:
        deduplication memory is worth more than a clean counter.
        """
        self.repeats.pop(signal, None)
        self.observed.pop(signal, None)

    @classmethod
    def load(cls, path: Path) -> "SupervisorState":
        """Read state, tolerating absence and corruption.

        Losing the counters costs one extra poll before the next page. Losing
        the fired signatures costs one duplicate page. Losing an escalation
        watch costs one complete grace window -- the next observation of the
        same subject starts its clock again, which is the fail-safe
        direction: later, never fabricated. None of it justifies refusing to
        poll at all.
        """
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return cls()
            if raw.get("schema") != SCHEMA_VERSION:
                return cls()
            # Both sections are checked for being mappings, not merely truthy.
            # A list or a string is truthy and has no `.items()`, and the
            # AttributeError that raises is not caught below -- so a corrupt
            # section would take the whole poll down rather than costing the
            # one extra poll this method is willing to pay.
            repeats = raw.get("repeats") or {}
            fired = raw.get("fired") or {}
            observed = raw.get("observed") or {}
            if not (
                isinstance(repeats, dict)
                and isinstance(fired, dict)
                and isinstance(observed, dict)
            ):
                return cls()
            state = cls()
            state.repeats = {str(k): int(v) for k, v in repeats.items()}
            state.fired = {str(k): str(v) for k, v in fired.items()}
            # Absent in state files written before `observed` existed; an
            # empty mapping is the tolerant default, and costs exactly one
            # poll: the next candidate re-establishes its signature at 1.
            state.observed = {str(k): str(v) for k, v in observed.items()}
            # The watch section is additive and per-entry defensive: one
            # malformed entry discards that item's window only -- it cannot
            # inherit a stale clock, so it restarts -- and a whole malformed
            # section discards every window while preserving the counters
            # above. Never a failed poll over a clock nobody can trust.
            watches = raw.get("escalations") or {}
            if isinstance(watches, dict):
                for key, entry in watches.items():
                    watch = _parse_watch(entry)
                    if watch is not None:
                        state.escalations[str(key)] = watch
            # Diagnosis launch memory is additive and per-entry defensive,
            # exactly like the watches: one malformed record discards that
            # incident's memory only (it becomes launchable again -- the
            # fail-safe direction), and a whole malformed section discards
            # every record while preserving every counter above it.
            diagnoses = raw.get("diagnoses") or {}
            if isinstance(diagnoses, dict):
                for key, entry in diagnoses.items():
                    record = _parse_diagnosis(entry)
                    if record is not None:
                        state.diagnoses[str(key)] = record
            processing = raw.get("action_processing") or {}
            if isinstance(processing, dict):
                for key, entry in processing.items():
                    memory = _parse_action_memory(entry)
                    if memory is not None:
                        state.action_processing[str(key)] = memory
            return state
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return cls()

    def save(self, path: Path) -> None:
        """Write atomically. Never touches the factory's `state.json`."""
        _atomic_write(path, json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- the poll

def _position_of(
    config: FactoryConfig, paths: FactoryPaths, item: int, errors: list[str]
) -> tuple[str | None, bool]:
    """The item's position, the stage it is parked at taking precedence.

    Artifact and label reads fail visibly as status errors and a decision
    point; they never raise out of the poll.
    """
    try:
        parked = labels.blocked_stage(config, item, paths.root)
    except labels.LabelError as error:
        errors.append(f"#{item}: cannot read labels: {error}")
        parked = None
    if parked:
        # An item parked for a person is a reported decision point. It is NOT
        # re-paged here: the tick already escalated it, and the supervisor
        # repeating that page would bury the one signal only it can send.
        return parked, True
    try:
        stage, _pr = current_position(config, paths, item)
    except GhError as error:
        errors.append(f"#{item}: cannot read position: {error}")
        return None, False
    return (stage.name if stage else "decider"), False


def _loop_dead_reading(
    config: FactoryConfig,
    *,
    open_items: list[int] | None,
    age: float | None,
    driver: PidOwner,
    lock: PidOwner,
) -> tuple[bool, str]:
    """The five-part loop-dead predicate, naming the check that answered No.

    Order is cheap-to-expensive and stable. A read failure on the open-item
    set answers No rather than Yes: a page manufactured from a GitHub outage
    would wake a person about GitHub, not about the factory.
    """
    if not config.supervisor.capabilities.loop_dead:
        return False, "signal disabled by configuration"
    if open_items is None:
        return False, "cannot establish the open item set (read failure)"
    if not open_items:
        return False, "no open items"
    window = config.supervisor.silence_window_seconds
    if age is None:
        return False, "last tick unreadable or never recorded"
    if age <= window:
        return False, f"last tick {age:.0f}s ago, inside the {window:.0f}s silence window"
    if driver.alive:
        return False, f"a driver is alive ({driver.detail})"
    if lock.alive:
        return False, f"a tick is in progress ({lock.detail})"
    return True, (
        f"last tick {age:.0f}s ago (silence window {window:.0f}s), "
        f"{len(open_items)} open item(s), no live driver, no live tick"
    )


# ---------------------------------------------------------------- the item

@dataclass(frozen=True)
class ItemObservation:
    """One artifact-derived snapshot of the active item, read in one poll.

    Everything here is recomputed from the world each poll and nothing here
    is ever written back: the tick owns the state file, and the stall signal
    must stay an observation, not a second orchestrator. `stages` carries,
    per configured stage, the same facts the tick's own artifact probes use
    -- present or absent, and pinned to the CURRENT head or not -- never the
    `stage:`/`blocked:` label projection, which a reopened item can carry
    from work finished weeks ago. The reviewer wait is carried by category
    (`review_wait`) plus the request's own stamp and the delivered review's
    id, so both the wait ending and a new request are observable progress.
    """

    item: int | None
    pr_number: int | None
    head: str | None
    stages: dict[str, dict[str, Any]]
    position: str | None
    budget: dict[str, Any]
    tracked: list[dict[str, Any]]
    review_wait: str
    review_requested_at: str | None
    delivered_review_id: int | None
    read_error: bool

    @property
    def backed(self) -> bool:
        """Is the stage state backed by an artifact on the item's head?

        The clarification's rule in one place: stage state counts only when
        an open pull request pins it to a current head. Without one there is
        nothing to pin against, so whatever labels the item carries is stale
        by definition and cannot raise a stall -- which is exactly the
        reopened-item false positive the predecessor project's supervisor
        kept waking on.
        """
        return bool(self.head)


def _review_wait_state(
    config: FactoryConfig,
    paths: FactoryPaths,
    pr: PullRequest | None,
    *,
    now: datetime,
    errors: list[str],
) -> tuple[str, Review | None, Review | None, str | None, bool]:
    """The categorical review-request state for this head, plus its facts.

    Returns `(state, actionable, delivered, requested_at, read_error)`. The
    state is one of `none`, `inside_grace`, `expired`, `delivered`, or
    `unreadable`. `actionable` is the review p3 would consume -- its own
    pick, unchanged. `delivered` is ANY external review pinned to this head:
    a review that found nothing is still the reviewer answering, and reading
    it as an unending wait is exactly the false stall this signal exists not
    to send. `requested_at` is the canonical stamp of the full-review
    request when one was read, so the request's own identity -- not just its
    category -- is part of what the poll observes.

    Only the CATEGORY of the wait can repeat: the request's age changes
    every second and would make every poll a new situation, so crossing the
    grace boundary, a second request, or a current-head review arriving is
    what the stall signal sees as progress. Judged against the poll's `now`
    and the tick's own grace bound, so the supervisor and the tick can never
    disagree about when a wait ended.
    """
    if pr is None:
        return "none", None, None, None, False
    try:
        actionable = artifacts.actionable_external_review(config.repo, pr, paths.root)
        delivered = artifacts.external_review(config.repo, pr, paths.root)
    except GhError as error:
        errors.append(f"cannot read the external review on PR #{pr.number}: {error}")
        return "unreadable", None, None, None, True
    if delivered is not None:
        # The wait ended: no request is outstanding in any sense this signal
        # cares about, so the request stamp is not probed. The delivery's own
        # identity is carried instead.
        return "delivered", actionable, delivered, None, False
    try:
        requested_at = artifacts.full_review_requested_at(
            config.repo, pr.number, pr.revision, paths.root
        )
    except GhError as error:
        errors.append(f"cannot read the review request on PR #{pr.number}: {error}")
        return "unreadable", actionable, None, None, True
    moment = _moment(requested_at) if requested_at else None
    if moment is None:
        # No readable request for THIS head: nothing is coming that the grace
        # window would cover. An unreadable stamp is not a wait either -- the
        # tick's own `review_is_pending` treats it the same way.
        return "none", actionable, None, None, False
    age = (now - moment).total_seconds()
    grace = config.limits.external_review_grace_seconds
    wait = "inside_grace" if age < grace else "expired"
    return wait, actionable, None, _canonical_tick(requested_at), False


def _observe_item(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    item: int | None,
    *,
    now: datetime,
    errors: list[str],
) -> ItemObservation:
    """Gather the active item's situation in one read-only pass.

    Stage facts come from the artifact probes with their current-head rules,
    so a review pinned to an earlier head reads as absent and cannot back
    the current stage. The budget is read, never defaulted into the state
    file: an item with no entry is a zero budget, and `FactoryState.budget`
    would mutate the loaded state -- the one thing this poll must not do.
    """
    if item is None:
        return ItemObservation(
            None, None, None, {}, None, {}, [], "none", None, None, False
        )

    read_error = False
    pr: PullRequest | None = None
    try:
        pr = artifacts.open_pull_request(config.repo, item, paths.root)
    except GhError as error:
        errors.append(f"#{item}: cannot read the open pull request: {error}")
        read_error = True

    (
        review_wait,
        actionable,
        delivered,
        requested_at,
        wait_error,
    ) = _review_wait_state(config, paths, pr, now=now, errors=errors)
    read_error = read_error or wait_error

    stages: dict[str, dict[str, Any]] = {}
    try:
        for stage in config.stages:
            entry: dict[str, Any] = {"artifact": stage.artifact}
            if stage.artifact == "open_pull_request":
                entry["done"] = pr is not None
                if pr is not None:
                    entry["pr"] = pr.number
                    entry["head"] = pr.revision
            elif pr is None:
                # No open PR means no head to pin anything against: every
                # later stage is absent, and only the p1 artifact could
                # change that. Recorded as absent rather than skipped so the
                # observation still names where the item is not.
                entry["done"] = False
                entry["absent"] = "no open pull request"
            elif stage.artifact == "pinned_review":
                review = artifacts.pinned_review(config.repo, pr, paths.root)
                entry["done"] = review is not None
                if review is not None:
                    entry["review_id"] = review.review_id
            elif stage.artifact == "ledger_entry":
                if actionable is None:
                    # The tick's own rule, minus its hidden wall clock: the
                    # stage is done unless a full review of THIS head was
                    # requested and is still inside its grace window.
                    entry["done"] = review_wait != "inside_grace"
                    entry["review_wait"] = review_wait
                else:
                    ledger = artifacts.ledger_entry(
                        paths.root, config.repo, pr.number, actionable.review_id
                    )
                    entry["done"] = ledger is not None
                    entry["review_id"] = actionable.review_id
            else:
                raise ValueError(f"unknown artifact probe {stage.artifact!r}")
            stages[stage.name] = entry
    except (GhError, ValueError) as error:
        errors.append(f"#{item}: cannot read stage artifacts: {error}")
        read_error = True

    position = (
        next(
            (name for name, entry in stages.items() if not entry.get("done")),
            "decider",
        )
        if stages
        else None
    )
    budget = state.budgets.get(str(item))
    budget_view = asdict(budget) if budget is not None else asdict(ItemBudget())
    tracked_view = sorted(
        (asdict(run) for run in state.tracked),
        key=lambda run: str(run.get("adw_id")),
    )
    return ItemObservation(
        item=item,
        pr_number=pr.number if pr is not None else None,
        head=pr.revision if pr is not None else None,
        stages=stages,
        position=position,
        budget=budget_view,
        tracked=tracked_view,
        review_wait=review_wait,
        review_requested_at=requested_at,
        delivered_review_id=(
            delivered.review_id if delivered is not None else None
        ),
        read_error=read_error,
    )


def _observation_signature(observation: ItemObservation) -> str:
    """The stable signature of one item observation.

    Canonical JSON -- sorted keys, deterministic collection order -- so the
    same situation signs the same whatever order the world returned it in.
    Deliberately excludes anything that moves on its own: poll time, elapsed
    ages, remaining grace seconds. The grace state enters as its CATEGORY,
    and the request and delivery enter by their OWN identities -- the request
    stamp and the delivered review's id -- so a second request, or a second
    review, is observable progress even when the category does not move.
    """
    canonical = json.dumps(
        {
            "item": observation.item,
            "pr": observation.pr_number,
            "head": observation.head,
            "review_wait": observation.review_wait,
            "review_requested_at": observation.review_requested_at,
            "delivered_review_id": observation.delivered_review_id,
            "stages": observation.stages,
            "position": observation.position,
            "budget": observation.budget,
            "tracked": observation.tracked,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _fingerprint(ITEM_STALLED, canonical)


@dataclass(frozen=True)
class InfrastructureClassification:
    """What the infrastructure signals concluded about the active item.

    Only the CONFIRMED faults and the UNREADABLE inputs land here; a healthy
    reading is not a fact the stall classification needs. Carried separately
    from the signal records so `_item_stalled_reading` stays a pure function
    of what the poll already worked out.
    """

    disk_below_floor: bool = False
    disk_unreadable: bool = False
    credit_exhausted: bool = False
    credit_unreadable: bool = False
    run_vanished: bool = False


def _item_stalled_reading(
    config: FactoryConfig,
    observation: ItemObservation,
    *,
    live_run,
    escalated_items: frozenset[int] = frozenset(),
    infra: InfrastructureClassification = InfrastructureClassification(),
) -> tuple[bool, str]:
    """The stalled-item predicate, naming the check that answered No.

    Fail closed everywhere: a read failure, an item the poll could not
    establish, stage state with no head-pinned backing, a review still
    inside its grace window, a persisted escalation, or any live tracked run
    all answer No. A page manufactured from a GitHub outage or a live run
    nobody noticed is worse than a late page, because it teaches the
    operator to ignore the signal.

    `escalated_items` carries items whose persisted escalation is unresolved
    OR could not be checked this poll. Either way the item is never eligible,
    inside the grace window and after it: the cause is known (or recorded)
    and a person has already been asked, which `escalation_unanswered` owns
    -- one item must not page as both a generic stall and an unanswered
    escalation, and a failed label read must not turn one into the other.

    `infra` carries the confirmed infrastructure faults and unreadable
    infrastructure inputs for this item. A machine that cannot run is not
    "nothing moving": the specific signal owns the page and names the cause,
    so the generic stall yields rather than double-classifying one fault.
    `loop_dead` is deliberately NOT folded in here -- a dead driver and a
    full disk are two real faults that can coexist.
    """
    if not config.supervisor.capabilities.item_stalled:
        return False, "signal disabled by configuration"
    if observation.read_error:
        return False, "cannot observe the active item's artifacts (read failure)"
    if observation.item is None:
        return False, "no active item to observe"
    if not observation.backed:
        return (
            False,
            "stage state has no current-head artifact backing (stale labels "
            "cannot raise a stall)",
        )
    if observation.item in escalated_items:
        return False, (
            f"#{observation.item} has a persisted escalation owned by "
            f"{ESCALATION_UNANSWERED} -- unresolved, or unreadable this poll"
        )
    if infra.disk_below_floor:
        return False, (
            f"the disk is below the launch floor, owned by {DISK_BELOW_FLOOR}; "
            f"the item cannot move for a machine reason"
        )
    if infra.credit_exhausted:
        return False, (
            f"the next stage's provider credit is exhausted, owned by "
            f"{PROVIDER_CREDIT_EXHAUSTED}; the launch is refused, not stalled"
        )
    if infra.run_vanished:
        return False, (
            f"a tracked run on #{observation.item} vanished without its "
            f"artifact, owned by {RUN_VANISHED}"
        )
    if infra.disk_unreadable or infra.credit_unreadable:
        return False, (
            "the infrastructure inputs are unreadable and already reported "
            "as status errors"
        )
    if observation.review_wait == "inside_grace":
        grace = config.limits.external_review_grace_seconds
        return (
            False,
            f"waiting for an external review of {observation.head[:8]} inside "
            f"the {grace:.0f}s grace window",
        )
    if live_run is not None:
        return False, (
            f"a tracked run is alive ({live_run.adw_id} {live_run.stage} on "
            f"#{live_run.item})"
        )
    return True, (
        f"#{observation.item} at {observation.position} (head "
        f"{observation.head[:8]}): no live run and the artifact-derived "
        f"situation is unchanged"
    )


# ------------------------------------------------------- unresolved escalations

@dataclass(frozen=True)
class EscalationObservation:
    """One persisted escalation the factory still owes a person's answer for.

    Everything here is read from the world and from the supervisor's own
    watch; nothing is written back. The fingerprint is the decider's identity
    for the escalation it sent and is what decides whether the subject
    changed; the `subject` text is for reporting only. `age_seconds` is
    measured from the supervisor's own first observation, never from a
    timestamp the factory recorded -- the factory writes none, deliberately.
    """

    item: int
    fingerprint: str
    subject: str
    blocked_stage: str
    pr_number: int | None
    pr_head: str | None
    first_observed_at: str
    age_seconds: float
    grace_seconds: float


def _escalation_state_key(item: int) -> str:
    """The per-item state key, distinct from the public signal name."""
    return f"{ESCALATION_UNANSWERED}:#{item}"


def _drop_watch(sup_state: SupervisorState, item: int) -> None:
    """Stop watching one item's escalation and re-arm its signal memory.

    Called only when the escalation is KNOWN resolved -- the item left the
    open set, the person cleared the blocked label, the PR closed, or the
    budget stopped carrying an escalation. A read failure never drops a
    watch: the window must not reset because GitHub blinked, or the page
    the outage delayed becomes a page the outage cancels.
    """
    sup_state.escalations.pop(str(item), None)
    sup_state.rearm(_escalation_state_key(item))


def _watch_observation(
    sup_state: SupervisorState,
    item: int,
    budget: ItemBudget,
    pr: PullRequest,
    *,
    now: datetime,
    grace: float,
) -> EscalationObservation:
    """Time one confirmed-unresolved escalation, starting or keeping its watch.

    The window opens when the supervisor FIRST observes this exact subject on
    this item: the same item and fingerprint keep their original start, a
    different fingerprint is a new subject and starts a new window with fresh
    signal memory, and the factory's persisted facts are never written back.
    """
    key = str(item)
    watch = sup_state.escalations.get(key)
    if watch is None or watch.get("fingerprint") != budget.escalated_fingerprint:
        # New subject (or first sight of this one): a window that inherits the
        # old subject's elapsed time would page early, and repeats or fired
        # state that carried over would page late or never. The stamp is
        # full-precision -- see `_stamp` -- so the boundary the grace window
        # promises is the boundary the clock keeps.
        sup_state.rearm(_escalation_state_key(item))
        watch = {
            "fingerprint": budget.escalated_fingerprint,
            "first_observed": _stamp(now),
        }
        sup_state.escalations[key] = watch
    moment = _moment(watch.get("first_observed"))
    if moment is None:
        # Defensive only: `_parse_watch` discards unparseable stamps on load.
        watch["first_observed"] = _stamp(now)
        moment = now
    age = max((now - moment).total_seconds(), 0.0)
    return EscalationObservation(
        item=item,
        fingerprint=budget.escalated_fingerprint,
        subject=budget.blocked_reason or "",
        blocked_stage=budget.blocked_stage or "",
        pr_number=pr.number,
        pr_head=pr.revision,
        first_observed_at=watch["first_observed"],
        age_seconds=age,
        grace_seconds=grace,
    )


def _escalation_observations(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    *,
    open_items: list[int] | None,
    sup_state: SupervisorState,
    now: datetime,
    errors: list[str],
) -> tuple[list[EscalationObservation], frozenset[int]]:
    """Every persisted escalation that is still unresolved, read-only.

    Returns the confirmed observations and, separately, the items whose
    escalation could not be CONFIRMED either way because a read failed.
    Those uncertain items are not observed -- fail closed, never a page --
    but they are still escalation-carrying items, which the stall
    classification needs: a generic `item_stalled` page for an item the
    budget says was escalated is the double-classification this signal
    exists to prevent, whether the label read succeeded or not.

    Inspects every item with a recorded escalation, not just the displayed
    active one -- a parked item is often not the queue head. An escalation is
    unresolved only when ALL of these hold:

    1. its budget still carries a fingerprint, a reason and a blocked stage;
    2. the item is still in the successfully read open/eligible item set;
    3. `labels.blocked_stage()` still reports it parked -- a person clearing
       the label resolves it immediately, before the next tick clears the
       budget;
    4. `artifacts.open_pull_request()` still finds its open PR, so a merged
       or closed PR can never produce the signal, including the
       merge-landed/issue-close-lag case.

    Every read failure is a visible status error and fails CLOSED for the
    affected observation: a GitHub outage is not an unanswered person.
    """
    candidates: list[tuple[int, ItemBudget]] = []
    for key, budget in state.budgets.items():
        if not (
            budget.escalated_fingerprint
            and budget.blocked_reason
            and budget.blocked_stage
        ):
            continue
        try:
            item = int(key)
        except (TypeError, ValueError):
            continue
        candidates.append((item, budget))
    candidates.sort(key=lambda pair: pair[0])

    observed: list[EscalationObservation] = []
    uncertain: set[int] = set()
    escalated_items = {item for item, _ in candidates}
    for item, budget in candidates:
        if open_items is None:
            # The open-item read failed elsewhere in the poll and is already a
            # visible error. Fail closed, keep the watch.
            uncertain.add(item)
            continue
        if item not in open_items:
            _drop_watch(sup_state, item)
            continue
        try:
            parked = labels.blocked_stage(config, item, paths.root)
        except labels.LabelError as error:
            errors.append(f"#{item}: cannot read the blocked label: {error}")
            uncertain.add(item)
            continue
        if parked is None:
            # The person came: the label is how blocked is lifted, and it is
            # lifted on GitHub before the next tick clears the budget.
            _drop_watch(sup_state, item)
            continue
        try:
            pr = artifacts.open_pull_request(config.repo, item, paths.root)
        except GhError as error:
            errors.append(f"#{item}: cannot read the open pull request: {error}")
            uncertain.add(item)
            continue
        if pr is None:
            # Merged or closed -- the item's open work is gone whatever the
            # issue or budget still says.
            _drop_watch(sup_state, item)
            continue
        observed.append(
            _watch_observation(
                sup_state,
                item,
                budget,
                pr,
                now=now,
                grace=config.supervisor.escalation_grace_seconds,
            )
        )

    # Watches for items the factory itself no longer reports as escalated:
    # the tick cleared the budget, so the escalation is resolved in its own
    # book even if the label lingers. Watches for items whose reads FAILED
    # are deliberately kept -- see `_drop_watch`.
    for key in list(sup_state.escalations):
        try:
            watched = int(key)
        except ValueError:
            sup_state.escalations.pop(key, None)
            continue
        if watched not in escalated_items:
            _drop_watch(sup_state, watched)
    return observed, frozenset(uncertain)


def _forget_escalation_watches(sup_state: SupervisorState) -> None:
    """Drop every escalation watch and its per-item signal memory.

    Used while the capability is switched off: nothing observes escalations
    then, so no window may age and no resolution can be synchronized through
    the dark period. A watch kept through it would hand the next enabled poll
    a stale clock -- an escalation resolved and re-raised while the signal
    was off could page as months unanswered on its first poll back. Every
    escalation seen after re-enabling gets one complete fresh window, which
    is the fail-safe direction: later, never early.
    """
    sup_state.escalations.clear()
    prefix = f"{ESCALATION_UNANSWERED}:"
    for section in (sup_state.repeats, sup_state.fired, sup_state.observed):
        for key in [key for key in section if key.startswith(prefix)]:
            section.pop(key, None)


def _escalation_signal(
    config: FactoryConfig,
    sup_state: SupervisorState,
    observations: list[EscalationObservation],
    *,
    errors: list[str],
) -> dict[str, Any]:
    """Observe every unresolved escalation; report and page the overdue ones.

    Strict past-grace semantics: an escalation aged exactly at the window is
    still suppressed -- only time strictly greater than the window is
    unanswered. Repeat counting, threshold, retry and exactly-once delivery
    all run per item under `escalation_unanswered:#<item>`, so several parked
    items age independently and resolving one never re-pages or delays
    another. The aggregate record exposes one `candidate` flag so the
    payload's health rule can treat any overdue incident as a decision point.
    """
    incidents: list[dict[str, Any]] = []
    for observation in observations:
        overdue = observation.age_seconds > observation.grace_seconds
        if overdue:
            reason = (
                f"#{observation.item} asked for a person and nobody has answered "
                f"for {observation.age_seconds:.0f}s "
                f"(grace {observation.grace_seconds:.0f}s): {observation.subject}"
            )
        else:
            reason = (
                f"#{observation.item} asked for a person "
                f"{observation.age_seconds:.0f}s ago, inside the "
                f"{observation.grace_seconds:.0f}s grace window: "
                f"{observation.subject}"
            )
        incident = _run_signal(
            config,
            sup_state,
            _escalation_state_key(observation.item),
            overdue,
            reason,
            _fingerprint(
                ESCALATION_UNANSWERED,
                str(observation.item),
                observation.fingerprint,
            ),
            item=observation.item,
            position=observation.blocked_stage,
            last_tick=None,
            errors=errors,
            page_name=ESCALATION_UNANSWERED,
        )
        incident.update(
            {
                "state": "unanswered" if overdue else "inside_grace",
                "subject": observation.subject,
                "stage": observation.blocked_stage,
                "fingerprint": observation.fingerprint,
                "pr": observation.pr_number,
                "head": observation.pr_head,
                "first_observed_at": observation.first_observed_at,
                "age_seconds": round(observation.age_seconds, 1),
                "grace_seconds": observation.grace_seconds,
            }
        )
        incidents.append(incident)

    overdue = [incident for incident in incidents if incident["state"] == "unanswered"]
    grace = config.supervisor.escalation_grace_seconds
    if overdue:
        named = ", ".join(f"#{incident['item']}" for incident in overdue)
        reason = (
            f"{len(overdue)} escalation(s) unanswered past the "
            f"{grace:.0f}s grace window: {named}"
        )
    elif incidents:
        named = ", ".join(f"#{incident['item']}" for incident in incidents)
        reason = (
            f"{len(incidents)} unresolved escalation(s) inside the "
            f"{grace:.0f}s grace window: {named}"
        )
    else:
        reason = "no unresolved escalations"
    return _aggregate_signal_record(config, incidents, reason)


def _aggregate_signal_record(
    config: FactoryConfig, incidents: list[dict[str, Any]], reason: str
) -> dict[str, Any]:
    """The aggregate record for a signal that counts incidents independently.

    One public signal key in the status, several independently counted,
    paged and deduplicated incidents under it -- the shape
    `escalation_unanswered` already uses per item, and the infrastructure
    signals use per provider/stage fault and per vanished run. The aggregate
    `candidate` flag is what the payload's health rule reads: any active
    incident is a decision point from its first poll.
    """
    pages = [incident["page"] for incident in incidents]
    return {
        "candidate": any(incident["candidate"] for incident in incidents),
        "incidents": incidents,
        "fired": any(incident["fired"] for incident in incidents),
        "page": pages[0] if pages and len(set(pages)) == 1 else (
            "not-needed" if not pages else "mixed"
        ),
        "reason": reason,
        "item": None,
        "position": None,
        "repeats": max(
            (incident["repeats"] for incident in incidents), default=0
        ),
        "repeat_threshold": config.supervisor.repeat_polls,
        "signature": None,
    }


def _run_signal(
    config: FactoryConfig,
    sup_state: SupervisorState,
    name: str,
    candidate: bool,
    reason: str,
    signature: str,
    *,
    item: int | None,
    position: str | None,
    last_tick: str | None,
    errors: list[str],
    page_name: str | None = None,
) -> dict[str, Any]:
    """Observe one signal: count repeats, page when due, deduplicate.

    `name` is the state key -- per incident for the per-item escalation
    signals -- while `page_name` is the public signal the page carries, so
    several items can age and deduplicate independently under one name a
    person recognizes.

    Repeats are counted per signature through `note_candidate`, so a signal
    whose situation changed starts at 1 instead of inheriting the count of
    whatever it observed before.

    A page is marked fired only when it was DELIVERED (or paging is disabled
    and the signal is acknowledged without one), so a failed delivery retries
    on the next poll instead of being remembered as news that never arrived.
    """
    public = page_name or name
    if not candidate:
        sup_state.rearm(name)
        return {
            "candidate": False,
            "item": item,
            "position": position,
            "reason": reason,
            "repeats": 0,
            "repeat_threshold": config.supervisor.repeat_polls,
            "fired": False,
            "signature": signature,
            "page": "not-needed",
        }

    repeats = sup_state.note_candidate(name, signature)
    threshold = config.supervisor.repeat_polls
    if repeats < threshold:
        return {
            "candidate": True,
            "item": item,
            "position": position,
            "reason": reason,
            "repeats": repeats,
            "repeat_threshold": threshold,
            "fired": False,
            "signature": signature,
            "page": "not-due",
        }

    if sup_state.has_fired(name, signature):
        # Same incident, already reported. The state repeats every poll; the
        # news does not.
        return {
            "candidate": True,
            "item": item,
            "position": position,
            "reason": reason,
            "repeats": repeats,
            "repeat_threshold": threshold,
            "fired": True,
            "signature": signature,
            "page": "already-sent",
        }

    if not config.supervisor.capabilities.page:
        sup_state.mark_fired(name, signature)
        return {
            "candidate": True,
            "item": item,
            "position": position,
            "reason": reason,
            "repeats": repeats,
            "repeat_threshold": threshold,
            "fired": True,
            "signature": signature,
            "page": "disabled",
        }

    result = escalate.escalate_factory_health(
        config.repo,
        public,
        reason,
        item=item,
        position=position,
        last_tick=last_tick,
    )
    if result.delivered:
        sup_state.mark_fired(name, signature)
        return {
            "candidate": True,
            "item": item,
            "position": position,
            "reason": reason,
            "repeats": repeats,
            "repeat_threshold": threshold,
            "fired": True,
            "signature": signature,
            "page": "sent",
        }

    errors.append(f"{public}: page not delivered: {result.detail}")
    return {
        "candidate": True,
        "item": item,
        "position": position,
        "reason": reason,
        "repeats": repeats,
        "repeat_threshold": threshold,
        "fired": False,
        "signature": signature,
        "page": "failed",
    }


# ------------------------------------------------- the infrastructure faults

# The per-incident state-key prefixes. The signals above key their memory by
# signal name (or signal+item); the infrastructure faults have dynamic
# incident populations -- one key per (item, stage, provider) credit fault and
# one per tracked run id -- so cleanup walks these prefixes.
_CREDIT_KEY_PREFIX = f"{PROVIDER_CREDIT_EXHAUSTED}:"
_RUN_KEY_PREFIX = f"{RUN_VANISHED}:"


def _credit_state_key(item: int, stage: str, provider: str) -> str:
    return f"{_CREDIT_KEY_PREFIX}#{item}:{stage}:{provider}"


def _run_state_key(adw_id: str) -> str:
    return f"{_RUN_KEY_PREFIX}{adw_id}"


def _rearm_prefixed(sup_state: SupervisorState, prefix: str, keep: set[str]) -> None:
    """Drop the incident memories under `prefix` that are not in `keep`.

    Called after a SUCCESSFUL observation of the whole incident population,
    so a key disappears exactly when the poll confirmed its incident is
    gone -- never because the source was unreadable. A read outage keeps
    every key: the page the outage delayed must not become a page the outage
    cancels, and a fault that recurs after a confirmed recovery is new news
    that may page again. A disabled capability is no observation either and
    does not call this -- it interrupts through `_interrupt_prefixed` -- so a
    key only ever disappears through a confirmed observation.
    """
    for section in (sup_state.repeats, sup_state.fired, sup_state.observed):
        for key in [key for key in section if key.startswith(prefix) and key not in keep]:
            section.pop(key, None)


def _interrupt_prefixed(sup_state: SupervisorState, prefix: str) -> None:
    """Interrupt every incident under `prefix`: the source went unreadable.

    The whole population could not be read this poll, so no key may count
    toward "consecutive" and none is confirmed gone. Counters and observed
    signatures are dropped per key (the threshold must be reached by
    readings, not by guesses); the fired signatures are deliberately KEPT,
    so the same unresolved incident cannot page a second time once the
    reads recover.
    """
    for section in (sup_state.repeats, sup_state.observed):
        for key in [key for key in section if key.startswith(prefix)]:
            section.pop(key, None)


def _unreadable_signal_record(config: FactoryConfig, reason: str) -> dict[str, Any]:
    """The quiet record for a signal whose input could not be read.

    Not a candidate -- an unreadable input is a read error, not a fault -- but
    never `rearm`ed either: the consecutive count was interrupted, not reset,
    and the fired signature stays so the same unresolved incident cannot page
    twice around an outage. The read problem itself is reported through
    `errors`, which already makes the poll a decision point.
    """
    return {
        "candidate": False,
        "item": None,
        "position": None,
        "reason": reason,
        "repeats": 0,
        "repeat_threshold": config.supervisor.repeat_polls,
        "fired": False,
        "signature": None,
        "page": "not-needed",
    }


def _fmt_pct(value: float | None) -> str:
    """One percentage for a reason line; a missing window says so."""
    return f"{value:g}%" if value is not None else "no data"


@dataclass(frozen=True)
class DiskSignal:
    """The disk signal's record, plus what the stall classification needs."""

    record: dict[str, Any]
    confirmed: bool
    unreadable: bool


def _disk_signal(
    config: FactoryConfig,
    paths: FactoryPaths,
    sup_state: SupervisorState,
    *,
    errors: list[str],
) -> DiskSignal:
    """Observe the free space on the factory's own filesystem, read-only.

    Always inspected, active item or none: a machine too full to launch under
    is a machine condition, not an item condition. The boundary and the
    numbers come from `gates.read_disk` -- the same reading the launch gate
    refuses on -- so the poll can never disagree with the tick about what
    "below the floor" means. The signature carries only the checked path and
    the configured floor: free space moves on its own, and a disk fluctuating
    below the floor is one unresolved incident, not a new one every poll.
    """
    reading = gates.read_disk(config, paths.root)
    floor = config.limits.disk_floor_gb
    facts: dict[str, Any] = {"path": str(paths.root), "floor_gb": floor}

    if reading.error is not None:
        errors.append(f"{DISK_BELOW_FLOOR}: {reading.error}")
        sup_state.interrupt(DISK_BELOW_FLOOR)
        record = _unreadable_signal_record(config, reading.error)
        record.update(facts, free_gb=None, shortfall_gb=None)
        return DiskSignal(record, confirmed=False, unreadable=True)

    facts["free_gb"] = reading.free_gb
    facts["shortfall_gb"] = reading.shortfall_gb
    if not config.supervisor.capabilities.disk_below_floor:
        candidate, reason = False, "signal disabled by configuration"
    elif reading.below_floor:
        candidate = True
        reason = (
            f"{reading.free_gb:.2f} GB free at {paths.root} is below the "
            f"{floor:.1f} GB floor by {reading.shortfall_gb:.2f} GB; the "
            f"factory refuses to launch under the floor"
        )
    else:
        candidate = False
        reason = (
            f"{reading.free_gb:.2f} GB free at {paths.root} is at or above "
            f"the {floor:.1f} GB floor"
        )
    record = _run_signal(
        config,
        sup_state,
        DISK_BELOW_FLOOR,
        candidate,
        reason,
        _fingerprint(DISK_BELOW_FLOOR, str(paths.root), f"{floor:g}"),
        item=None,
        position=None,
        last_tick=None,
        errors=errors,
    )
    record.update(facts)
    return DiskSignal(record, confirmed=candidate, unreadable=False)


def _next_launchable_stage(
    config: FactoryConfig, observation: ItemObservation
) -> str | None:
    """The artifact-derived stage the tick would next try to launch, if any.

    The observation already carries, per configured stage, whether its
    current-head artifact is present. The tick's own rule is applied to that:
    the first stage whose artifact is missing, omitting a stage that needs a
    pull request while there is none to launch it against. All stages done
    means the decider owns the item, and no credit gate is being waited on.
    Read-only -- `select` is never called and no budget is touched.
    """
    if observation.item is None:
        return None
    for stage in config.stages:
        entry = observation.stages.get(stage.name)
        if entry is not None and entry.get("done"):
            continue
        if stage.arg == "pr" and observation.pr_number is None:
            return None  # nothing for the tick to launch on this item now
        return stage.name
    return None


@dataclass(frozen=True)
class CreditSignal:
    """The credit signal's aggregate record, plus its classification facts."""

    record: dict[str, Any]
    confirmed: bool
    unreadable: bool


def _credit_signal(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    observation: ItemObservation,
    sup_state: SupervisorState,
    *,
    now: datetime,
    errors: list[str],
) -> CreditSignal:
    """Observe the credit the active item's next launch waits on, read-only.

    Only a real active item's artifact-derived next launchable stage is
    inspected -- the stage whose launch the tick would next refuse on credit.
    Items that are not waiting on the credit gate are skipped rather than
    mislabelled: the decider's items, stages with no issue/PR target to launch
    on, items whose slot a tracked run already occupies, and items a known
    escalation or terminal budget condition has already parked.

    One independently counted incident per exhausted (item, stage, provider),
    through the same `_run_signal` counting, threshold, retry and exactly-once
    delivery every other signal uses. The signature carries item, stage,
    provider and floor -- never the percentages or the snapshot age, which
    drain on their own and would make one draining provider a new incident
    every poll. Read errors (a stale, missing or malformed snapshot, a
    missing roster or workflow) are visible errors, never exhaustion pages:
    they interrupt the affected incidents' counting without rearming them or
    erasing their fired signatures, so an outage neither pages nor un-pages.
    """
    capabilities = config.supervisor.capabilities
    item = observation.item
    stage_name = _next_launchable_stage(config, observation)

    def quiet(reason: str, *, unreadable: bool = False) -> CreditSignal:
        return CreditSignal(
            _aggregate_signal_record(config, [], reason),
            confirmed=False,
            unreadable=unreadable,
        )

    if not capabilities.provider_credit_exhausted:
        # Nothing observes credit while the signal is off, so no count may
        # grow through the dark period -- but nothing is confirmed gone
        # either. The disabled poll gets the interruption an unreadable
        # source gets, never the rearm a successful observation earns, so a
        # fault that outlives the disabled period stays `already-sent`
        # instead of paging a second time once the signal is back.
        _interrupt_prefixed(sup_state, _CREDIT_KEY_PREFIX)
        return quiet("signal disabled by configuration")
    if item is None:
        return quiet("no active item to inspect")
    if observation.read_error:
        # The artifact reads already failed visibly; the next launchable
        # stage cannot be established, so nothing about credit can be claimed.
        # The artifact observation is the source that decides WHICH stage's
        # credit is waited on: an unreadable one neither counts toward
        # "consecutive" nor confirms any incident gone, so the counting is
        # interrupted (fired signatures kept) exactly as for an unreadable
        # snapshot.
        _interrupt_prefixed(sup_state, _CREDIT_KEY_PREFIX)
        return quiet(
            "cannot observe the active item's artifacts (read failure)",
            unreadable=True,
        )
    if observation.budget.get("blocked_stage"):
        return quiet(
            f"#{item} is blocked at {observation.budget['blocked_stage']} "
            f"(a person or a terminal budget condition, not a credit gate)"
        )
    if any(run.item == item for run in state.tracked):
        return quiet(f"#{item} already has a run in flight")
    if stage_name is None:
        return quiet("the active item has no launchable stage waiting on credit")

    stage = config.stage_by_name(stage_name)
    if stage is None:  # defensive: the name came from config.stages itself
        return quiet(f"configured stage {stage_name!r} could not be resolved")

    reading = gates.inspect_credits(
        config, stage, paths.root, now=now, credits_file=gates.CREDITS_FILE
    )
    if reading.derivation_error is not None:
        errors.append(
            f"{PROVIDER_CREDIT_EXHAUSTED}: cannot derive {stage_name} providers: "
            f"{reading.derivation_error}"
        )
        # Nothing about this stage's credit could be read: interrupt every
        # credit incident (no counting, no rearming), keeping fired signatures.
        _interrupt_prefixed(sup_state, _CREDIT_KEY_PREFIX)
        return quiet(reading.derivation_error, unreadable=True)
    if not reading.providers:
        # A successful observation, not a read failure: this stage needs no
        # metered provider, so any credit incident remembered for it is
        # confirmed gone. Returning without rearming left the fired signature
        # behind, and a seat swapped back to a metered model would then read
        # as already-paged and never page again.
        _rearm_prefixed(sup_state, f"{_CREDIT_KEY_PREFIX}#{item}:{stage_name}:", set())
        return quiet(
            f"{stage_name} uses no metered provider among "
            f"{len(reading.seats)} seat(s)"
        )
    snapshot = reading.snapshot
    if snapshot is None or snapshot.error is not None:
        problem = snapshot.error if snapshot is not None else "no snapshot read"
        errors.append(
            f"{PROVIDER_CREDIT_EXHAUSTED}: cannot read the credits for "
            f"{stage_name}: {problem}"
        )
        # Same interruption as the derivation fault: the snapshot is the
        # source, and an unreadable source neither counts nor confirms gone.
        _interrupt_prefixed(sup_state, _CREDIT_KEY_PREFIX)
        return quiet(problem, unreadable=True)

    # A trusted snapshot with unreadable providers: the read problem is a
    # visible error, and a provider the monitor could not read is never
    # relabelled as drained credit. Its incident memory is INTERRUPTED -- the
    # unreadable poll does not count -- and deliberately NOT rearmed, so the
    # same unresolved provider cannot page again once it reads exhausted.
    unreadable_providers = [
        entry for entry in snapshot.providers if entry.problem is not None
    ]
    for entry in unreadable_providers:
        errors.append(
            f"{PROVIDER_CREDIT_EXHAUSTED}: {stage_name} provider "
            f"{entry.provider} could not be read: {entry.detail}"
        )
        sup_state.interrupt(_credit_state_key(item, stage_name, entry.provider))

    # A trusted snapshot: every earlier incident this poll does not observe
    # again is CONFIRMED resolved (or no longer required), so its memory goes.
    # The unreadable providers stay in `keep` -- an unreadable provider is
    # neither exhausted nor recovered, and its fired signature outlives the
    # outage so the unresolved incident never pages twice.
    incidents: list[dict[str, Any]] = []
    keep: set[str] = {
        _credit_state_key(item, stage_name, entry.provider)
        for entry in unreadable_providers
    }
    for provider_reading in reading.exhausted:
        provider = provider_reading.provider
        key = _credit_state_key(item, stage_name, provider)
        keep.add(key)
        windows = ", ".join(provider_reading.exhausted_windows)
        reason = (
            f"#{item} at {stage_name} needs provider {provider}, whose "
            f"remaining credit is at or below the "
            f"{provider_reading.floor_pct:g}% floor: 5h "
            f"{_fmt_pct(provider_reading.pct_5h)}, weekly "
            f"{_fmt_pct(provider_reading.pct_weekly)} "
            f"(exhausted window(s): {windows}); the launch is refused"
        )
        incident = _run_signal(
            config,
            sup_state,
            key,
            True,
            reason,
            _fingerprint(
                PROVIDER_CREDIT_EXHAUSTED,
                str(item),
                stage_name,
                provider,
                f"{provider_reading.floor_pct:g}",
            ),
            item=item,
            position=stage_name,
            last_tick=None,
            errors=errors,
            page_name=PROVIDER_CREDIT_EXHAUSTED,
        )
        incident.update(
            {
                "incident": f"{stage_name}:{provider}",
                "stage": stage_name,
                "provider": provider,
                "floor_pct": provider_reading.floor_pct,
                "pct_remaining_5h": provider_reading.pct_5h,
                "pct_remaining_weekly": provider_reading.pct_weekly,
                "exhausted_windows": list(provider_reading.exhausted_windows),
            }
        )
        incidents.append(incident)
    # Scoped to the active item, item-wide: `keep` preserves the current
    # stage's live incidents, while the item's OTHER stages' keys are
    # confirmed no longer waited on -- the next launchable stage only
    # advances past one whose artifact landed. A key that survived the
    # advance would answer the item's later return to that stage (a new
    # head, the artifact gone again, the provider still drained) with a
    # stale `already-sent`, suppressing the page for a fault that came back.
    # The bare prefix also matches the keys of every other item, and dropping
    # those would rearm a fault nobody looked at -- item A forgets it already
    # paged while item B is active, then pages a second time for the same
    # unresolved fault.
    _rearm_prefixed(sup_state, f"{_CREDIT_KEY_PREFIX}#{item}:", keep)

    if incidents:
        named = ", ".join(
            f"#{incident['item']} at {incident['stage']} needs "
            f"{incident['provider']}"
            for incident in incidents
        )
        reason = (
            f"{len(incidents)} required provider(s) at or below the credit "
            f"floor, blocking the active item's next stage: {named}"
        )
    elif unreadable_providers:
        reason = "required provider(s) whose credit could not be read: " + "; ".join(
            entry.detail for entry in unreadable_providers
        )
    else:
        reason = (
            f"every provider {stage_name} needs is above the "
            f"{config.limits.credit_floor_pct:g}% credit floor"
        )
    return CreditSignal(
        _aggregate_signal_record(config, incidents, reason),
        confirmed=bool(incidents),
        unreadable=bool(unreadable_providers),
    )


@dataclass(frozen=True)
class VanishedSignal:
    """The vanished-run signal's aggregate record, plus the per-item facts."""

    record: dict[str, Any]
    vanished_items: frozenset[int]


def _run_vanished_signal(
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    sup_state: SupervisorState,
    observations: dict[int, ItemObservation],
    *,
    now: datetime,
    errors: list[str],
) -> VanishedSignal:
    """Observe every tracked run for a death that landed nothing, read-only.

    A run whose process is gone with no artifact holds a slot that will never
    free on its own -- the tick reaps it only when it can see the same facts,
    and until then the factory looks busy while doing nothing. A run is a
    candidate only when ALL of these hold: the process is not alive, the
    stage is known, the item's artifact observation succeeded, and that
    stage's current-head artifact is absent. A dead process WITH its artifact
    is normal completion awaiting reap -- never a fault. An unknown stage or
    an artifact read failure is a visible error, never a fabricated page.

    The run is never untracked and `read_run()` is never called to make the
    artifact look complete: the tick owns reaping, and progress is judged on
    artifacts alone. One independently counted incident per run id; the
    signature carries run id, item and stage -- never poll time, process age
    or artifact-read timing. Every tracked entry is inspected, not only the
    displayed active item: a vanished run parks any item, not just the head
    of the queue.
    """
    incidents: list[dict[str, Any]] = []
    keep: set[str] = set()
    unjudgeable: set[str] = set()
    vanished_items: set[int] = set()

    if not config.supervisor.capabilities.run_vanished:
        # Nothing judges the runs while the signal is off, so no count may
        # grow through the dark period -- but nothing is confirmed gone
        # either. The disabled poll gets the interruption an unreadable
        # source gets, never the rearm a successful observation earns, so a
        # run that outlives the disabled period stays `already-sent`
        # instead of paging a second time once the signal is back.
        _interrupt_prefixed(sup_state, _RUN_KEY_PREFIX)
        return VanishedSignal(
            _aggregate_signal_record(config, [], "signal disabled by configuration"),
            frozenset(),
        )

    def unjudgeable_run(adw_id: str) -> None:
        """A run this poll can neither confirm vanished nor confirm fine.

        Its key is INTERRUPTED -- an unreadable artifact observation or an
        unknown stage must not count toward "consecutive" -- and deliberately
        kept out of the rearm cleanup, so the fired signature survives the
        gap and the same unresolved run cannot page twice when reads recover.
        """
        key = _run_state_key(adw_id)
        sup_state.interrupt(key)
        unjudgeable.add(key)

    for run in sorted(state.tracked, key=lambda entry: entry.adw_id):
        stage = config.stage_by_name(run.stage)
        if stage is None:
            errors.append(
                f"{RUN_VANISHED}: tracked run {run.adw_id} names unknown stage "
                f"{run.stage!r}; cannot judge its artifact"
            )
            unjudgeable_run(run.adw_id)
            continue
        observation = observations.get(run.item)
        if observation is None:
            observation = _observe_item(
                config, paths, state, run.item, now=now, errors=errors
            )
            observations[run.item] = observation
        if observation.read_error:
            # The artifact observation for this run's item failed and is
            # already a visible error. The run is neither confirmed vanished
            # nor confirmed fine, so its memory is interrupted, not reset.
            unjudgeable_run(run.adw_id)
            continue
        entry = observation.stages.get(run.stage)
        if entry is None:
            errors.append(
                f"{RUN_VANISHED}: no artifact observation for {run.adw_id} "
                f"at {run.stage}; cannot judge it"
            )
            unjudgeable_run(run.adw_id)
            continue
        if entry.get("done"):
            # The artifact landed: normal completion awaiting the tick's reap.
            continue
        if launcher.is_alive(run.pid):
            continue
        key = _run_state_key(run.adw_id)
        keep.add(key)
        vanished_items.add(run.item)
        reason = (
            f"tracked run {run.adw_id} ({run.run_class}, pid {run.pid}, "
            f"launched {run.launched_at}) on #{run.item} at {run.stage} has "
            f"no live process and no {stage.artifact} artifact on the current "
            f"head; it is holding a slot that will not free on its own"
        )
        incident = _run_signal(
            config,
            sup_state,
            key,
            True,
            reason,
            _fingerprint(RUN_VANISHED, run.adw_id, str(run.item), run.stage),
            item=run.item,
            position=run.stage,
            last_tick=None,
            errors=errors,
            page_name=RUN_VANISHED,
        )
        incident.update(
            {
                "incident": run.adw_id,
                "adw_id": run.adw_id,
                "stage": run.stage,
                "run_class": run.run_class,
                "pid": run.pid,
                "launched_at": run.launched_at,
                "artifact": stage.artifact,
                "artifact_state": "absent",
            }
        )
        incidents.append(incident)

    # Only now, with every tracked run judged or excused: a key disappears
    # exactly when its run was confirmed not vanished, or left the tracked
    # set entirely (reaped by a tick). Runs this poll could not judge keep
    # their memory -- see `_rearm_prefixed`.
    _rearm_prefixed(sup_state, _RUN_KEY_PREFIX, keep | unjudgeable)

    if incidents:
        named = ", ".join(
            f"{incident['adw_id']} on #{incident['item']} at "
            f"{incident['stage']}"
            for incident in incidents
        )
        reason = f"{len(incidents)} tracked run(s) vanished without an artifact: {named}"
    elif state.tracked:
        reason = "every tracked run is alive or has landed its artifact"
    else:
        reason = "no tracked runs"
    return VanishedSignal(
        _aggregate_signal_record(config, incidents, reason),
        frozenset(vanished_items),
    )


def _disabled_escalation_signal(config: FactoryConfig) -> dict[str, Any]:
    """The quiet record for a signal switched off by configuration.

    Disabling the capability restores the pre-signal behaviour exactly: no
    watches are kept, `item_stalled` sees no unresolved escalations to yield
    to, and the status still carries the signal's shape so tooling written
    against it does not branch on its presence.
    """
    return {
        "candidate": False,
        "incidents": [],
        "fired": False,
        "page": "not-needed",
        "reason": "signal disabled by configuration",
        "item": None,
        "position": None,
        "repeats": 0,
        "repeat_threshold": config.supervisor.repeat_polls,
        "signature": None,
    }


# --------------------------------------------------- the supervisor item actions

# The outcomes a status reader can act on without parsing prose. `observed`
# is a dry-run record surfaced by a poll; `reserved` is a reservation that
# outlived its poll -- a crash window nobody may auto-replay.
ACTION_OBSERVED = item_actions.STATE_OBSERVED
ACTION_RESERVED = item_actions.STATE_RESERVED

def _action_entry(
    record: item_actions.ItemActionRecord, outcome: str, detail: str
) -> dict[str, Any]:
    """One status entry: the same sentence the run log printed, plus outcome."""
    return {
        "id": record.id,
        "action": record.action,
        "item": record.item,
        "reason": record.reason,
        "flag": record.flag,
        "mode": record.mode,
        "kind": record.kind,
        "signature": record.signature,
        "diagnosis": record.diagnosis,
        "reached_at": record.reached_at,
        "acted_at": record.acted_at,
        "outcome": outcome,
        "detail": detail,
        "record": item_actions.render(record),
    }


def _remember_action(
    sup_state: SupervisorState,
    record: item_actions.ItemActionRecord,
    state_name: str,
    detail: str,
    now: datetime,
) -> None:
    sup_state.action_processing[record.id] = {
        "state": state_name,
        "detail": detail,
        "at": _stamp(now),
        "action": record.action,
        "item": record.item,
    }


def _execute_armed_action(
    config: FactoryConfig,
    paths: FactoryPaths,
    sup_state: SupervisorState,
    record: item_actions.ItemActionRecord,
    path: Path,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Reserve, execute once, and persist the outcome of one armed record.

    The reservation is SAVED before any effect, exactly like a diagnosis
    launch: a crash in between leaves a reservation that suppresses replay
    (nobody may guess a half-run action is safe to repeat) and is reported
    for human review on the next poll. State-touching actions run under the
    tick lock against freshly reloaded state; if a live tick owns the lock
    the action defers and a later safe poll retries it.
    """
    _remember_action(sup_state, record, item_actions.STATE_RESERVED, "", now)
    sup_state.save(paths.supervisor_state_file)
    item_actions.mark_record(
        path, record, state=item_actions.STATE_RESERVED, acted_at=None
    )
    if record.action in item_actions.STATE_LOCKED_ACTIONS:
        try:
            with TickLock(paths.tick_lock):
                fresh = FactoryState.load(paths.state_file)
                outcome = item_actions.dispatch(
                    record, config, paths, fresh, now=now
                )
        except TickLockHeld as error:
            outcome = item_actions.ActionOutcome(
                item_actions.STATE_DEFERRED,
                f"a live tick holds the factory lock; deferred to a later "
                f"poll ({error})",
            )
    else:
        outcome = item_actions.dispatch(record, config, paths, None, now=now)
    acted_at = _stamp(now) if outcome.outcome == item_actions.STATE_ACTED else None
    _remember_action(sup_state, record, outcome.outcome, outcome.detail, now)
    sup_state.save(paths.supervisor_state_file)
    updated = item_actions.mark_record(
        path,
        record,
        state=outcome.outcome,
        detail=outcome.detail,
        acted_at=acted_at,
    )
    return _action_entry(updated, outcome.outcome, outcome.detail)


def _process_item_actions(
    config: FactoryConfig,
    paths: FactoryPaths,
    sup_state: SupervisorState,
    *,
    now: datetime,
    errors: list[str],
) -> dict[str, Any]:
    """Ingest the item-action records before any health or signal is computed.

    Dry-run records are marked observed and surfaced only -- their pinned
    mode is part of their identity, so arming a flag later never makes a
    historical record executable. Armed records are reserved, executed once
    under the tick lock where the action touches state, and updated to
    acted/failed/deferred. A reservation that already outlived its poll is
    reported for human review and never auto-replayed; a deferral is the one
    retryable outcome, picked up again by a later poll.

    Malformed record files stay inert and are reported as errors. Returns
    the status entries (deterministically sorted) and whether factory state
    was written, so the poll can reload it before it observes anything.
    """
    records, read_errors = item_actions.read_action_records(
        paths.item_action_dir
    )
    for problem in read_errors:
        errors.append(f"item-action record not processed: {problem}")

    entries: list[dict[str, Any]] = []
    state_written = False
    for record, path in records:
        memory = sup_state.action_processing.get(record.id)
        if record.mode == item_actions.MODE_DRY_RUN:
            # Pinned dry-run: surfaced, never executed, whatever the live
            # configuration now says.
            detail = "recorded and surfaced; no flag is armed for it"
            if memory is None:
                _remember_action(
                    sup_state, record, ACTION_OBSERVED, detail, now
                )
                updated = item_actions.mark_record(
                    path, record, state=ACTION_OBSERVED
                )
                sup_state.save(paths.supervisor_state_file)
                entries.append(
                    _action_entry(updated, ACTION_OBSERVED, detail)
                )
            else:
                entries.append(
                    _action_entry(
                        record, memory.get("state", ""), memory.get("detail", "")
                    )
                )
            continue

        # An armed record. Terminal memories (acted, failed) are surfaced
        # only; a deferred one is retried on a later poll; a reserved one is
        # a crash window only a person may resolve.
        memory_state = memory.get("state") if memory else None
        if memory_state is None and record.state in (
            item_actions.STATE_ACTED,
            item_actions.STATE_FAILED,
            item_actions.STATE_RESERVED,
        ):
            # The processing memory was lost (a corrupt or deleted
            # supervisor-state.json) but the immutable record itself says the
            # action was already completed, or was reserved and never
            # resolved. Rebuild the memory from the record and surface it --
            # a lost memory file must never turn one executed action into
            # two, and must never auto-replay a crash window.
            memory_state = record.state
            _remember_action(
                sup_state, record, record.state, record.detail, now
            )
            memory = sup_state.action_processing[record.id]
            sup_state.save(paths.supervisor_state_file)
        if memory_state in (item_actions.STATE_ACTED, item_actions.STATE_FAILED):
            detail = memory.get("detail", "") if memory else record.detail
            entries.append(_action_entry(record, memory_state, detail))
            continue
        if memory_state == item_actions.STATE_RESERVED:
            errors.append(
                f"{record.action} #{record.item}: action record {record.id} "
                f"holds an unresolved reservation from {memory.get('at')}; a "
                f"person must review it before anything retries"
            )
            entries.append(_action_entry(record, ACTION_RESERVED, memory.get("detail", "")))
            continue
        entry = _execute_armed_action(
            config, paths, sup_state, record, path, now=now
        )
        state_written = True
        entries.append(entry)

    return {"entries": entries, "state_written": state_written}


# ------------------------------------------------- the one-shot diagnosis agent

# The launch outcomes a status reader can act on without parsing prose.
DIAGNOSIS_NOT_DUE = "not-due"
DIAGNOSIS_LAUNCHED = "launched"
DIAGNOSIS_ACTIVE = "active"
DIAGNOSIS_INSIDE_GRACE = "inside-grace"
DIAGNOSIS_ALREADY_RAN = "already-ran"
DIAGNOSIS_FAILED = "failed"


def _diagnosis_key(kind: str, item: int | None, incident: str | None = None) -> str:
    """The launch-memory key: the PUBLIC signal kind, its item, its incident.

    The per-item escalation incidents each get their own key, so two overdue
    items are two launches and never one aggregate request. `loop_dead` may
    carry no item (a factory-wide signal); it still gets one stable key.

    `incident` is the internal stable discriminator the multi-incident
    infrastructure signals carry (a `stage:provider` pair, or a vanished
    run's id): two different faults on one item are two launches, and one
    must never overwrite the other's launch record. The original three
    kinds pass no incident, so their existing keys are unchanged.
    """
    base = f"{kind}:{item if item is not None else '-'}"
    return f"{base}:{incident}" if incident is not None else base


def _diagnosis_outcome(
    kind: str,
    item: int | None,
    state_name: str,
    *,
    signature: str,
    record: dict[str, Any] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """One status-shaped outcome; the record, when present, is the source."""
    return {
        "kind": kind,
        "item": item,
        "state": state_name,
        "signature": signature,
        "run_id": record.get("adw_id") if record else None,
        "pid": record.get("pid") if record else None,
        "launched_at": record.get("launched_at") if record else None,
        "log": record.get("log") if record else None,
        "detail": detail,
    }


def _dispatch_diagnosis(
    config: FactoryConfig,
    paths: FactoryPaths,
    sup_state: SupervisorState,
    *,
    kind: str,
    item: int | None,
    signature: str,
    reason: str,
    now: datetime,
    errors: list[str],
    incident: str | None = None,
) -> dict[str, Any]:
    """Wake one diagnosis agent for one incident, or say why not.

    The identity is the incident's existing stable signature -- never the
    poll time, elapsed age or repeat count, which change every tick and
    would make every poll a new incident. An unchanged signature has been
    diagnosed and never launches again; a live or freshly launched run
    suppresses the next (the predecessor project stacked a new agent every
    tick while the first was still working); only a materially different
    signature launches again once those guards clear.

    The reservation is SAVED before the spawn and completed after it: a
    crash in between leaves a reservation that suppresses duplicate
    launches for the grace window (fail toward temporary suppression, never
    a duplicate agent), while a synchronous launch failure removes it so the
    next poll retries.
    """
    key = _diagnosis_key(kind, item, incident)
    record = sup_state.diagnoses.get(key)
    grace = config.supervisor.diagnosis_grace_seconds

    if record is not None:
        pid = record.get("pid")
        if pid and launcher.is_alive(pid):
            return _diagnosis_outcome(
                kind, item, DIAGNOSIS_ACTIVE,
                signature=signature, record=record,
                detail=(
                    f"diagnosis run {record.get('adw_id')} (pid {pid}) is "
                    f"still working on this signal"
                ),
            )
        if record.get("launched") and record.get("signature") == signature:
            # The last successfully launched signature outlives its process:
            # an unchanged incident never relaunches every poll.
            return _diagnosis_outcome(
                kind, item, DIAGNOSIS_ALREADY_RAN,
                signature=signature, record=record,
                detail="this incident signature has already been diagnosed",
            )
        moment = _moment(record.get("launched_at"))
        age = (now - moment).total_seconds() if moment is not None else None
        if age is not None and age < grace:
            remaining = grace - age
            return _diagnosis_outcome(
                kind, item, DIAGNOSIS_INSIDE_GRACE,
                signature=signature, record=record,
                detail=(
                    f"inside the {grace:.0f}s diagnosis launch grace "
                    f"({remaining:.0f}s remaining)"
                ),
            )

    adw_id = launcher.new_adw_id()
    reservation = {
        "kind": kind,
        "item": item,
        "signature": signature,
        "adw_id": adw_id,
        "launched_at": _stamp(now),
        "pid": None,
        "log": None,
        "launched": False,
    }
    sup_state.diagnoses[key] = reservation
    sup_state.save(paths.supervisor_state_file)
    try:
        started = launcher.launch_diagnosis(
            kind=kind,
            item=item,
            signature=signature,
            reason=reason,
            observed_at=_iso(now),
            root=paths.root,
            log_dir=paths.diagnosis_log_dir,
            config_rel=config.sssf_config,
            adw_id=adw_id,
            moment=now,
        )
    except launcher.LaunchError as error:
        # Synchronous failure: nothing is running, so the reservation comes
        # out again and the next poll retries. Visible as a status error,
        # never as a silently missing diagnosis.
        sup_state.diagnoses.pop(key, None)
        sup_state.save(paths.supervisor_state_file)
        errors.append(f"{kind}: diagnosis agent launch failed: {error}")
        return _diagnosis_outcome(
            kind, item, DIAGNOSIS_FAILED,
            signature=signature, record=reservation, detail=str(error),
        )
    reservation.update(pid=started.pid, log=str(started.log_file), launched=True)
    sup_state.save(paths.supervisor_state_file)
    return _diagnosis_outcome(
        kind, item, DIAGNOSIS_LAUNCHED,
        signature=signature, record=reservation,
        detail=f"diagnosis agent {adw_id} woke for this signal",
    )


def _diagnose_signals(
    config: FactoryConfig,
    paths: FactoryPaths,
    sup_state: SupervisorState,
    signals: dict[str, Any],
    *,
    now: datetime,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Dispatch every due incident, one agent per incident, read-only.

    Due means the incident's repeat count has reached the signal's existing
    threshold -- independently of whether human paging is enabled or the
    page was delivered: the agent's job is to work out what is wrong, and a
    person not receiving a page does not make the diagnosis less owed. The
    simple signals dispatch their own record; `escalation_unanswered`
    dispatches each overdue incident separately and never one aggregate
    request for several items.
    """
    outcomes: list[dict[str, Any]] = []

    def dispatch(record: dict[str, Any], kind: str) -> dict[str, Any]:
        signature = record.get("signature")
        due = bool(
            record.get("candidate")
            and signature
            and record.get("repeats", 0) >= record.get("repeat_threshold", 0)
        )
        if not due:
            if not record.get("candidate"):
                detail = "the signal is not a candidate this poll"
            else:
                detail = (
                    f"{record.get('repeats', 0)} of "
                    f"{record.get('repeat_threshold', 0)} consecutive polls"
                )
            return _diagnosis_outcome(
                kind, record.get("item"), DIAGNOSIS_NOT_DUE,
                signature=signature, detail=detail,
            )
        return _dispatch_diagnosis(
            config,
            paths,
            sup_state,
            kind=kind,
            item=record.get("item"),
            signature=signature,
            reason=record.get("reason") or "",
            now=now,
            errors=errors,
            incident=record.get("incident"),
        )

    for kind in (LOOP_DEAD, ITEM_STALLED, DISK_BELOW_FLOOR):
        outcome = dispatch(signals[kind], kind)
        signals[kind]["diagnosis"] = outcome
        outcomes.append(outcome)

    for kind in (
        ESCALATION_UNANSWERED,
        PROVIDER_CREDIT_EXHAUSTED,
        RUN_VANISHED,
    ):
        for incident in signals[kind].get("incidents", []):
            outcome = dispatch(incident, kind)
            incident["diagnosis"] = outcome
            outcomes.append(outcome)
    return outcomes


def run_poll(
    config: FactoryConfig, paths: FactoryPaths, now: datetime | None = None
) -> dict[str, Any]:
    """One poll. Reads the world, decides, replaces the status file.

    Returns the status payload that was written. Raises `OSError` when the
    poll could not persist its result -- a detected factory problem is a
    successful poll, and the caller must not report it as a crashed task --
    and `SupervisorPollLockHeld` when another poll is inside the same
    transaction, which the caller reports as a harmlessly skipped poll.

    The whole transaction runs under the inter-process poll lock: state
    load, item-action ingestion, signal evaluation, diagnosis reservation
    and spawn, and the supervisor's own state/status writes. Two polls
    racing from an empty directory must not both wake an agent on one
    signal, nor both take the same armed action.
    """
    now = now or _now()
    paths.ensure_state_dir()
    with SupervisorPollLock(paths.supervisor_poll_lock):
        from . import recovery
        # Recovery owns separate state/audit files and never calls decide.
        # Run it before ordinary health reads, so they observe the new facts.
        recovered = recovery.run_recovery(config, paths, now)
        payload = _run_poll_locked(config, paths, now)
        payload["recovery"] = recovered
        if recovered["errors"]:
            payload["errors"].extend(recovered["errors"])
            payload["health"] = "decision_point"
        if any(r.get("candidate") for r in recovered["runs"]):
            payload["health"] = "decision_point"
        _atomic_write(paths.supervisor_status_file,
                      json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return payload


def _run_poll_locked(
    config: FactoryConfig, paths: FactoryPaths, now: datetime
) -> dict[str, Any]:
    """The poll itself, already serialized against every other poll."""
    errors: list[str] = []

    # ---- what the factory owns; read, never written ---------------------
    state = FactoryState.load(paths.state_file)

    driver = read_pid_owner(paths.driver_pid)
    # Judged by TickLock's own staleness bound, so "holds the tick lock" means
    # the same thing here as it does to the next tick: a lock past the bound is
    # reclaimable and therefore unheld, whatever PID it names.
    lock = read_pid_owner(
        paths.tick_lock, max_age_seconds=TICK_LOCK_MAX_AGE_SECONDS
    )

    # ---- the supervisor's item actions -----------------------------------
    # Read and processed before any health or signal is computed, because an
    # armed action can change the very facts (budgets, labels, tracked runs)
    # the rest of this poll observes -- and because a dry-run record is news
    # of its own, not a fault to fold into a signal. Dry-run records cost no
    # business-state write at all; only an armed action may, and only its one
    # named mutation.
    sup_state = SupervisorState.load(paths.supervisor_state_file)
    from .recovery import paused
    maintenance = paused(paths)
    actions = ({"entries": [], "state_written": False} if maintenance else
               _process_item_actions(config, paths, sup_state, now=now, errors=errors))
    if actions["state_written"]:
        state = FactoryState.load(paths.state_file)
    moment = _moment(state.last_tick_at)
    age = (now - moment).total_seconds() if moment is not None else None

    # ---- the open item set ------------------------------------------------
    try:
        open_items = labels.eligible_items(config, paths.root)
    except labels.LabelError as error:
        open_items = None
        errors.append(f"cannot list open items: {error}")

    active: int | None = None
    if open_items:
        # The item already underway, else the head of the FIFO queue the tick
        # would pick.
        active = (
            state.active_item if state.active_item in open_items else open_items[0]
        )
    position, blocked = (
        _position_of(config, paths, active, errors) if active is not None else (None, False)
    )

    # ---- signals ----------------------------------------------------------
    candidate, reason = _loop_dead_reading(
        config, open_items=open_items, age=age, driver=driver, lock=lock
    )
    # The signature is built from canonical facts only: the tick as one UTC
    # instant whatever form it was written in, and the open item set as a set
    # -- sorted, because the order GitHub happens to return the queue in is
    # not part of the incident. The live FIFO order above is untouched; only
    # what the signature sees is canonicalized.
    signature = _fingerprint(
        LOOP_DEAD,
        _canonical_tick(state.last_tick_at),
        ",".join(str(item) for item in sorted(open_items or [])),
    )

    # ---- unresolved escalations -------------------------------------------
    # Read-only, like everything above: detection never judges an item,
    # launches a run or spends a budget -- it times the escalations the
    # decider already sent and recorded, and stops watching the moment the
    # item is unblocked, closed or merged. With the capability switched off,
    # the watches are FORGOTTEN rather than frozen (nothing can age or
    # resolve them in the dark), and `item_stalled` yields to nothing.
    escalation_enabled = config.supervisor.capabilities.escalation_unanswered
    escalated_for_stall: frozenset[int] = frozenset()
    if escalation_enabled:
        escalations, uncertain = _escalation_observations(
            config,
            paths,
            state,
            open_items=open_items,
            sup_state=sup_state,
            now=now,
            errors=errors,
        )
        escalated_for_stall = (
            frozenset(watch.item for watch in escalations) | uncertain
        )
    else:
        escalations = []
        _forget_escalation_watches(sup_state)

    # ---- the stalled-item observation -------------------------------------
    # Read-only, like everything above: the observation is the fingerprint's
    # raw material, and the tick alone may act on what it describes. A live
    # tracked run anywhere in the factory suppresses the candidate outright --
    # work is happening -- while the run's mere PRESENCE in the tracked set is
    # already part of the signature, so a run that starts and exits between
    # polls cannot leave the old repeat count standing.
    observation = _observe_item(config, paths, state, active, now=now, errors=errors)
    observations: dict[int, ItemObservation] = (
        {active: observation} if active is not None else {}
    )

    # ---- the infrastructure faults -----------------------------------------
    # Three deterministic machine readings, evaluated before the stall
    # classification so a specific fault can own the page a generic "nothing
    # is moving" would otherwise send: the disk under the launch floor, the
    # credit drained out of a provider the next launch needs, and a run that
    # died without landing anything. All read-only, all model-free, all
    # counted through the same per-signal repeat/threshold/dedup machinery
    # as every other signal (issue #111).
    disk = _disk_signal(config, paths, sup_state, errors=errors)
    credit = _credit_signal(
        config, paths, state, observation, sup_state, now=now, errors=errors
    )
    vanished = _run_vanished_signal(
        config, paths, state, sup_state, observations, now=now, errors=errors
    )

    live_run = next(
        (run for run in state.tracked if launcher.is_alive(run.pid)), None
    )
    infra = InfrastructureClassification(
        disk_below_floor=disk.confirmed,
        disk_unreadable=disk.unreadable,
        credit_exhausted=credit.confirmed,
        credit_unreadable=credit.unreadable,
        run_vanished=active in vanished.vanished_items,
    )
    stall_candidate, stall_reason = _item_stalled_reading(
        config,
        observation,
        live_run=live_run,
        escalated_items=escalated_for_stall,
        infra=infra,
    )

    signals = {
        LOOP_DEAD: _run_signal(
            config,
            sup_state,
            LOOP_DEAD,
            candidate,
            reason,
            signature,
            item=active,
            position=position,
            last_tick=state.last_tick_at,
            errors=errors,
        ),
        ITEM_STALLED: _run_signal(
            config,
            sup_state,
            ITEM_STALLED,
            stall_candidate,
            stall_reason,
            _observation_signature(observation),
            item=observation.item,
            position=observation.position,
            last_tick=None,
            errors=errors,
        ),
        ESCALATION_UNANSWERED: (
            _escalation_signal(config, sup_state, escalations, errors=errors)
            if escalation_enabled
            else _disabled_escalation_signal(config)
        ),
        DISK_BELOW_FLOOR: disk.record,
        PROVIDER_CREDIT_EXHAUSTED: credit.record,
        RUN_VANISHED: vanished.record,
    }

    # ---- the one-shot diagnosis agents -------------------------------------
    # Every due incident wakes at most one read-only agent, right here: after
    # the repeat/signature facts exist, before the state/status writes, and
    # under the poll lock so a racing poll cannot double-launch it. A launch
    # failure is one visible error on one signal; the rest of the poll stands.
    diagnoses = ([] if maintenance else _diagnose_signals(
        config, paths, sup_state, signals, now=now, errors=errors
    ))

    # An armed action that failed or deferred is a decision point for a
    # person; a surfaced dry-run record is news, not a fault, and must never
    # turn a healthy poll into one.
    action_decision = any(
        entry["mode"] == item_actions.MODE_ARMED
        and entry["outcome"] in (
            item_actions.STATE_FAILED,
            item_actions.STATE_DEFERRED,
            ACTION_RESERVED,
        )
        for entry in actions["entries"]
    )

    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "polled_at": _iso(now),
        "repo": config.repo,
        "health": (
            "decision_point"
            if (
                errors
                or blocked
                or action_decision
                or any(s["candidate"] for s in signals.values())
            )
            else "healthy"
        ),
        "active_item": active,
        "position": position,
        "blocked": blocked,
        "open_items": open_items,
        "open_item_count": len(open_items) if open_items is not None else None,
        "last_tick_at": state.last_tick_at,
        "last_tick_age_seconds": round(age, 1) if age is not None else None,
        "silence_window_seconds": config.supervisor.silence_window_seconds,
        "driver_alive": driver.alive,
        "driver_detail": driver.detail,
        "tick_lock_alive": lock.alive,
        "tick_lock_detail": lock.detail,
        "signals": signals,
        "diagnoses": diagnoses,
        "item_actions": actions["entries"],
        "errors": errors,
    }

    # Supervisor-owned writes close the poll. `state.json` is not among them
    # here: the only path that ever writes it from this poll is an armed item
    # action, and that wrote through `FactoryState.save_action` under the tick
    # lock, before anything was observed.
    sup_state.save(paths.supervisor_state_file)
    _atomic_write(
        paths.supervisor_status_file, json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    return payload


def summarize(payload: dict[str, Any]) -> str:
    """The one-line-per-fact report `python -m adws_factory supervisor` prints."""
    active = (
        f"#{payload['active_item']} at {payload['position']}"
        if payload["active_item"] is not None
        else "none"
    )
    items = (
        ", ".join(f"#{item}" for item in payload["open_items"])
        if payload["open_items"]
        else "none"
    )
    age = payload["last_tick_age_seconds"]
    tick = f"{age:.0f}s ago" if age is not None else "never"
    lines = [
        f"health    {payload['health']}",
        f"active    {active}",
        f"open      {items}",
        f"tick      {tick}",
        f"driver    {'alive' if payload['driver_alive'] else 'no live driver'}",
        f"lock      {'held' if payload['tick_lock_alive'] else 'no live tick'}",
    ]
    for name, signal in payload["signals"].items():
        if name in (
            ESCALATION_UNANSWERED,
            PROVIDER_CREDIT_EXHAUSTED,
            RUN_VANISHED,
        ):
            # Printed below, one line per incident: an overdue item owes the
            # operator its subject and its clock, and an infrastructure fault
            # owes the provider+stage or run+stage that names it -- facts the
            # generic line would bury.
            continue
        state = "CANDIDATE" if signal["candidate"] else "no"
        if signal["candidate"]:
            who = ""
            if signal.get("item") is not None:
                who = f"#{signal['item']}"
                if signal.get("position"):
                    who += f" at {signal['position']}"
                who += " "
            state += f" {who}({signal['repeats']}/{signal['repeat_threshold']}"
            state += ", page " + signal["page"] + ")"
        lines.append(f"signal    {name}: {state} -- {signal['reason']}")
    escalation = payload["signals"].get(ESCALATION_UNANSWERED)
    if escalation:
        state = "CANDIDATE" if escalation["candidate"] else "no"
        lines.append(
            f"signal    {ESCALATION_UNANSWERED}: {state} -- {escalation['reason']}"
        )
        for incident in escalation.get("incidents", []):
            timing = (
                f"unanswered {incident['age_seconds']:.0f}s, "
                f"grace {incident['grace_seconds']:.0f}s"
                if incident["state"] == "unanswered"
                else f"inside grace, {incident['age_seconds']:.0f}s of "
                f"{incident['grace_seconds']:.0f}s"
            )
            where = f"#{incident['item']} at {incident['stage']}"
            lines.append(
                f"  {where}: {timing}, page {incident['page']} -- "
                f"{incident['subject']}"
            )
    for kind in (PROVIDER_CREDIT_EXHAUSTED, RUN_VANISHED):
        aggregate = payload["signals"].get(kind)
        if not aggregate:
            continue
        state = "CANDIDATE" if aggregate["candidate"] else "no"
        lines.append(f"signal    {kind}: {state} -- {aggregate['reason']}")
        for incident in aggregate.get("incidents", []):
            if kind == PROVIDER_CREDIT_EXHAUSTED:
                where = (
                    f"#{incident['item']} at {incident['stage']} needs "
                    f"{incident['provider']}"
                )
            else:
                where = (
                    f"run {incident['adw_id']} on #{incident['item']} at "
                    f"{incident['stage']}"
                )
            lines.append(
                f"  {where}: {incident['repeats']}/{incident['repeat_threshold']}, "
                f"page {incident['page']} -- {incident['reason']}"
            )
    # One line per diagnosis outcome, so a person can see at a glance that an
    # agent woke (and where its log is) or why one did not.
    for outcome in payload.get("diagnoses") or []:
        kind = outcome.get("kind", "?")
        item = outcome.get("item")
        who = f"{kind} #{item}" if item is not None else kind
        line = f"diagnosis {who}: {outcome.get('state')}"
        if outcome.get("log"):
            line += f", log {outcome['log']}"
        if outcome.get("detail"):
            line += f" -- {outcome['detail']}"
        lines.append(line)
    # One line per item action, so the on-demand report agrees with the
    # status JSON and the diagnosis run log: the action, the item, the gate,
    # and what became of it this poll.
    for entry in payload.get("item_actions") or []:
        line = (
            f"action     {entry['action']} #{entry['item']}: "
            f"{entry['outcome']}"
        )
        if entry.get("flag"):
            line += f" ({entry['flag']} = {entry['mode']})"
        if entry.get("detail"):
            line += f" -- {entry['detail']}"
        lines.append(line)
    recovery_status = payload.get("recovery", {})
    if recovery_status.get("paused"):
        lines.append("recovery  maintenance pause (live ticks also paused)")
    for entry in recovery_status.get("runs", []):
        if entry.get("candidate"):
            lines.append(f"recovery  hang candidate {entry['adw_id']}: "
                         f"idle {entry['idle_seconds']:.0f}s, polls {entry['repeats']}")
    for entry in recovery_status.get("actions", []):
        lines.append(f"recovery  {entry['action']}: {entry.get('outcome')}")
    for error in payload["errors"]:
        lines.append(f"error     {error}")
    lines.append("status    written to supervisor-status.json")
    return "\n".join(lines)
