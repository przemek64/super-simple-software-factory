"""Where the factory reads and writes.

The split matters and is not cosmetic (ADR-0004): `adws_factory/` is tracked
source in the monitored tree, and every mutable byte the factory produces lands
under `{data_dir}/factory/`, inside the directory the permission guard already
excludes. Putting state anywhere else means a live agent phase sees an
unexplained write, attributes it to the agent, restores the tree and fails the
phase. That mechanism has already eaten hand-written work twice.

`data_dir` is read from the SSSF config rather than hardcoded, because the guard
excludes exactly `defaults.data_dir` — a second source of truth here would drift
from the exclusion and reintroduce the failure silently.
"""

from __future__ import annotations

from pathlib import Path

import yaml

__version__ = "0.1.0"

DEFAULT_CONFIG_REL = "adws/adw_sssf_config/sssf.config.yaml"


def repo_root(start: Path | None = None) -> Path:
    """The checkout root, found by walking up to the directory holding `adws/`.

    Not `git rev-parse --show-toplevel`: the factory also runs from inside
    worktrees, where that answers with the worktree and not the canonical
    checkout the config paths are relative to.
    """
    here = (start or Path(__file__).resolve()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "adws").is_dir() and (candidate / DEFAULT_CONFIG_REL).is_file():
            return candidate
    raise RuntimeError(
        f"no SSSF checkout above {here}: expected a parent holding {DEFAULT_CONFIG_REL}"
    )


class FactoryPaths:
    """Resolved locations for one checkout. Cheap to build, safe to rebuild."""

    def __init__(self, root: Path | None = None, config_rel: str = DEFAULT_CONFIG_REL):
        self.root = root or repo_root()
        self.config_file = self.root / config_rel
        raw = yaml.safe_load(self.config_file.read_text(encoding="utf-8")) or {}
        defaults = raw.get("defaults") or {}

        data_dir = defaults.get("data_dir")
        if not data_dir:
            raise RuntimeError(
                f"{self.config_file} has no defaults.data_dir; the factory cannot "
                "place its state inside the guard's exclusion without it"
            )

        self.data_dir = self.root / data_dir
        self.sessions_dir = self.data_dir / "sessions"
        self.factory_dir = self.data_dir / "factory"

        # The tracer writes this directly and the UI polls it. `justfile` and the
        # config must agree; they have drifted before, and a factory reading the
        # stale path sees a database frozen in time.
        observability = raw.get("observability") or {}
        self.tracer_db = self.root / (observability.get("db") or f"{data_dir}/sssf.db")

        self.state_file = self.factory_dir / "state.json"
        self.tick_lock = self.factory_dir / "tick.lock"
        self.tick_log = self.factory_dir / "tick.log"
        # The driver's liveness marker and the supervisor's own two files.
        # All beside `state.json`, still inside the guard's exclusion, because
        # the supervisor's rule is that it never writes the factory's state --
        # its memory and its report live in files only it owns.
        self.driver_pid = self.factory_dir / "driver.pid"
        self.supervisor_state_file = self.factory_dir / "supervisor-state.json"
        self.supervisor_status_file = self.factory_dir / "supervisor-status.json"
        # The inter-process lock that makes one supervisor poll exclusive:
        # two scheduled/on-demand polls racing one empty directory must not
        # both launch a diagnosis agent on the same signal. An empty file the
        # OS locks; it is created once and outlives every poll, so a late
        # contender always opens the same inode instead of a fresh name.
        self.supervisor_poll_lock = self.factory_dir / "supervisor-poll.lock"
        # The diagnosis agents' logs: one timestamped file per launch, kept
        # beside the supervisor's own state, never beside tracked source.
        self.diagnosis_log_dir = self.factory_dir / "diagnosis-logs"
        # The immutable item-action audit records the diagnosis agents write
        # and the supervisor polls read: one JSON file per reached action,
        # under the guard's exclusion like every other mutable byte, and kept
        # forever so replacing `supervisor-status.json` never erases history.
        self.item_action_dir = self.factory_dir / "item-actions"

    def ensure_state_dir(self) -> Path:
        """Create the state directory. Never creates anything tracked."""
        self.factory_dir.mkdir(parents=True, exist_ok=True)
        return self.factory_dir

    def session_dir(self, adw_id: str) -> Path:
        return self.sessions_dir / adw_id

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"FactoryPaths(root={self.root}, factory_dir={self.factory_dir})"
