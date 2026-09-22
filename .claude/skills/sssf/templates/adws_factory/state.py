"""Persisted orchestration state.

Lives at `{data_dir}/factory/state.json` — untracked, inside the permission
guard's exclusion (ADR-0004). Anyone who "tidies" this into the repository
reintroduces the failure it was placed here to avoid.

This file holds only what cannot be recomputed: which runs are in flight, and
what has been spent. Everything about *progress* is read from artifacts each
tick (ADR-0003) and is deliberately absent here, so a corrupted or deleted state
file costs attempt history, never correctness.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__version__ = "0.1.1"

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TrackedRun:
    """A run the factory launched and has not yet reaped."""

    adw_id: str
    item: int
    stage: str
    run_class: str
    pid: int | None = None
    launched_at: str = field(default_factory=_now)
    # The revision the stage was launched against, so a finished reader run can
    # be tested for staleness without re-deriving what it looked at.
    base_revision: str | None = None


@dataclass
class ItemBudget:
    """What one item has spent. Two counters, because failures are not alike."""

    work_attempts: int = 0
    launches: int = 0
    rounds: int = 0
    blocked_stage: str | None = None
    blocked_reason: str | None = None
    # Fingerprint of the last escalation actually sent for this item. An item
    # waiting on a person is re-judged on EVERY tick, so without this the same
    # unresolved blocker pages the operator once a minute all night. Storing the
    # reason rather than a flag means a genuinely new blocker still gets through.
    escalated_fingerprint: str | None = None

    def spend_launch(self) -> None:
        self.launches += 1

    def spend_work_attempt(self) -> None:
        self.work_attempts += 1

    def block(self, stage: str, reason: str) -> None:
        self.blocked_stage = stage
        self.blocked_reason = reason

    @property
    def is_blocked(self) -> bool:
        return self.blocked_stage is not None


@dataclass
class FactoryState:
    schema: int = SCHEMA_VERSION
    active_item: int | None = None
    tracked: list[TrackedRun] = field(default_factory=list)
    budgets: dict[str, ItemBudget] = field(default_factory=dict)
    last_tick_at: str | None = None

    # ---- budgets -------------------------------------------------------

    def budget(self, item: int) -> ItemBudget:
        return self.budgets.setdefault(str(item), ItemBudget())

    def clear_budget(self, item: int) -> None:
        """Reset an item's history. Only a person unblocking should cause this."""
        self.budgets.pop(str(item), None)

    # ---- tracked runs --------------------------------------------------

    def track(self, run: TrackedRun) -> None:
        self.tracked.append(run)

    def untrack(self, adw_id: str) -> None:
        self.tracked = [run for run in self.tracked if run.adw_id != adw_id]

    def runs_for(self, item: int) -> list[TrackedRun]:
        return [run for run in self.tracked if run.item == item]

    def writer_runs(self) -> list[TrackedRun]:
        return [run for run in self.tracked if run.run_class == "writer"]

    def reader_runs(self) -> list[TrackedRun]:
        return [run for run in self.tracked if run.run_class == "reader"]

    # ---- persistence ---------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "FactoryState":
        """Read state, tolerating absence. A missing file is a first run."""
        if not path.is_file():
            return cls()
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt state costs attempt history, not correctness: progress is
            # re-read from artifacts. Starting clean beats refusing to tick.
            return cls()

        if raw.get("schema") != SCHEMA_VERSION:
            return cls()

        state = cls(
            schema=raw.get("schema", SCHEMA_VERSION),
            active_item=raw.get("active_item"),
            last_tick_at=raw.get("last_tick_at"),
        )
        state.tracked = [TrackedRun(**entry) for entry in raw.get("tracked", [])]
        state.budgets = {
            key: ItemBudget(**value) for key, value in (raw.get("budgets") or {}).items()
        }
        return state

    def save(self, path: Path) -> None:
        """Write atomically — a tick may be killed mid-write at any moment.

        This is the tick's own save: it stamps `last_tick_at`, because the
        only legitimate writer of `state.json` is a tick and every existing
        caller is one.
        """
        self.last_tick_at = _now()
        self._write(path)

    def save_action(self, path: Path) -> None:
        """Write atomically WITHOUT stamping `last_tick_at`.

        The one deliberate writer besides a tick: a separately armed
        supervisor item action performing its single named mutation (for
        example clearing one item's budget). An item action is not a factory
        tick -- stamping the tick clock here would reset the supervisor's
        silence window and hide a dead loop behind a budget edit.
        """
        self._write(path)

    def _write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, sort_keys=True)

        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
