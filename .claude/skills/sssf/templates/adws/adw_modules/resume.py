"""Reading a previous run's completed phases back out of the tracer db.

A failed run is not worthless: its successful phases are recorded, and so are
the envelopes they produced. Re-running the whole ADW to reach the phase that
actually failed pays for that work twice -- a planner pass costs real money and
real minutes, and re-committing an identical plan fails outright with "nothing
to commit".

This module answers two questions about a prior run, and nothing else:

  * which leading phases succeeded, so a resume knows where to start;
  * what envelope a skipped agent phase produced, so the phase that consumed it
    still receives it.

Deliberately NOT here: any decision about what to skip. The ADW owns that.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


class ResumeError(RuntimeError):
    """A resume was asked for and cannot be honoured. Never a silent fallback."""


def _connect(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise ResumeError(
            f"no run database at {db_path} -- there is nothing to resume from.")
    # Read-only: a resume inspects history, it never edits it.
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def completed_prefix(db_path: Path, adw_id: str) -> list[str]:
    """Names of the leading phases that succeeded, in order, stopping at the first that did not.

    Stopping at the first non-success is the point: a phase that ran after a
    failure was computed against a tree that has since been repaired, so its
    result is stale even though the row says success.
    """
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT name, status FROM phases WHERE adw_id = ? ORDER BY seq",
            (adw_id,),
        ).fetchall()

    if not rows:
        raise ResumeError(
            f"run {adw_id!r} has no phases in {db_path} -- check the id with "
            f"`just phases {adw_id}`.")

    done: list[str] = []
    for name, status in rows:
        if status != "success":
            break
        done.append(name)
    return done


def envelope_json(db_path: Path, adw_id: str, phase_name: str) -> Optional[dict]:
    """The valid envelope a named phase produced, or None if it produced none.

    Only `valid` envelopes are returned: a malformed one was never accepted by
    the original run and must not be handed onward by a resumed one.
    """
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT e.payload_json FROM envelopes e "
            "JOIN phases p ON p.phase_id = e.phase_id "
            "WHERE e.adw_id = ? AND p.name = ? AND e.valid = 1 "
            "ORDER BY e.attempt DESC LIMIT 1",
            (adw_id, phase_name),
        ).fetchone()

    if row is None or row[0] is None:
        return None
    return json.loads(row[0])


def rehydrate(db_path: Path, adw_id: str, phase_name: str, output_type):
    """Rebuild a skipped phase's typed envelope so the next phase still gets it.

    Raises rather than returning None: if a phase is being skipped *because* it
    succeeded, its envelope has to be there. A missing one means the db and the
    skip decision disagree, and guessing would hand the builder a plan it never
    made.
    """
    payload = envelope_json(db_path, adw_id, phase_name)
    if payload is None:
        raise ResumeError(
            f"phase {phase_name!r} of run {adw_id!r} has no valid envelope to "
            f"reuse. A phase is only skipped because it succeeded, so a missing "
            f"envelope means the db and the skip decision disagree -- rerun the "
            f"whole ADW rather than resuming.")
    return output_type(**payload)
