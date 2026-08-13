#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""ADW Kill — stop a run: agent children first, then the workflow process.

Signals are sent only after verifying the pid still matches the command the
trace recorded — pids get recycled, and a recycled pid must not be killed.

On POSIX the ADW's own SIGTERM handler (`_finalize_when_killed` in session.py)
finalizes the session as failed. Windows has no SIGTERM: the process is
terminated outright and that handler never runs, so this script closes the
books itself, and `session.ensure` sweeps for corpses at every run start
whether or not anyone remembers to run this.

Usage:
    uv run adws/adw_kill.py --adw-id a1b2c3d4 [--grace 5]
    uv run adws/adw_kill.py --list
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adw_modules import procinfo    # noqa: E402

DEFAULT_DB = "adws/adw_runtime/sssf.db"


def _terminate(pid: int, force: bool) -> None:
    """Ask a process to stop (force=False), or end it outright (force=True)."""
    if procinfo.IS_WINDOWS:
        # No signals here. `taskkill` without /F posts WM_CLOSE, which a console
        # process may ignore -- that is what the grace period is for. /T takes
        # the tree, so an agent's own children go with it.
        args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        subprocess.run(args, capture_output=True, text=True)
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def reconcile(db: Path, adw_id: str) -> None:
    """Close the books on a run whose processes are gone.

    A hard-killed or crashed ADW never reaches its own finalizer, so its
    session row reads 'running' forever. Once every recorded pid is verified
    dead, finalize on its behalf: session failed, open phases failed, process
    rows closed. Idempotent, so a self-finalized run is left exactly as it
    wrote itself.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    conn = sqlite3.connect(db)
    try:
        changed = conn.execute(
            "UPDATE sessions SET status='fail', ended_at=?"
            " WHERE adw_id=? AND status='running'", (ts, adw_id)).rowcount
        conn.execute(
            "UPDATE phases SET status='fail', ended_at=?,"
            " error='process died without finalizing (hard kill or crash)'"
            " WHERE adw_id=? AND status='running'", (ts, adw_id))
        conn.execute(
            "UPDATE processes SET ended_at=? WHERE adw_id=? AND ended_at IS NULL",
            (ts, adw_id))
        conn.commit()
        if changed:
            print(f"finalized {adw_id}: session marked fail, trace closed")
    finally:
        conn.close()


def show_running(conn: sqlite3.Connection) -> int:
    """List what the trace believes is in flight, and whether it really is."""
    rows = conn.execute(
        "SELECT s.adw_id, s.adw_name, p.pid, p.command FROM sessions s"
        " LEFT JOIN processes p ON p.adw_id = s.adw_id AND p.kind = 'adw'"
        " WHERE s.status = 'running' ORDER BY s.started_at").fetchall()
    if not rows:
        print("no sessions read 'running'")
        return 0
    live = procinfo.live_commands(pid for _, _, pid, _ in rows if pid)
    for adw_id, adw_name, pid, command in rows:
        actual = live.get(int(pid)) if pid else None
        state = ("alive" if actual is not None
                 and procinfo.is_same_process(command, actual) else "DEAD")
        print(f"{adw_id}  {state:5}  pid {pid or '-':<8} {adw_name or ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ADW Kill — stop a run: agent children first, then the workflow")
    ap.add_argument("--adw-id", help="the run to stop")
    ap.add_argument("--list", action="store_true",
                    help="show sessions reading 'running' and whether they are")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"trace db path (default: {DEFAULT_DB})")
    ap.add_argument("--grace", type=float, default=5.0,
                    help="seconds between the polite stop and the forced one (default 5)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"error: no trace db at {db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    if args.list:
        return show_running(conn)
    if not args.adw_id:
        print("error: --adw-id is required (or --list)", file=sys.stderr)
        return 2

    rows = conn.execute(
        "SELECT kind, name, pid, command FROM processes"
        " WHERE adw_id=? AND ended_at IS NULL"
        " ORDER BY (kind = 'agent') DESC, id",  # children first, workflow last
        (args.adw_id,),
    ).fetchall()
    if not rows:
        print(f"no live processes recorded for {args.adw_id}")
        reconcile(db, args.adw_id)       # a dead run may still read 'running'
        return 0

    live = procinfo.live_commands(pid for _, _, pid, _ in rows)
    targets = []
    for kind, name, pid, command in rows:
        actual = live.get(int(pid))
        if actual is None:
            print(f"skip pid {pid}: already gone")
            continue
        if not procinfo.is_same_process(command, actual):
            print(f"skip pid {pid}: recorded '{command}' but now running"
                  f" '{actual[:80]}' — recycled, not killing")
            continue
        targets.append((int(pid), kind, name))
        print(f"stop {kind} {name or args.adw_id} pid {pid}")
        _terminate(int(pid), force=False)

    deadline = time.monotonic() + max(0.0, args.grace)
    while time.monotonic() < deadline:
        if not procinfo.live_commands(pid for pid, _, _ in targets):
            break
        time.sleep(0.25)

    still = procinfo.live_commands(pid for pid, _, _ in targets)
    for pid, kind, name in targets:
        if pid in still:
            print(f"force {kind} {name or args.adw_id} pid {pid}")
            _terminate(pid, force=True)

    # A polite stop lets the ADW finalize its own trace; a forced one (and any
    # pid already gone) cannot -- and on Windows nothing ever can. Once nothing
    # recorded is alive, close the books.
    time.sleep(0.5)
    if not procinfo.live_commands(pid for _, _, pid, _ in rows):
        reconcile(db, args.adw_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
