"""The supervisor's five item actions: narrow, gated, and recorded first.

Every action here is about the loop's own state, never about the work: clear
a budget a launcher failure spent, relaunch a parked item, page again on an
escalation nobody answered, put an item on hold, ask the external reviewer
again. Each one is reached by the diagnosis agent's reasoning, recorded as an
immutable audit record naming the action, the item, the reason and the flag
holding it back -- and, while its flag says `dry_run`, the record IS the whole
deliverable. Nothing happens.

Why they stay off by default: an automated gate that enacted its own judgement
once destroyed a pull request that had met most of its requirements, on five
findings that were all false. Any action here is one step from un-parking an
item the decider parked on a real blocker (ADR-0002 puts that authority with
the decider), so the supervisor must not take it back by accident. A person
arms exactly one flag, for exactly one action, deliberately.

Hard boundaries, all deliberate:

* One flag, one action. `may_clear_budget` authorizes `clear_budget` and
  nothing else; a record naming another action never rides along.
* The mode is PINNED in the record when reasoning reaches the action. A
  historical `dry_run` record never becomes executable because the operator
  later armed its flag -- arming one flag must not replay a month of audit
  records.
* Loop-state operations only. Nothing here calls the decider, adjudicates a
  finding, merges, closes an item, rewinds/resets/reverts Git history, edits
  or deletes a branch or worktree, or prunes work.
* Every handler catches its own infrastructure failures and reports them as
  the action's outcome, so one bad request never aborts the status of the
  others.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import artifacts, escalate, labels
from .artifacts import GhError
from .config import ITEM_ACTION_FLAGS, FactoryConfig
from .labels import LabelError
from .launcher import LaunchError
from .paths import FactoryPaths
from .state import FactoryState
from .tick import TickReport, launch_stage, unsatisfied_stages

__version__ = "0.1.0"

MODE_DRY_RUN = "dry_run"
MODE_ARMED = "armed"

# The record lifecycle. `reached` is what the diagnosis tool writes; a poll
# then moves it exactly once through the processing states below. Terminal
# states are never re-entered: a record that was processed stays processed.
STATE_REACHED = "reached"
STATE_OBSERVED = "observed"    # a poll surfaced a dry-run record; nothing else
STATE_RESERVED = "reserved"    # a poll owns the armed record; effect not yet run
STATE_ACTED = "acted"
STATE_FAILED = "failed"
STATE_DEFERRED = "deferred"

STATES = (
    STATE_REACHED,
    STATE_OBSERVED,
    STATE_RESERVED,
    STATE_ACTED,
    STATE_FAILED,
    STATE_DEFERRED,
)

# The rendered prefixes for an armed record a poll has processed. Everything
# else -- dry-run records above all -- renders as WOULD-ACT, which is the
# honest sentence: reasoning reached it, the flag did not. A reserved armed
# record is the one exception that must NOT read as a would-act: the
# reservation is already on disk, so the effect may have run inside a crash
# window, and the sentence surfaced for human review has to say so.
WOULD_ACT = "WOULD-ACT"
OUTCOME_PREFIX = {
    STATE_RESERVED: "ACTION-RESERVED",
    STATE_ACTED: "ACTED",
    STATE_FAILED: "ACTION-FAILED",
    STATE_DEFERRED: "ACTION-DEFERRED",
}

# The actions whose effect touches `state.json` (a budget clear, a launch and
# its tracking). The poll holds the tick lock around exactly these; the other
# three write GitHub or Signal, never factory state.
STATE_LOCKED_ACTIONS = ("clear_budget", "relaunch_item")

# The environment a diagnosis run binds its incident into before the agent
# call. The action tools spawn a Python request that reads these, so a model
# cannot select another item by supplying an arbitrary number: the binding is
# process state, not a tool argument.
ENV_ITEM = "SSSF_SUPERVISOR_ITEM"
ENV_KIND = "SSSF_SUPERVISOR_KIND"
ENV_SIGNATURE = "SSSF_SUPERVISOR_SIGNATURE"
ENV_DIAGNOSIS = "SSSF_SUPERVISOR_DIAGNOSIS"

_BECAUSE = "  because  "
_CONTINUATION = " " * len(_BECAUSE)
_WRAP_WIDTH = 100


class ItemActionError(RuntimeError):
    """An action request could not be turned into a record. Never a dispatch."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """A full-precision UTC ISO stamp, so clock arithmetic keeps every fraction."""
    return moment.astimezone(timezone.utc).isoformat()


def _moment(stamp: str | None) -> datetime | None:
    """Parse a timestamp this module (or the factory) wrote. Naive means UTC."""
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- the record

@dataclass(frozen=True)
class ItemActionRecord:
    """One reached action: the reasoning's own account of what it would do.

    Everything a person needs to audit the decision BEFORE it has
    consequences. The identity fields (`id`, `action`, `item`, `kind`,
    `signature`, `diagnosis`, `flag`, `mode`) never change after creation;
    only the processing fields (`state`, `detail`, `acted_at`) move, once,
    under the supervisor's poll lock.
    """

    id: str
    action: str
    item: int
    reason: str
    flag: str
    mode: str
    kind: str
    signature: str
    diagnosis: str
    reached_at: str
    state: str = STATE_REACHED
    detail: str = ""
    acted_at: str | None = None


@dataclass(frozen=True)
class ActionOutcome:
    """What one dispatch did: the outcome and the sentence explaining it."""

    outcome: str  # acted | failed | deferred
    detail: str


def record_id(kind: str, signature: str, item: int, action: str) -> str:
    """The deterministic identity of one reached action.

    Built from the incident signature, the item and the action -- never from
    the reach time or the reason, which change between retries. The same
    incident reaching the same action again is the SAME record: retries
    cannot create or enact duplicates, and only a materially different
    incident (a new signature) can reach a fresh one.
    """
    return hashlib.sha256(
        f"{kind}|{signature}|{item}|{action}".encode("utf-8", "replace")
    ).hexdigest()[:16]


def record_file(directory: Path, record_id: str, action: str, item: int) -> Path:
    return directory / f"{action}_{item}_{record_id}.json"


def _id_in_name(name: str) -> str:
    stem = name[:-5] if name.endswith(".json") else name
    return stem.rsplit("_", 1)[-1]


def parse_record(raw: Any) -> ItemActionRecord | None:
    """One action record, or None when any field cannot be trusted.

    Strict on purpose: a record that acts must be whole. The flag must be the
    one that belongs to the named action (a record may never let one flag
    authorize another action name), the mode must be a real mode, and every
    timestamp must parse. Partially parsed data is inert.
    """
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if not isinstance(action, str) or action not in ITEM_ACTION_FLAGS:
        return None
    item = raw.get("item")
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        return None
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    mode = raw.get("mode")
    if mode not in (MODE_DRY_RUN, MODE_ARMED):
        return None
    flag = raw.get("flag")
    if flag != ITEM_ACTION_FLAGS[action]:
        return None
    identifier = raw.get("id")
    kind = raw.get("kind")
    signature = raw.get("signature")
    diagnosis = raw.get("diagnosis")
    for text in (identifier, kind, signature, diagnosis):
        if not isinstance(text, str) or not text:
            return None
    reached_at = raw.get("reached_at")
    if not isinstance(reached_at, str) or _moment(reached_at) is None:
        return None
    state = raw.get("state")
    if state not in STATES:
        return None
    detail = raw.get("detail")
    if detail is None:
        detail = ""
    if not isinstance(detail, str):
        return None
    acted_at = raw.get("acted_at")
    if acted_at is not None and (
        not isinstance(acted_at, str) or _moment(acted_at) is None
    ):
        return None
    return ItemActionRecord(
        id=identifier,
        action=action,
        item=item,
        reason=reason,
        flag=flag,
        mode=mode,
        kind=kind,
        signature=signature,
        diagnosis=diagnosis,
        reached_at=reached_at,
        state=state,
        detail=detail,
        acted_at=acted_at,
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        raise


def write_record(record: ItemActionRecord, path: Path) -> None:
    _atomic_write(path, json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")


def mark_record(
    path: Path, record: ItemActionRecord, *, state: str, detail: str = "",
    acted_at: str | None = None,
) -> ItemActionRecord:
    """Move one record to a processing state and persist it, identity intact."""
    updated = replace(record, state=state, detail=detail, acted_at=acted_at)
    write_record(updated, path)
    return updated


def read_action_records(
    directory: Path,
) -> tuple[list[tuple[ItemActionRecord, Path]], list[str]]:
    """Every valid record in the directory, with its file, and why others are not.

    Malformed files are reported, never acted on: the caller surfaces them as
    status errors for a person, and partially parsed data stays inert. The
    record's identity is cross-checked against its filename so a renamed or
    hand-edited file cannot smuggle an id.
    """
    try:
        files = sorted(path for path in directory.glob("*.json") if path.is_file())
    except OSError:
        return [], []
    records: list[tuple[ItemActionRecord, Path]] = []
    errors: list[str] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{path.name}: unreadable ({error})")
            continue
        record = parse_record(raw)
        if record is None:
            errors.append(f"{path.name}: not a valid item-action record")
            continue
        if record.id != _id_in_name(path.name):
            errors.append(
                f"{path.name}: record id {record.id} does not match its filename"
            )
            continue
        records.append((record, path))
    records.sort(key=lambda pair: (pair[0].reached_at, pair[0].id))
    return records, errors


# ---------------------------------------------------------------- rendering

def _reason_lines(reason: str) -> list[str]:
    """The `because` block: the complete reason, wrapped to be readable.

    The reason is the deliverable's substance and is never truncated. A long
    one wraps with its continuation aligned under `because`; a reason the
    model wrote across several lines keeps its own line breaks.
    """
    lines: list[str] = []
    for index, raw in enumerate(reason.splitlines() or [""]):
        wrapped = textwrap.fill(
            raw,
            width=_WRAP_WIDTH,
            initial_indent=_BECAUSE if index == 0 else _CONTINUATION,
            subsequent_indent=_CONTINUATION,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(wrapped.splitlines() or [_BECAUSE])
    return lines


def render(record: ItemActionRecord) -> str:
    """The canonical sentence for one record, used everywhere it appears.

    The same formatter feeds the tool output, the diagnosis run log, the
    status JSON and the summary, so a reader of any one of them sees the
    same line a reader of the others does. A record with an armed outcome
    says so in its prefix and carries the outcome's own detail.
    """
    prefix = (
        OUTCOME_PREFIX[record.state]
        if record.mode == MODE_ARMED and record.state in OUTCOME_PREFIX
        else WOULD_ACT
    )
    lines = [f"{prefix}  {record.action}  #{record.item}"]
    lines.extend(_reason_lines(record.reason))
    lines.append(f"  gated by {record.flag} = {record.mode}")
    if (
        record.mode == MODE_ARMED
        and record.state in OUTCOME_PREFIX
        # A reserved record renders its prefix and nothing more: it has no
        # outcome yet and has not acted, so neither line may appear -- not
        # even for a hand-edited file that carries one.
        and record.state != STATE_RESERVED
    ):
        if record.detail:
            lines.append(f"  outcome  {record.state} -- {record.detail}")
        if record.acted_at:
            lines.append(f"  acted at {record.acted_at}")
    return "\n".join(lines)


# ---------------------------------------------------------------- requesting

def request_action(
    *,
    action: str,
    reason: str,
    kind: str,
    signature: str,
    item: int,
    diagnosis: str,
    mode: str,
    directory: Path,
    now: datetime | None = None,
) -> ItemActionRecord:
    """Create (or idempotently return) the record for one reached action.

    The mode is pinned HERE, from the configuration in force at the moment
    reasoning reached the action. It is part of the record's identity from
    then on: a later configuration change re-reads nothing.
    """
    if action not in ITEM_ACTION_FLAGS:
        raise ItemActionError(
            f"unknown action {action!r}; the five are: "
            f"{', '.join(sorted(ITEM_ACTION_FLAGS))}"
        )
    reason = (reason or "").strip()
    if not reason:
        raise ItemActionError(
            f"{action}: a reached action needs its complete reason"
        )
    for name, value in (
        ("signal kind", kind),
        ("incident signature", signature),
        ("diagnosis run id", diagnosis),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ItemActionError(f"{action}: missing the bound {name}")
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise ItemActionError(
            f"{action}: item actions are bound to one incident item; a "
            f"factory-wide incident has none to act on"
        )
    if mode not in (MODE_DRY_RUN, MODE_ARMED):
        raise ItemActionError(f"{action}: unknown mode {mode!r}")

    identifier = record_id(kind, signature, item, action)
    path = record_file(directory, identifier, action, item)
    if path.exists():
        try:
            existing = parse_record(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            existing = None
            problem = str(error)
        else:
            problem = "not a valid item-action record"
        if existing is None:
            raise ItemActionError(
                f"{path.name} already exists but cannot be read back ({problem}); "
                f"refusing to overwrite it"
            )
        return existing

    record = ItemActionRecord(
        id=identifier,
        action=action,
        item=item,
        reason=reason,
        flag=ITEM_ACTION_FLAGS[action],
        mode=mode,
        kind=kind,
        signature=signature,
        diagnosis=diagnosis,
        reached_at=_stamp(now or _now()),
        state=STATE_REACHED,
    )
    write_record(record, path)
    return record


def request_from_environment(
    action: str, reason: str, *, environ: dict[str, str] | None = None,
    config: FactoryConfig | None = None, paths: FactoryPaths | None = None,
) -> ItemActionRecord:
    """The entry the diagnosis agent's tools reach: env-bound, config-pinned.

    The incident context (item, kind, signature, diagnosis run id) comes from
    the process environment the diagnosis runner bound before the agent call,
    never from arguments -- a model cannot select another item by supplying
    an arbitrary number. The action mode is read from the factory
    configuration in force right now and pinned into the record.
    """
    env = os.environ if environ is None else environ
    item_text = (env.get(ENV_ITEM) or "").strip()
    if not item_text:
        raise ItemActionError(
            f"{action}: no incident item is bound in the environment; item "
            f"actions never apply to a factory-wide incident"
        )
    try:
        item = int(item_text)
    except ValueError:
        raise ItemActionError(
            f"{action}: the bound incident item {item_text!r} is not a number"
        ) from None
    kind = (env.get(ENV_KIND) or "").strip()
    signature = (env.get(ENV_SIGNATURE) or "").strip()
    diagnosis = (env.get(ENV_DIAGNOSIS) or "").strip()

    flag = ITEM_ACTION_FLAGS[action]
    if config is None:
        config = FactoryConfig.load()
    if paths is None:
        paths = FactoryPaths()
    mode = getattr(config.supervisor.item_actions, flag)
    return request_action(
        action=action,
        reason=reason,
        kind=kind,
        signature=signature,
        item=item,
        diagnosis=diagnosis,
        mode=mode,
        directory=paths.item_action_dir,
    )


def records_for_diagnosis(
    directory: Path, diagnosis: str, signature: str
) -> list[tuple[ItemActionRecord, Path]]:
    """Every valid record this incident reached, oldest first.

    Matched on the diagnosis run id AND the incident signature: a run id can
    be reused by a later incident on the same signal, and the run log must
    never attribute one incident's reached actions to another. Used by the
    diagnosis runner itself so the log prints every record the incident
    produced even when the agent call later failed.
    """
    records, _ = read_action_records(directory)
    return [
        pair
        for pair in records
        if pair[0].diagnosis == diagnosis and pair[0].signature == signature
    ]


# ---------------------------------------------------------------- dispatching

def _act_clear_budget(
    record: ItemActionRecord,
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    *,
    now: datetime,
) -> ActionOutcome:
    budget = state.budgets.get(str(record.item))
    if budget is None:
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} has no persisted budget; there is nothing to clear",
        )
    spent = (
        f"work={budget.work_attempts}, launches={budget.launches}, "
        f"rounds={budget.rounds}"
    )
    state.clear_budget(record.item)
    # `save_action`, never `save`: an item action is not a tick, and stamping
    # the tick clock here would reset the supervisor's silence window.
    state.save_action(paths.state_file)
    return ActionOutcome(
        STATE_ACTED, f"cleared #{record.item}'s budget ({spent}) and nothing else"
    )


def _act_relaunch_item(
    record: ItemActionRecord,
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    *,
    now: datetime,
) -> ActionOutcome:
    # The same gates the tick's own selection applies, in its order, with
    # ONE deliberate bypass: the blocked-label skip. An item the decider
    # parked is exactly what this action exists to relaunch, so parking
    # alone must not stop it -- but every other reason the tick would not
    # launch (ineligible/held/done, unmet dependencies, the launch ceiling,
    # a run in flight, no stage left, rounds exhausted) still applies,
    # because those gates exist for reasons that do not vanish with the
    # label. `launch_stage` below re-enforces the writer/reader and disk /
    # credit limits per launch.
    if record.item not in labels.eligible_items(config, paths.root):
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} is not an eligible open item (no entry label, "
            f"held by a person, or done); this action relaunches queue items, "
            f"not retired or held ones",
        )
    parked = labels.blocked_stage(config, record.item, paths.root)
    if parked is None:
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} is not parked (no blocked label); this action "
            f"only relaunches an item the decider parked",
        )
    live = state.runs_for(record.item)
    if live:
        return ActionOutcome(
            STATE_DEFERRED,
            f"#{record.item} already has a tracked run "
            f"({', '.join(run.adw_id for run in live)}); nothing to launch",
        )
    budget = state.budgets.get(str(record.item))
    launches = budget.launches if budget is not None else 0
    if launches >= config.limits.launch_ceiling:
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} is at its launch ceiling "
            f"({launches}/{config.limits.launch_ceiling})",
        )
    waiting = artifacts.unmet_dependencies(config.repo, record.item, paths.root)
    if waiting:
        return ActionOutcome(
            STATE_DEFERRED,
            f"#{record.item} is waiting on "
            + ", ".join(f"#{n}" for n in waiting)
            + "; the dependency gate stays closed until that work lands",
        )
    pending, pr = unsatisfied_stages(config, paths, record.item)
    if not pending:
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} has no unsatisfied stage to launch; it belongs "
            f"to the decider, not to this action",
        )
    stage = pending[0]
    rounds = budget.rounds if budget is not None else 0
    if (
        rounds >= config.limits.max_rounds
        and stage.name != config.stages[0].name
    ):
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} has spent {rounds} of "
            f"{config.limits.max_rounds} rounds at {stage.name}; the rounds "
            f"gate stays closed and the decider owns what happens next",
        )
    report = TickReport(started_at=_stamp(now))
    # Reuse `launch_stage` itself, so the writer/reader limits, the disk and
    # credit gates, the launch spend and the tracking are the tick's own
    # rules -- this action bypasses only the selection skip that parking
    # caused. It removes no label, clears no counter, prunes nothing.
    launch_stage(config, paths, state, record.item, stage, pr, report)
    if report.launched:
        state.save_action(paths.state_file)
        return ActionOutcome(
            STATE_ACTED,
            f"launched {stage.name} for the parked #{record.item} "
            f"(at {parked}): {report.launched[0]}",
        )
    detail = "; ".join(report.skipped + report.errors)
    return ActionOutcome(
        STATE_DEFERRED,
        f"the launch gates refused {stage.name} for #{record.item}"
        + (f": {detail}" if detail else ""),
    )


def _act_page_again(
    record: ItemActionRecord,
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    *,
    now: datetime,
) -> ActionOutcome:
    # The page is the action; the PR number is decoration. A PR read that
    # fails must not stop a person being paged about an escalation nobody
    # answered.
    pr_number: int | None = None
    try:
        pr = artifacts.open_pull_request(config.repo, record.item, paths.root)
        pr_number = pr.number if pr is not None else None
    except GhError:
        pr_number = None
    result = escalate.escalate_item(
        config.repo,
        record.item,
        record.reason,
        pr_number=pr_number,
    )
    if result.delivered:
        return ActionOutcome(STATE_ACTED, f"paged again ({result.detail})")
    return ActionOutcome(
        STATE_FAILED, f"the page was not delivered: {result.detail}"
    )


def _act_hold_item(
    record: ItemActionRecord,
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    *,
    now: datetime,
) -> ActionOutcome:
    labels.mark_held(config, record.item, paths.root)
    return ActionOutcome(
        STATE_ACTED,
        f"added the {config.labels.hold!r} label to #{record.item} and nothing else",
    )


def _act_request_review(
    record: ItemActionRecord,
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState,
    *,
    now: datetime,
) -> ActionOutcome:
    pr = artifacts.open_pull_request(config.repo, record.item, paths.root)
    if pr is None:
        return ActionOutcome(
            STATE_FAILED,
            f"#{record.item} has no open pull request to request a review of",
        )
    # This IS the "ask again" action, so the per-head dedupe check is
    # deliberately not made: the whole point is a second request.
    artifacts.request_full_review(config.repo, pr.number, pr.revision, paths.root)
    return ActionOutcome(
        STATE_ACTED,
        f"requested a full review of {pr.revision[:8]} on PR #{pr.number}",
    )


_HANDLERS: dict[str, Callable[..., ActionOutcome]] = {
    "clear_budget": _act_clear_budget,
    "relaunch_item": _act_relaunch_item,
    "page_again": _act_page_again,
    "hold_item": _act_hold_item,
    "request_review": _act_request_review,
}


def dispatch(
    record: ItemActionRecord,
    config: FactoryConfig,
    paths: FactoryPaths,
    state: FactoryState | None,
    *,
    now: datetime | None = None,
) -> ActionOutcome:
    """Perform exactly this record's named action, once, and report it.

    `state` is the factory state the caller reloaded under the tick lock for
    the state-touching actions; the GitHub/Signal actions ignore it. Every
    infrastructure failure (`GhError`, `LabelError`, `LaunchError`, `OSError`)
    is caught per action and returned as the action's own outcome, so one
    bad request never aborts the status of the others.
    """
    handler = _HANDLERS[record.action]
    if record.action in STATE_LOCKED_ACTIONS and state is None:
        # The caller must reload factory state under the tick lock for the
        # state-touching actions; running them against nothing is a caller
        # bug, reported as the action's own failure rather than an AttributeError.
        return ActionOutcome(
            STATE_FAILED,
            f"{record.action} needs freshly loaded factory state and got none",
        )
    try:
        return handler(record, config, paths, state, now=now or _now())
    except (GhError, LabelError, LaunchError, OSError) as error:
        return ActionOutcome(
            STATE_FAILED,
            f"{record.action} on #{record.item} failed: "
            f"{type(error).__name__}: {error}",
        )


# ---------------------------------------------------------------- the CLI

def main(argv: list[str] | None = None) -> int:
    """The request command the diagnosis agent's action tools spawn.

    Reads the bound incident from the environment, pins the configured mode,
    creates the immutable record, and prints the rendered record -- which is
    exactly what the tool returns to the model and what the run log prints.
    """
    parser = argparse.ArgumentParser(
        prog="adws_factory.item_actions",
        description=(
            "Record one supervisor item action for the bound incident. The "
            "action mode comes from factory.yaml; a dry-run mode records the "
            "WOULD-ACT sentence and performs nothing."
        ),
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=tuple(_HANDLERS),
        help="which item action reasoning reached",
    )
    parser.add_argument(
        "--reason", required=True, help="the complete reason, verbatim"
    )
    args = parser.parse_args(argv)
    try:
        record = request_from_environment(args.action, args.reason)
    except ItemActionError as error:
        print(f"action request refused: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"action request failed: {error}", file=sys.stderr)
        return 1
    print(render(record))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via python -m
    sys.exit(main())
