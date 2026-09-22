"""Everything that can refuse a launch.

Two gates, both fail-closed. A gate that cannot read its input blocks rather
than assumes the best: on the predecessor project a credit gate that treated
unreadable data as "fine" launched a doomed run against an exhausted provider,
and a disk that filled mid-phase surfaced as a spurious test failure rather
than as a disk error.

Neither gate spends the work budget when it refuses -- being unable to start is
not the work being wrong (see docs/orchestration.md, two-budget table).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import FactoryConfig, Stage

__version__ = "0.1.1"

CREDITS_FILE = Path(r"C:/dev/projects/grey-factory/credits.json")

# Model prefix -> the provider key used in credits.json. A model whose prefix is
# absent here is unmetered and not gated; MiniMax is deliberately in that set --
# it is cheap enough that gating on it would stall work for no saving.
_PROVIDER_BY_PREFIX = {
    "openai-codex": "codex",
    "openai": "codex",
    "zai": "zai",
    "anthropic": "claude",
    "claude": "claude",
    "kimi": "kimi",
    "kimi-coding": "kimi",
}
_UNMETERED_PREFIXES = {"minimax"}


@dataclass(frozen=True)
class GateResult:
    """Why a launch may or may not proceed. `detail` is written to the log."""

    passed: bool
    gate: str
    detail: str

    def __bool__(self) -> bool:
        return self.passed


# ------------------------------------------------------------ structured readings
# Read-only companions to the gates above: the same facts, as data rather than
# as the sentence the launch gate prints. The supervisor reports infrastructure
# faults from these, so it never parses `GateResult.detail` prose and never
# re-implements a threshold the gate already owns (issue #111).


@dataclass(frozen=True)
class DiskReading:
    """One free-space reading: the path, the numbers, or why they are absent.

    `free_gb` is None only when the capacity itself could not be read; that is
    a read error, never "plenty of space". The boundary is the gate's own:
    only strictly below the floor is below it, equality passes.
    """

    path: Path
    floor_gb: float
    free_gb: float | None = None
    error: str | None = None

    @property
    def below_floor(self) -> bool:
        return self.free_gb is not None and self.free_gb < self.floor_gb

    @property
    def shortfall_gb(self) -> float | None:
        """How far below the floor, or None when at/above it or unreadable."""
        if not self.below_floor:
            return None
        return self.floor_gb - self.free_gb


def read_disk(config: FactoryConfig, path: Path) -> DiskReading:
    """One structured capacity reading. Read-only; never raises."""
    try:
        free_gb = shutil.disk_usage(path).free / (1024**3)
    except OSError as error:
        return DiskReading(
            path=path,
            floor_gb=config.limits.disk_floor_gb,
            error=f"cannot read free space at {path}: {error}",
        )
    return DiskReading(
        path=path, floor_gb=config.limits.disk_floor_gb, free_gb=free_gb
    )


@dataclass(frozen=True)
class ProviderReading:
    """One required provider, from one credits snapshot.

    `exhausted_windows` names the present usable windows at or below the
    floor -- the narrow, page-worthy fact. Everything else (`problem`) is a
    read or configuration failure that must be REPORTED, never relabelled as
    exhausted credit: a stale snapshot is not a drained account.

    `problem` is one of "absent" (not in the snapshot), "status" (the monitor
    itself reported the provider stale or with no data),
    "no-usable-window" (present but carrying neither window), or
    "malformed" (the entry, or a percentage inside it, is not the shape the
    reader can trust; `problem_detail` says what was seen).
    """

    provider: str
    floor_pct: float
    pct_5h: float | None = None
    pct_weekly: float | None = None
    status: str | None = None
    problem: str | None = None
    problem_detail: str = ""
    exhausted_windows: tuple[str, ...] = ()

    @property
    def exhausted(self) -> bool:
        return bool(self.exhausted_windows)

    @property
    def detail(self) -> str:
        """The exact sentence the launch gate has always failed on."""
        if self.problem == "absent":
            return f"{self.provider} absent from credits"
        if self.problem == "status":
            return f"{self.provider} is {self.status}"
        if self.problem == "no-usable-window":
            return f"{self.provider} reported no usable window"
        if self.problem == "malformed":
            return f"{self.provider} {self.problem_detail}"
        if self.exhausted_windows:
            window = self.exhausted_windows[0]
            pct = self.pct_5h if window == "5h" else self.pct_weekly
            return f"{self.provider} {window} at {pct}%"
        return f"{self.provider} ok"


@dataclass(frozen=True)
class CreditSnapshot:
    """One credits.json read: every provider asked about, or why none can be.

    `error` carries the snapshot-level fail-closed facts (missing file,
    unreadable bytes, missing/unparseable/stale `updated_at`); `providers` is
    empty unless the snapshot itself could be trusted.
    """

    providers: tuple[ProviderReading, ...]
    age_minutes: float | None = None
    error: str | None = None

    @property
    def trusted(self) -> bool:
        return self.error is None


def inspect_providers(
    config: FactoryConfig,
    providers: set[str],
    *,
    now: datetime | None = None,
    credits_file: Path = CREDITS_FILE,
) -> CreditSnapshot:
    """Inspect EVERY named provider on one snapshot, structured.

    The gate answers its question with the first refusal; a supervisor has to
    answer "which providers are drained", so this walks the whole set in
    deterministic (sorted) order and reports each one's own facts. `now` is
    injectable so a poll's snapshot-age judgement is deterministic in tests;
    the freshness bound and every fail-closed rule are the gate's own.

    Structurally malformed input fails closed as a read error rather than
    raising: the document must be a mapping, `providers` must be a mapping,
    each entry must be a mapping, `updated_at` must be a string ISO stamp,
    and a present percentage must be a number. A snapshot the reader cannot
    trust is reported as exactly that -- never as "plenty of credit" and
    never as a drained account.
    """
    now = now or datetime.now(timezone.utc)
    if not credits_file.is_file():
        return CreditSnapshot((), error=f"no credits file at {credits_file}")
    try:
        data = json.loads(credits_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        return CreditSnapshot((), error=f"cannot read credits: {error}")
    if not isinstance(data, dict):
        return CreditSnapshot(
            (), error=f"credits are not a mapping: {type(data).__name__}"
        )

    updated_at = data.get("updated_at")
    if not updated_at:
        return CreditSnapshot((), error="credits file carries no updated_at")
    if not isinstance(updated_at, str):
        return CreditSnapshot((), error=f"unparseable updated_at: {updated_at!r}")
    try:
        stamp = datetime.fromisoformat(updated_at)
    except ValueError:
        return CreditSnapshot((), error=f"unparseable updated_at: {updated_at!r}")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_minutes = (now - stamp).total_seconds() / 60
    # One minute of skew is tolerated (monitor and reader clocks differ by a
    # little); a stamp materially AHEAD of `now` is not fresh forever -- a
    # future-dated snapshot is data of unknown age, same fail-closed rule.
    if age_minutes > config.limits.credit_max_age_minutes or age_minutes < -1.0:
        return CreditSnapshot(
            (),
            age_minutes=age_minutes,
            error=(
                f"credits are {age_minutes:.0f} min old, outside the "
                f"{config.limits.credit_max_age_minutes} min bound"
            ),
        )

    reported = data.get("providers") or {}
    if not isinstance(reported, dict):
        return CreditSnapshot(
            (),
            age_minutes=age_minutes,
            error=f"credits providers are not a mapping: {type(reported).__name__}",
        )
    floor = config.limits.credit_floor_pct
    readings: list[ProviderReading] = []
    for provider in sorted(providers):
        info = reported.get(provider)
        if info is None:
            readings.append(
                ProviderReading(provider=provider, floor_pct=floor, problem="absent")
            )
            continue
        if not isinstance(info, dict):
            readings.append(
                ProviderReading(
                    provider=provider,
                    floor_pct=floor,
                    problem="malformed",
                    problem_detail=f"entry is a {type(info).__name__}, not a mapping",
                )
            )
            continue
        status = info.get("status")
        pct_5h = info.get("pct_remaining_5h")
        pct_weekly = info.get("pct_remaining_weekly")
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, (int, float)))
            for value in (pct_5h, pct_weekly)
        ):
            readings.append(
                ProviderReading(
                    provider=provider,
                    floor_pct=floor,
                    status=status if isinstance(status, str) else None,
                    problem="malformed",
                    problem_detail=(
                        "reported a percentage that is not a number "
                        f"(5h {pct_5h!r}, weekly {pct_weekly!r})"
                    ),
                )
            )
            continue
        if status in ("stale", "no-data"):
            readings.append(
                ProviderReading(
                    provider=provider,
                    floor_pct=floor,
                    pct_5h=pct_5h,
                    pct_weekly=pct_weekly,
                    status=status,
                    problem="status",
                )
            )
            continue
        if pct_5h is None and pct_weekly is None:
            readings.append(
                ProviderReading(
                    provider=provider,
                    floor_pct=floor,
                    status=status if isinstance(status, str) else None,
                    problem="no-usable-window",
                )
            )
            continue
        # A missing window is not gated on -- Codex exposes only a weekly one
        # -- but a present window at or below the floor is exhausted credit.
        exhausted: list[str] = []
        if pct_5h is not None and pct_5h <= floor:
            exhausted.append("5h")
        if pct_weekly is not None and pct_weekly <= floor:
            exhausted.append("weekly")
        readings.append(
            ProviderReading(
                provider=provider,
                floor_pct=floor,
                pct_5h=pct_5h,
                pct_weekly=pct_weekly,
                status=status if isinstance(status, str) else None,
                exhausted_windows=tuple(exhausted),
            )
        )
    return CreditSnapshot(tuple(readings), age_minutes=age_minutes)


@dataclass(frozen=True)
class StageCreditReading:
    """Everything a stage's credit situation is, without a sentence to parse.

    `derivation_error` is the roster/workflow/scan half of `check_credits`:
    a missing roster, a missing workflow, an unparseable config, or a workflow
    no roster agent is named in. Those block the launch gate and are reported
    to the supervisor as configuration faults -- they are not credit facts.

    `snapshot` is None when there was nothing to inspect: either the
    derivation failed, or the stage genuinely uses no metered provider (which
    is a pass, not an error). `providers` is the sorted metered set.
    """

    stage: str
    workflow: str
    seats: frozenset[str]
    providers: tuple[str, ...]
    derivation_error: str | None
    snapshot: CreditSnapshot | None

    @property
    def exhausted(self) -> tuple[ProviderReading, ...]:
        """Only the genuinely drained providers, in sorted order."""
        if self.snapshot is None or not self.snapshot.trusted:
            return ()
        return tuple(
            reading for reading in self.snapshot.providers if reading.exhausted
        )


def inspect_credits(
    config: FactoryConfig,
    stage: Stage,
    root: Path,
    *,
    now: datetime | None = None,
    credits_file: Path = CREDITS_FILE,
) -> StageCreditReading:
    """Derive a stage's providers and inspect them, all structured.

    The same derivation and fail-closed rules `check_credits` applies, kept in
    one place so the tick's gate and the supervisor's fault report can never
    drift: same roster scan, same freshness bound, same window semantics.
    Read-only; never launches, never mutates a budget.
    """
    sssf_config = root / config.sssf_config
    if not sssf_config.is_file():
        return StageCreditReading(
            stage=stage.name,
            workflow=stage.workflow,
            seats=frozenset(),
            providers=(),
            derivation_error=f"roster missing at {sssf_config}",
            snapshot=None,
        )

    workflow_file = root / stage.workflow
    if not workflow_file.is_file():
        # agents_for_workflow returns an empty set for a missing file, which
        # would read below as "nothing metered" and pass. A stage pointing at a
        # workflow that is not there is a configuration fault, not a free launch.
        return StageCreditReading(
            stage=stage.name,
            workflow=stage.workflow,
            seats=frozenset(),
            providers=(),
            derivation_error=f"workflow missing at {workflow_file}",
            snapshot=None,
        )

    try:
        seats, metered = seats_for_stage(root, stage, sssf_config)
    except (
        OSError,
        yaml.YAMLError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        # A roster whose STRUCTURE cannot be walked (agents that are not a
        # list of mappings, a model that is not a string) is a configuration
        # fault the same way a missing roster is: fail closed, name it, never
        # fall through to "nothing metered here".
        return StageCreditReading(
            stage=stage.name,
            workflow=stage.workflow,
            seats=frozenset(),
            providers=(),
            derivation_error=f"cannot derive providers: {error}",
            snapshot=None,
        )

    if not seats:
        # The scan matched no roster agent in a workflow that certainly runs
        # some. `agents_for_workflow` is written to rot LOUDLY -- rename a seat
        # and it finds nothing -- and its docstring promises the caller blocks
        # on that.
        return StageCreditReading(
            stage=stage.name,
            workflow=stage.workflow,
            seats=frozenset(),
            providers=(),
            derivation_error=(
                f"no roster agent named in {stage.workflow}; "
                f"a renamed seat or the wrong roster at {sssf_config}"
            ),
            snapshot=None,
        )

    if not metered:
        # Genuinely unmetered: seats were found, none of them cost anything.
        return StageCreditReading(
            stage=stage.name,
            workflow=stage.workflow,
            seats=frozenset(seats),
            providers=(),
            derivation_error=None,
            snapshot=None,
        )

    snapshot = inspect_providers(config, metered, now=now, credits_file=credits_file)
    return StageCreditReading(
        stage=stage.name,
        workflow=stage.workflow,
        seats=frozenset(seats),
        providers=tuple(sorted(metered)),
        derivation_error=None,
        snapshot=snapshot,
    )


def _snapshot_gate_detail(snapshot: CreditSnapshot) -> GateResult:
    """Collapse one trusted-or-not snapshot into the gate's first refusal."""
    if snapshot.error is not None:
        return GateResult(False, "credits", snapshot.error)
    for reading in snapshot.providers:
        if reading.problem is not None or reading.exhausted:
            return GateResult(False, "credits", reading.detail)
    age_note = (
        f"{snapshot.age_minutes:.0f} min old"
        if snapshot.age_minutes is not None
        else ""
    )
    names = ", ".join(reading.provider for reading in snapshot.providers)
    return GateResult(True, "credits", f"{names} ok ({age_note})")


def check_disk(config: FactoryConfig, path: Path) -> GateResult:
    """Refuse to launch below the floor.

    The floor is not just about worktrees. The permission guard snapshots the
    whole checkout around every agent phase, and that snapshot includes the run
    store -- one observed snapshot was 5.5 GB. So the disk cost of a run is the
    run store times the number of phases, not the size of a worktree.

    Formats `read_disk`'s structured reading back into the gate's sentence;
    the boundary lives in one place (`DiskReading.below_floor`) so the launch
    gate and the supervisor can never disagree about what "below" means.
    """
    reading = read_disk(config, path)
    if reading.error is not None:
        return GateResult(False, "disk", reading.error)
    if reading.below_floor:
        return GateResult(
            False,
            "disk",
            f"{reading.free_gb:.2f} GB free is below the "
            f"{reading.floor_gb:.1f} GB floor",
        )

    warn = config.limits.disk_warn_gb
    note = f"{reading.free_gb:.2f} GB free"
    if reading.free_gb < warn:
        note += f" (below the {warn:.1f} GB warning line)"
    return GateResult(True, "disk", note)


def agents_for_workflow(root: Path, workflow: str, roster: set[str]) -> set[str]:
    """Which roster agents a workflow actually names.

    Read from the workflow source rather than a per-stage list in configuration.
    A hand-maintained list silently rots when a seat is swapped; this goes stale
    loudly instead -- rename an agent and the scan returns nothing, which the
    caller treats as unreadable and blocks on.
    """
    source_file = root / workflow
    if not source_file.is_file():
        return set()
    source = source_file.read_text(encoding="utf-8", errors="replace")
    found = set()
    for name in roster:
        if re.search(rf'["\']{re.escape(name)}["\']', source):
            found.add(name)
    return found


def seats_for_stage(root: Path, stage: Stage, sssf_config: Path) -> tuple[set[str], set[str]]:
    """The agents a stage's run will use, and the metered providers they draw on.

    Both halves are returned because the caller cannot fail correctly on the
    second alone. No metered provider has two causes -- a workflow that
    genuinely runs only unmetered models, and a workflow whose seats the scan
    could not find at all -- and they need opposite answers. The agent set
    separates them: empty means the scan found nothing to price.
    """
    raw = yaml.safe_load(sssf_config.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    default_model = defaults.get("model", "")
    agents = {entry["name"]: entry for entry in (raw.get("agents") or []) if "name" in entry}

    used = agents_for_workflow(root, stage.workflow, set(agents))
    providers: set[str] = set()
    for name in used:
        model = agents[name].get("model") or default_model
        prefix = str(model).split("/", 1)[0].lower()
        if prefix in _UNMETERED_PREFIXES:
            continue
        provider = _PROVIDER_BY_PREFIX.get(prefix)
        if provider:
            providers.add(provider)
    return used, providers


def providers_for_stage(root: Path, stage: Stage, sssf_config: Path) -> set[str]:
    """The metered providers a stage's run will draw on. See `seats_for_stage`."""
    return seats_for_stage(root, stage, sssf_config)[1]


def check_credits(
    config: FactoryConfig,
    stage: Stage,
    root: Path,
    credits_file: Path = CREDITS_FILE,
) -> GateResult:
    """Require every metered provider the stage will use to be above threshold.

    Collapses `inspect_credits`'s structured stage reading back into the
    gate's one answer, so the tick's gate and the supervisor's fault report
    share one derivation and one snapshot reader: the derivation faults
    refuse here (a missing roster, a missing workflow, a roster that cannot
    be walked, a workflow in which no roster agent is named at all), an
    unmetered stage passes, and everything else is the snapshot's own
    fail-closed reading.
    """
    reading = inspect_credits(config, stage, root, credits_file=credits_file)
    if reading.derivation_error is not None:
        return GateResult(False, "credits", reading.derivation_error)
    if not reading.providers:
        # Genuinely unmetered: seats were found, none of them cost anything.
        return GateResult(
            True,
            "credits",
            f"no metered provider among {len(reading.seats)} seat(s) "
            f"in {reading.workflow}",
        )
    return _snapshot_gate_detail(reading.snapshot)


def provider_for_model(model: str) -> str | None:
    """The credits.json key a `provider/id` model string draws on, or None.

    None means unmetered -- either a deliberately unmetered prefix or one this
    table does not know. Callers that gate on this must decide which of those
    they are willing to launch on; `check_model_credits` treats both as free,
    matching `seats_for_stage`, which has always skipped unknown prefixes.
    """
    prefix = str(model or "").split("/", 1)[0].lower()
    if prefix in _UNMETERED_PREFIXES:
        return None
    return _PROVIDER_BY_PREFIX.get(prefix)


def check_model_credits(
    config: FactoryConfig,
    model: str,
    credits_file: Path = CREDITS_FILE,
) -> GateResult:
    """The credit gate for one model named directly, not derived from a workflow.

    The decider's own verification agent spends metered quota without launching
    a stage, and ADR-0002 puts it under the same gate as the work it supervises:
    "The decider spends metered model quota of its own, so it is subject to the
    same credit gate as the workflows it supervises." `check_credits` cannot
    serve it -- that one starts from a workflow file and a roster, and there is
    neither here.
    """
    if not model:
        # An empty model is a configuration fault, not a free call. Same
        # reasoning as the missing-workflow branch above.
        return GateResult(False, "credits", "no verification model configured")

    provider = provider_for_model(model)
    if provider is None:
        return GateResult(True, "credits", f"{model} is unmetered")

    return check_providers(config, {provider}, credits_file)


def check_providers(
    config: FactoryConfig,
    providers: set[str],
    credits_file: Path = CREDITS_FILE,
) -> GateResult:
    """Every named provider must be above the floor, on a snapshot fresh enough to trust.

    Fail-closed in four distinct ways, each paying for an observed failure: an
    unreadable file, a stale snapshot, a provider the monitor could not read,
    and a provider present but reporting no usable window. The third is the one
    that actually bit -- a provider whose reader was failing carried no
    percentage fields at all and sailed through a naive comparison.

    Collapses `inspect_providers`'s structured reading back into the gate's
    one answer, so the launch gate and the supervisor's fault report read
    the same snapshot through the same rules.
    """
    return _snapshot_gate_detail(
        inspect_providers(config, providers, credits_file=credits_file)
    )


def check_all(config: FactoryConfig, stage: Stage, root: Path) -> list[GateResult]:
    """Every launch gate, in order. Callers launch only if all passed."""
    return [check_disk(config, root), check_credits(config, stage, root)]
