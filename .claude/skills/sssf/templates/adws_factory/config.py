"""The factory's own configuration: the stage sequence, the limits, the labels.

The stage sequence is data (GLOSSARY: Stage), so adding a review pass or
swapping a workflow is an edit to `factory.yaml`, not to `tick.py`. Defaults
below are the first deployment described in docs/orchestration.md; a
`adws_factory/factory.yaml` beside this module overrides any of them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from . import verify

__version__ = "0.1.1"

RunClass = Literal["writer", "reader"]


@dataclass(frozen=True)
class Stage:
    """One position in the sequence, bound to the workflow that advances it.

    `artifact` names the probe in `artifacts.py` that proves this stage
    happened. A stage with no durable artifact cannot be orchestrated
    (ADR-0003), so there is no "none" option here on purpose.
    """

    name: str
    workflow: str
    run_class: RunClass
    artifact: str
    arg: Literal["issue", "pr"]

    @property
    def is_writer(self) -> bool:
        return self.run_class == "writer"


@dataclass(frozen=True)
class Limits:
    """Everything that can stop a launch.

    The two budgets are separate because failures are not alike: counting
    infrastructure noise against the work budget parks healthy items, and
    counting nothing loops forever on a broken environment.
    """

    # Work-class failures only. Exhausting this parks the item blocked.
    work_attempts: int = 3
    # Every launch, whatever happened. A hard ceiling that infrastructure
    # failures cannot dodge, so a wedged environment still terminates.
    launch_ceiling: int = 12
    # Identical failures in a row before the item is parked. Two, because the
    # second identical failure is the one that proves the first was not a
    # blip: the environment or the work is wrong and another launch spends
    # the budget to learn nothing. Counts every failure class, unlike
    # `work_attempts` -- an item that dies the same way every time is the
    # case this exists for, and its classification is usually "unknown".
    repeat_failure_ceiling: int = 2
    # Review-then-fix stops when a round is clean, or after this many rounds
    # regardless. Without it the pair cycles forever: fixing changes the
    # revision, which staleness-checks the review that preceded it.
    #
    # Three, not two. Two spends one round on the initial review and its fix and
    # the second on verifying that fix, leaving nothing for a regression the
    # verification pass finds -- so an item one pass from done escalates instead,
    # which is the expensive kind of wrong: it wakes a person for work the factory
    # could have finished. A third round buys exactly one repair-and-reverify.
    # Convergence does not depend on the cap; a clean round still ends it early.
    max_rounds: int = 3

    # One item, and one writer run within it. Two writer runs on a branch is the
    # collision class that lost work on the predecessor project.
    max_active_items: int = 1
    max_writer_runs: int = 1
    max_reader_runs: int = 2

    # How long a requested external review is still considered "coming", so the
    # fix stage is launched to wait for it rather than skipped.
    #
    # MUST track the fix workflow's own await timeout (adw_coderabbit.py
    # --timeout, 900s). Larger and a second fix run starts waiting after the
    # first gave up, spinning until the launch ceiling. Equal, and at most ONE
    # run waits the request out: it exits at request+900, the next tick sees the
    # request expired, the stage reads done, and the decider -- which handles a
    # reviewer that answers without pinning to the head -- takes over.
    external_review_grace_seconds: float = 900.0

    # Below this, launch nothing. Worktrees accumulate and are not always
    # cleaned, and the safety snapshot copies the run store on every phase, so
    # the real cost of a run is much larger than its worktree.
    #
    # 7 GB is a deliberate compromise, not a safe number. One observed snapshot
    # was 5.5 GB on its own, so a single unlucky run can still cross this floor
    # from above and fill the disk mid-phase -- which surfaces as a spurious
    # test failure, not as a disk error. Raise it once the run store is capped
    # and the guard excludes data_dir; until then the floor is thinner than the
    # worst case it is guarding against.
    disk_floor_gb: float = 7.0
    disk_warn_gb: float = 12.0

    # Metered providers must be above this to launch, and the quota file must be
    # fresher than the staleness bound. A stale file previously read as "fine"
    # and launched a doomed run.
    credit_floor_pct: float = 10.0
    credit_max_age_minutes: int = 60


@dataclass(frozen=True)
class Labels:
    """Label vocabulary. Only the first three are inputs; the rest are output.

    Any label the factory writes must exist before it writes it — a missing
    label crashed an entire tick on the predecessor project, so `labels.py`
    ensures them up front rather than at the point of use.
    """

    entry: str = "approved-for-dev"
    hold: str = "factory-hold"
    target_prefix: str = "target: "

    stage_prefix: str = "stage: "      # cosmetic projection, recomputed each tick
    blocked_prefix: str = "blocked: "  # per stage; cleared only by a person
    done: str = "factory-done"

    def stage(self, name: str) -> str:
        return f"{self.stage_prefix}{name}"

    def blocked(self, name: str) -> str:
        return f"{self.blocked_prefix}{name}"

    def all_managed(self, stages: list[Stage]) -> list[str]:
        """Every label the factory may write, for the ensure-exists pass."""
        managed = [self.done, self.hold]
        for stage in stages:
            managed.append(self.stage(stage.name))
            managed.append(self.blocked(stage.name))
        return managed


@dataclass(frozen=True)
class SupervisorCapabilities:
    """Which supervisor signals exist and which may page a person.

    Configuration, not code: a signal being switched off is an operator's
    decision while diagnosing, the same as `verify_blocking_findings` is for
    the decider.

    The three infrastructure signals (`disk_below_floor`,
    `provider_credit_exhausted`, `run_vanished`) are machine conditions the
    poll reads directly -- no model, no decider -- so they are on by default:
    an operator reading "healthy, nothing to act on" while the disk is full
    learns nothing (issue #111).
    """

    loop_dead: bool = True
    item_stalled: bool = True
    escalation_unanswered: bool = True
    disk_below_floor: bool = True
    provider_credit_exhausted: bool = True
    run_vanished: bool = True
    page: bool = True


# The only two values an item-action flag may carry. Deliberately strings,
# never booleans: `may_clear_budget: true` is a plausible typo for a mode, and
# a bool cannot distinguish "armed" from "yes I meant the default".
ActionMode = Literal["dry_run", "armed"]
ACTION_MODES: tuple[str, ...] = ("dry_run", "armed")

# The five item actions and the flag that gates each one. One flag, one
# action: turning one on arms exactly that action and nothing else.
ITEM_ACTION_FLAGS: dict[str, str] = {
    "clear_budget": "may_clear_budget",
    "relaunch_item": "may_relaunch_item",
    "page_again": "may_page_again",
    "hold_item": "may_hold_item",
    "request_review": "may_request_review",
}


@dataclass(frozen=True)
class SupervisorItemActions:
    """The five supervisor item actions, each independently gated.

    Every flag defaults to `dry_run`: an action the diagnostician reaches is
    RECORDED, never performed, until a person edits `factory.yaml` to arm
    exactly the one they meant. An automated gate that enacted its own
    judgement once destroyed a pull request that had met most of its
    requirements, on five findings that were all false (ADR-0002 puts that
    authority with the decider); these flags exist so the supervisor cannot
    take it back by accident.

    Loop-state operations only. No action rewinds, resets, reverts, edits a
    branch or merges -- flag on or off.
    """

    may_clear_budget: ActionMode = "dry_run"
    may_relaunch_item: ActionMode = "dry_run"
    may_page_again: ActionMode = "dry_run"
    may_hold_item: ActionMode = "dry_run"
    may_request_review: ActionMode = "dry_run"


@dataclass(frozen=True)
class SupervisorRecovery:
    """Infrastructure authority only; disabled unless explicitly armed."""

    restart_driver: bool = False
    stop_hung_runs: bool = False
    reap_dead_runs: bool = False
    hung_min_age_seconds: float = 7200.0
    hung_idle_seconds: float = 3600.0
    restart_cooldown_seconds: float = 300.0
    max_restart_attempts: int = 3


@dataclass(frozen=True)
class SupervisorConfig:
    """The health poll's thresholds.

    Every number here reaches the poll and the scheduled-task installer from
    `factory.yaml`, so there is one place to change the schedule and one place
    to change the sensitivity. The defaults describe the first deployment:
    poll every minute, call silence after five minutes, and page only after
    two consecutive polls observe the same situation.

    `escalation_grace_seconds` is how long an unchanged delivered escalation
    may wait for a person before `escalation_unanswered` reports it. It is a
    different wait from the external-review grace, the loop silence window
    and `repeat_polls`, so it is deliberately its own number rather than a
    shared "grace" that would drift to serve three masters badly.

    `repeat_polls` is the per-signal consecutive-poll threshold, shared by
    every signal the poll runs (`loop_dead`, `item_stalled`,
    `escalation_unanswered`, `disk_below_floor`,
    `provider_credit_exhausted`, `run_vanished`): how many polls in a row
    must observe the same situation before a person is paged. Every candidate
    is visible in the status file on its FIRST poll; escalation and diagnosis
    become due only after this many identical consecutive polls. There is
    deliberately no second, per-signal count -- two thresholds for one page
    would drift apart.

    The default is two, not one: a fault must persist across more than one
    poll before it pages, so a momentary reading (a disk blip, a snapshot
    mid-rewrite) never wakes anybody. Configured values below two are
    rejected rather than honoured, for the same reason -- a threshold of one
    is exactly the transient page this number exists to prevent.
    `repeat_polls` applies independently once any signal first becomes a
    candidate: an escalation must first outlive its grace window, then
    survive this many consecutive polls before it is reportable.

    `diagnosis_grace_seconds` is the diagnosis agent's own launch cooldown:
    how long one signal/item's launch memory suppresses the next launch for
    that incident. It guards the startup race (a reservation saved, the
    child not yet observable) and the crash window after a reservation, so a
    new agent never stacks on a signal every tick while the first is still
    working. It is deliberately NOT the external-review grace, the
    unanswered-human grace or the loop silence window -- those time waits a
    person owes an answer; this one times one launch against its own
    successor. An unchanged incident stays diagnosed for good by its
    recorded signature, not by this clock.
    """

    poll_interval_minutes: int = 1
    silence_window_seconds: float = 300.0
    escalation_grace_seconds: float = 3600.0
    diagnosis_grace_seconds: float = 1800.0
    repeat_polls: int = 2
    capabilities: SupervisorCapabilities = field(default_factory=SupervisorCapabilities)
    item_actions: SupervisorItemActions = field(default_factory=SupervisorItemActions)
    recovery: SupervisorRecovery = field(default_factory=SupervisorRecovery)


DEFAULT_STAGES: list[Stage] = [
    Stage("p1", "adws/adw_simple_sdlc.py", "writer", "open_pull_request", "issue"),
    Stage("p2", "adws/adw_pr_review_2axis_m3.py", "reader", "pinned_review", "pr"),
    Stage("p3", "adws/adw_coderabbit.py", "writer", "ledger_entry", "pr"),
]


@dataclass
class FactoryConfig:
    stages: list[Stage] = field(default_factory=lambda: list(DEFAULT_STAGES))
    limits: Limits = field(default_factory=Limits)
    labels: Labels = field(default_factory=Labels)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    repo: str = "przemek64/labeltool-test2"
    sssf_config: str = "adws/adw_sssf_config/sssf.config.yaml"

    # The decider's own seat. It rules on whether a blocking finding is true of
    # the code, which decides whether an unattended merge happens, so it is a
    # reviewer-grade model rather than the cheapest one and it is credit-gated
    # like any other metered work (ADR-0002).
    verification_model: str = verify.DEFAULT_MODEL
    # An escape hatch, not a tuning knob. Off, every blocking finding escalates
    # to a person exactly as it did before the agent existed -- which is the
    # fail-safe direction, and the right setting while diagnosing a bad ruling.
    verify_blocking_findings: bool = True

    def stage_by_name(self, name: str) -> Stage | None:
        return next((s for s in self.stages if s.name == name), None)

    def next_stage(self, after: str | None) -> Stage | None:
        """The stage following `after`, or the first stage when `after` is None."""
        if after is None:
            return self.stages[0] if self.stages else None
        for index, stage in enumerate(self.stages):
            if stage.name == after:
                return self.stages[index + 1] if index + 1 < len(self.stages) else None
        return None

    @classmethod
    def load(cls, path: Path | None = None) -> "FactoryConfig":
        """Load `factory.yaml` if present, else the first-deployment defaults."""
        path = path or (Path(__file__).parent / "factory.yaml")
        if not path.is_file():
            return cls()

        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls()
        if raw.get("repo"):
            config.repo = raw["repo"]
        if raw.get("sssf_config"):
            config.sssf_config = raw["sssf_config"]
        if raw.get("verification_model"):
            config.verification_model = raw["verification_model"]
        if "verify_blocking_findings" in raw:
            config.verify_blocking_findings = bool(raw["verify_blocking_findings"])
        if raw.get("stages"):
            config.stages = [Stage(**entry) for entry in raw["stages"]]
        if raw.get("limits"):
            config.limits = Limits(**raw["limits"])
        if raw.get("labels"):
            config.labels = Labels(**raw["labels"])
        if "supervisor" in raw:
            config.supervisor = _parse_supervisor(raw["supervisor"], path)
        return config


def _parse_supervisor(raw: Any, path: Path) -> SupervisorConfig:
    """Read the `supervisor` section, refusing values that would misbehave.

    A nonpositive interval or silence window does not fail loudly on its own:
    it makes the poll page on every run, or never. Naming the field and the
    file here is what turns a silent misconfiguration into a one-line fix.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: supervisor must be a mapping, not {type(raw).__name__}")

    def positive_int(key: str) -> int:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{path}: supervisor.{key} must be a whole number, got {value!r}"
            )
        if value <= 0:
            raise ValueError(
                f"{path}: supervisor.{key} must be positive, got {value!r}"
            )
        return value

    def repeat_polls_int(key: str) -> int:
        """`repeat_polls` is judged harder than merely positive: at least two.

        A threshold of one would let a single transient poll page a person --
        a disk reading taken mid-rewrite, a quota snapshot half-written. The
        acceptance criterion is explicit that a fault must persist across
        more than one poll before it escalates, so anything below two is a
        misconfiguration to refuse by name, not a sensitivity to honour.
        """
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{path}: supervisor.{key} must be a whole number, got {value!r}"
            )
        if value < 2:
            raise ValueError(
                f"{path}: supervisor.{key} must be at least two, got {value!r}; "
                f"a fault must persist across more than one poll before it "
                f"pages a person"
            )
        return value

    def positive_number(key: str) -> float:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"{path}: supervisor.{key} must be a number, got {value!r}"
            )
        if value <= 0:
            raise ValueError(
                f"{path}: supervisor.{key} must be positive, got {value!r}"
            )
        number = float(value)
        # `.inf` and `.nan` are both writable in YAML and both pass a `> 0`
        # test -- inf trivially, nan because every comparison against it is
        # false. Either one silently disables the signal it configures: with an
        # infinite silence window the age check is always satisfied so
        # `loop_dead` can never fire, and with nan it is never satisfied so the
        # loop can be called dead while a driver holds it.
        if not math.isfinite(number):
            raise ValueError(
                f"{path}: supervisor.{key} must be finite, got {value!r}"
            )
        return number

    # A partial section keeps the defaults for whatever it does not name; only
    # a value that IS present is judged. A key this parser does not know -- a
    # misspelling such as `silence_window_second` -- would otherwise keep its
    # default in force unjudged, which is the silent misconfiguration this
    # function exists to catch.
    known = {
        "poll_interval_minutes",
        "silence_window_seconds",
        "escalation_grace_seconds",
        "diagnosis_grace_seconds",
        "repeat_polls",
        "capabilities",
        "item_actions",
        "recovery",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"{path}: supervisor has unknown key(s): {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    if "poll_interval_minutes" in raw:
        kwargs["poll_interval_minutes"] = positive_int("poll_interval_minutes")
    if "silence_window_seconds" in raw:
        kwargs["silence_window_seconds"] = positive_number("silence_window_seconds")
    if "escalation_grace_seconds" in raw:
        kwargs["escalation_grace_seconds"] = positive_number(
            "escalation_grace_seconds"
        )
    if "diagnosis_grace_seconds" in raw:
        # Same strict positive-finite rule as the other windows: an infinite
        # cooldown means a changed incident is never diagnosed again, and a
        # nan one means every poll launches a new agent on one signal.
        kwargs["diagnosis_grace_seconds"] = positive_number(
            "diagnosis_grace_seconds"
        )
    if "repeat_polls" in raw:
        kwargs["repeat_polls"] = repeat_polls_int("repeat_polls")
    supervisor = SupervisorConfig(**kwargs)

    # Validated BEFORE defaulting. `raw.get(...) or {}` turned a present
    # falsy value (`false`, `0`, `[]`) into the empty default, so the typo
    # sailed through unvalidated -- exactly the class of mistake this
    # function exists to catch. An absent key is the only thing that means
    # "use the defaults".
    caps: Any = raw["capabilities"] if "capabilities" in raw else {}
    if not isinstance(caps, dict):
        raise ValueError(
            f"{path}: supervisor.capabilities must be a mapping, not {caps!r}"
        )
    unknown_caps = sorted(
        set(caps)
        - {
            "loop_dead",
            "item_stalled",
            "escalation_unanswered",
            "disk_below_floor",
            "provider_credit_exhausted",
            "run_vanished",
            "page",
        }
    )
    if unknown_caps:
        raise ValueError(
            f"{path}: supervisor.capabilities has unknown key(s): {', '.join(unknown_caps)}"
        )
    flags = {}
    for key in (
        "loop_dead",
        "item_stalled",
        "escalation_unanswered",
        "disk_below_floor",
        "provider_credit_exhausted",
        "run_vanished",
        "page",
    ):
        if key in caps:
            value = caps[key]
            if not isinstance(value, bool):
                raise ValueError(
                    f"{path}: supervisor.capabilities.{key} must be true or false,"
                    f" got {value!r}"
                )
            flags[key] = value
    item_actions = _parse_item_actions(
        raw["item_actions"] if "item_actions" in raw else {}, path
    )
    return replace(
        supervisor,
        capabilities=SupervisorCapabilities(**flags) if flags else SupervisorCapabilities(),
        item_actions=item_actions,
        recovery=_parse_recovery(raw.get("recovery", {}), path),
    )


def _parse_recovery(raw: Any, path: Path) -> SupervisorRecovery:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: supervisor.recovery must be a mapping")
    defaults = SupervisorRecovery()
    unknown = set(raw) - set(defaults.__dataclass_fields__)
    if unknown:
        raise ValueError(f"{path}: supervisor.recovery unknown keys: {sorted(unknown)}")
    for key, value in raw.items():
        default = getattr(defaults, key)
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = type(value) is int and value > 0
        else:
            valid = (type(value) in (int, float) and math.isfinite(value)
                     and value > 0)
        if not valid:
            raise ValueError(f"{path}: invalid supervisor.recovery.{key}: {value!r}")
    return SupervisorRecovery(**raw)


def _parse_item_actions(raw: Any, path: Path) -> SupervisorItemActions:
    """Read `supervisor.item_actions`: five independent dry_run/armed flags.

    Strict by name, exactly like the capabilities beside it. A misspelled
    flag (`may_clear_buget`), an unknown extra, or a mode that is not the
    literal `dry_run` or `armed` fails the load naming the key -- a boolean
    or a near-miss mode must never coerce through `bool()` into an armed
    action, and must never silently keep the default either. A partial
    mapping keeps `dry_run` for whatever it does not name; only an ABSENT
    section means "all five default".
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: supervisor.item_actions must be a mapping, not {raw!r}"
        )
    unknown = sorted(set(raw) - set(ITEM_ACTION_FLAGS.values()))
    if unknown:
        raise ValueError(
            f"{path}: supervisor.item_actions has unknown key(s): {', '.join(unknown)}"
        )
    modes: dict[str, ActionMode] = {}
    for action, flag in ITEM_ACTION_FLAGS.items():
        if flag not in raw:
            continue
        value = raw[flag]
        if isinstance(value, bool) or not isinstance(value, str) or value not in ACTION_MODES:
            raise ValueError(
                f"{path}: supervisor.item_actions.{flag} must be dry_run or armed,"
                f" got {value!r}"
            )
        modes[flag] = value
    return SupervisorItemActions(**modes)
