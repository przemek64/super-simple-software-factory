"""Read-only access to what a run recorded about itself, and what that means.

Two jobs, kept together because the second is meaningless without the first:
reading the tracer database, and classifying a failure as *work* or
*infrastructure*.

The classification is mechanical on purpose. It reads the recorded phase error
and gate results, never an LLM's account of its own failure — a model asked why
it failed will rationalise, and the answer decides whether an item burns its
attempt budget. Getting this wrong in either direction is expensive: counting
infrastructure noise parks healthy items, counting nothing loops forever on a
broken environment.

Everything here opens the database read-only. The factory must never write to
the tracer; the tracer owns it and the UI polls it live.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__version__ = "0.1.3"

FailureClass = Literal["work", "infrastructure", "unknown"]

TERMINAL_STATUSES = {"success", "fail", "failed", "error", "killed", "cancelled"}
SUCCESS_STATUSES = {"success", "succeeded", "complete", "completed"}


# An AGENT reached out of bounds. The guard refusing it is the guard working,
# not the environment breaking, so retrying changes nothing until the agent
# does — that is a work failure and must spend the budget. Checked before the
# infrastructure patterns, which own the breaches nobody's agent caused (a
# snapshot budget blown, a tree edited underneath the run).
_AGENT_BREACH_PATTERNS = [
    r"outside this run's (worktree|roots)",
    r"may read but cannot write the canonical checkout",
    r"is (read-only|limited to|barred from) .*but modified",
]

# NOT in the list above, deliberately: "agent modified canonical or sibling
# checkout state". Every pattern there comes from a guard that watched a
# specific agent make a specific tool call, so it can name who. That one comes
# from a bulk before/after diff of the tree, which cannot -- it says "agent"
# only because an agent was running at the time. ANY change to the checkout
# during a run produces it, including a person's.
#
# It was in the list for one night. An operator switched branches to make an
# unrelated commit while a p3 was working; the guard saw adws_factory/* vanish
# and reappear, blamed the agent, and spent a work attempt on an item that had
# done nothing wrong. Unattributable means infrastructure: no budget spent, and
# the launch ceiling still stops a genuinely wedged item.

# Infrastructure: the environment failed, not the work. These do not spend the
# work budget. Each pattern below is here because it actually happened.
_INFRASTRUCTURE_PATTERNS = [
    r"permission (guard|snapshot)",         # guard refusal / snapshot bounds
    r"blocked: uses dynamic or opaque",     # the guard's bash refusal
    r"no space left on device",             # disk, not code
    r"errno 28",
    r"ModuleNotFoundError",                 # gate ran without the operator's env
    r"program not found",
    r"could not resolve|resolve argv",
    r"\b(timeout|timed out)\b",
    r"\b(overloaded|rate.?limit|429|503|502)\b",
    r"worktree .*(lock|locked|already exists)",
    r"disk floor",
    r"credit (gate|floor)",
    r"connection (refused|reset|aborted)",
]

# Work: the run reached the work and the work was not acceptable. These spend
# the attempt budget.
_WORK_PATTERNS = [
    r"acceptance gate",
    r"accepted_findings_touched",
    r"gate failed",
    r"tests? failed|suite (is )?red|\d+ failed",
    r"review .*(rejected|blocking)",
]

# Gates whose failure is always the work, regardless of message text.
_WORK_GATES = {"acceptance", "accepted_findings_touched", "tests", "quality"}


@dataclass
class PhaseRecord:
    name: str
    kind: str
    status: str
    error: str | None
    attempt: int
    seq: int

    @property
    def failed(self) -> bool:
        return self.status not in SUCCESS_STATUSES and self.status in TERMINAL_STATUSES


@dataclass
class GateRecord:
    gate: str
    passed: bool
    violations: list[str]


@dataclass
class ErrorEvent:
    """An error the run recorded as an EVENT rather than against a phase.

    The acceptance decision lives here and nowhere else. An ADW can finish with
    every phase `success` and every gate passed and still refuse to accept the
    result -- and when it does, it says so by emitting `not_accepted`, not by
    marking a phase failed. Five runs read as "failed with nothing recorded"
    for exactly that reason: the cause had been written down all along, in a
    table nothing here was reading.
    """

    name: str
    reason: str
    phase_id: str

    @property
    def phase(self) -> str:
        """The phase name out of `<adw_id>_<seq>_<name>`, else the raw id."""
        parts = self.phase_id.split("_", 2)
        return parts[2] if len(parts) == 3 and parts[2] else self.phase_id


@dataclass
class RunRecord:
    """One run, as the tracer recorded it."""

    adw_id: str
    adw_name: str
    status: str
    started_at: str | None
    ended_at: str | None
    phases: list[PhaseRecord]
    gates: list[GateRecord]
    errors: list[ErrorEvent] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        """Has the run stopped? An absent end time means it is still going."""
        return self.status in TERMINAL_STATUSES and self.ended_at is not None

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCESS_STATUSES

    @property
    def failed_phases(self) -> list[PhaseRecord]:
        return [phase for phase in self.phases if phase.failed]

    @property
    def failed_gates(self) -> list[GateRecord]:
        """Gates that were still failing when the run stopped.

        A gate the agent was corrected on and then PASSED is not a failure --
        it is the correction loop doing its job. `gate_results` keeps one row per
        attempt, so the same gate appears failed on attempt 1 and passed on
        attempt 2, and reading every failed row counted the first one forever.

        Cost of getting this wrong: run 0a7aaa37 fixed all six of its findings,
        passed this gate on its retry, died later for an unrelated reason -- and
        was charged a work attempt for the attempt-1 failure it had already
        corrected. Same principle as `terminal_errors`: recovered is not failed.
        """
        recovered = {gate.gate for gate in self.gates if gate.passed}
        return [gate for gate in self.gates
                if not gate.passed and gate.gate not in recovered]

    @property
    def terminal_errors(self) -> list[ErrorEvent]:
        """Error events that ENDED the run, not ones it recovered from.

        A `permission_breach_corrected` is the correction loop working: the call
        was refused, the agent was told why, and it carried on to finish. Reading
        those as causes is worse than reading nothing -- their text names the
        guard, so an infrastructure pattern matches and a genuine work failure
        gets graded infrastructure instead. Anything `*_corrected` is a recovery.
        """
        return [e for e in self.errors if not e.name.endswith("_corrected")]


def _connect(db: Path) -> sqlite3.Connection:
    """Open read-only, so a bug here can never corrupt the tracer's database."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def read_run(db: Path, adw_id: str) -> RunRecord | None:
    """Load one run and everything it recorded. None when the id is unknown."""
    if not db.is_file():
        return None

    with _connect(db) as connection:
        session = connection.execute(
            "SELECT adw_id, adw_name, status, started_at, ended_at "
            "FROM sessions WHERE adw_id = ?",
            (adw_id,),
        ).fetchone()
        if session is None:
            return None

        phases = [
            PhaseRecord(
                name=row["name"],
                kind=row["kind"] or "",
                status=row["status"] or "",
                error=row["error"],
                attempt=row["attempt"] or 0,
                seq=row["seq"] or 0,
            )
            for row in connection.execute(
                "SELECT name, kind, status, error, attempt, seq FROM phases "
                "WHERE adw_id = ? ORDER BY seq",
                (adw_id,),
            )
        ]

        gates = []
        for row in connection.execute(
            "SELECT gate, passed, violations_json FROM gate_results WHERE adw_id = ?",
            (adw_id,),
        ):
            try:
                violations = json.loads(row["violations_json"] or "[]")
            except json.JSONDecodeError:
                violations = []
            gates.append(
                GateRecord(
                    gate=row["gate"] or "",
                    passed=bool(row["passed"]),
                    violations=[str(item) for item in violations],
                )
            )

        # A separate try: older databases predate this table, and a missing
        # events table must not stop the factory reading everything else.
        errors = []
        try:
            for row in connection.execute(
                "SELECT name, payload_json, phase_id FROM events "
                "WHERE adw_id = ? AND type = 'error' ORDER BY rowid",
                (adw_id,),
            ):
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                errors.append(
                    ErrorEvent(
                        name=row["name"] or "",
                        reason=str(payload.get("reason") or payload.get("error") or ""),
                        phase_id=row["phase_id"] or "",
                    )
                )
        except sqlite3.Error:
            errors = []

    return RunRecord(
        adw_id=session["adw_id"],
        adw_name=session["adw_name"] or "",
        status=(session["status"] or "").lower(),
        started_at=session["started_at"],
        ended_at=session["ended_at"],
        phases=phases,
        gates=gates,
        errors=errors,
    )


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_failure(run: RunRecord) -> tuple[FailureClass, str]:
    """Decide whether a failed run spends the work budget, and say why.

    Order matters. A named work gate is decisive regardless of message text,
    because an infrastructure-sounding word in a violation string must not buy
    a free retry on genuinely unacceptable work. Infrastructure is checked next,
    so environment noise does not park a healthy item. Anything unrecognised is
    `unknown` and is treated as infrastructure by the caller — the safe
    direction, since the launch ceiling still terminates a wedged environment.
    """
    for gate in run.failed_gates:
        # Match the gate's BASE name. A gate factory stamps its arity into
        # __name__ -- "accepted_findings_touched(6 accepted)" -- so an exact-set
        # test never fired for the very gates this decisive rule exists to catch.
        # They landed on the substring in _WORK_PATTERNS instead, several checks
        # later, where an infrastructure-sounding word could beat them to it.
        if gate.gate.split("(", 1)[0].strip() in _WORK_GATES:
            return "work", f"gate {gate.gate} failed"

    haystack_parts: list[str] = []
    for phase in run.failed_phases:
        haystack_parts.append(f"{phase.name}: {phase.error or ''}")
    for gate in run.failed_gates:
        haystack_parts.append(f"{gate.gate}: {' '.join(gate.violations)}")
    # Error EVENTS, not just phase errors. A run can end with every phase
    # `success` and every gate passed and still be refused, and it records that
    # refusal here. Reading only phases and gates is why five failures showed up
    # as "nothing recorded" when the reason had been written down all along.
    for error in run.terminal_errors:
        haystack_parts.append(f"{error.name} at {error.phase}: {error.reason}")
    haystack = "\n".join(haystack_parts)

    # A review phase that ends `not_accepted` is the work being judged and
    # refused -- the reviewer withheld approval, which is precisely a work
    # failure. A `retest` one is NOT: "the suite never came back clean" covers
    # both a red suite and a suite that could not run at all, and those need
    # opposite answers. Left to the patterns below, and to `unknown` if they say
    # nothing, rather than guessed at.
    for error in run.terminal_errors:
        if error.name == "not_accepted" and "review" in error.phase:
            return "work", f"not accepted at {error.phase}: {error.reason}"

    if not haystack.strip():
        return "unknown", "run failed with nothing recorded"

    if _matches(_AGENT_BREACH_PATTERNS, haystack):
        return "work", _first_line(haystack)

    if _matches(_INFRASTRUCTURE_PATTERNS, haystack):
        return "infrastructure", _first_line(haystack)

    if _matches(_WORK_PATTERNS, haystack):
        return "work", _first_line(haystack)

    return "unknown", _first_line(haystack)


def _first_line(text: str, limit: int = 200) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:limit]
