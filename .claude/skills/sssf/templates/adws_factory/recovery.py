"""Bounded infrastructure recovery, called under the independent poll lock.

No review judgement, branch reset, merge or budget reset lives here. Reaping
uses the tick's existing artifact-first rules under its lock. Process evidence
is fail-closed. psutil is required only when a recovery capability is armed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import launcher
from .state import FactoryState


def paused(paths) -> bool:
    return (paths.factory_dir / "recovery.paused").exists()


def set_paused(paths, value: bool) -> None:
    paths.ensure_state_dir()
    marker = paths.factory_dir / "recovery.paused"
    if value:
        marker.write_text("Maintenance: automatic recovery and ticks paused.\n", encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)


def _seconds(stamp) -> float:
    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _save(path, value):
    from .supervisor import _atomic_write
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _state(paths):
    # FactoryState.load tolerates corruption for historical reasons. Recovery
    # cannot interpret an unreadable tracking set as permission to restart.
    raw = json.loads(paths.state_file.read_text(encoding="utf-8"))
    if raw.get("schema") != 1 or not isinstance(raw.get("tracked"), list):
        raise ValueError("factory state schema/tracked set unreadable")
    return FactoryState.load(paths.state_file)


def _processes():
    try:
        import psutil
    except ImportError as error:
        raise RuntimeError("armed recovery requires psutil: python -m pip install psutil") from error
    return psutil


def alive(pid):
    if not pid:
        return False
    ps = _processes()
    try:
        return ps.Process(pid).is_running()
    except ps.NoSuchProcess:
        return False


def run_identity(paths, config, run):
    """A PID is not an identity: bind birth time, cwd, workflow and run id."""
    ps = _processes()
    proc = ps.Process(run.pid)
    stage = config.stage_by_name(run.stage)
    argv = proc.cmdline()
    if stage is None or "--adw-id" not in argv:
        raise ValueError(f"{run.adw_id}: process does not name its tracked workflow")
    i = argv.index("--adw-id")
    if i + 1 >= len(argv) or argv[i + 1] != run.adw_id:
        raise ValueError(f"{run.adw_id}: process run id mismatch")
    if not any(Path(arg).name == Path(stage.workflow).name for arg in argv):
        raise ValueError(f"{run.adw_id}: process workflow mismatch")
    if Path(proc.cwd()).resolve() != paths.root.resolve():
        raise ValueError(f"{run.adw_id}: process checkout mismatch")
    born = proc.create_time()
    if abs(born - _seconds(run.launched_at)) > 120:
        raise ValueError(f"{run.adw_id}: PID birth time differs from launch (possible reuse)")
    return {"pid": proc.pid, "born": born, "argv": argv}


def progress(paths, run):
    """Per-run durable progress, not the DB file mtime (other runs write it).

    Include raw stream/log metadata so a streaming response counts even
    before it emits a tool or phase event. Read failure is NOT silence.
    """
    with sqlite3.connect(paths.tracer_db.resolve().as_uri() + "?mode=ro", uri=True,
                         timeout=5) as db:
        db.execute("BEGIN")
        session = db.execute(
            "SELECT status, ended_at, total_tokens FROM sessions WHERE adw_id=?",
            (run.adw_id,)).fetchone()
        phases = db.execute(
            "SELECT phase_id,status,attempt,started_at,ended_at FROM phases "
            "WHERE adw_id=? ORDER BY phase_id", (run.adw_id,)).fetchall()
        events = db.execute(
            "SELECT MAX(rowid), COUNT(*) FROM events WHERE adw_id=?",
            (run.adw_id,)).fetchone()
    files = []
    directory = paths.session_dir(run.adw_id)
    if directory.exists():
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                stat = path.stat()
                files.append((str(path.relative_to(directory)), stat.st_size, stat.st_mtime_ns))
    for path in sorted((paths.factory_dir / "logs").glob(f"*{run.adw_id}*")):
        stat = path.stat()
        files.append((path.name, stat.st_size, stat.st_mtime_ns))
    return json.dumps([session, phases, events, files], sort_keys=True)


def stop_tree(paths, config, run, expected):
    """Freeze the verified parent before enumerating children; kill children first.

    psutil's Process methods check birth time before signalling, including on
    Windows. Never use taskkill on an unverified numeric PID. On partial failure
    resume surviving processes and leave tracking in place for inspection.
    """
    ps = _processes()
    if run_identity(paths, config, run) != expected:
        raise ValueError("process identity changed before termination")
    parent = ps.Process(run.pid)
    if parent.create_time() != expected["born"]:
        raise ValueError("PID reused between identity check and process handle")
    suspended = []
    try:
        parent.suspend()
        suspended.append(parent)
        # Stabilise the tree: children can themselves be launching children.
        known = {parent.pid}
        for _ in range(10):
            children = [p for p in parent.children(recursive=True) if p.pid not in known]
            if not children:
                break
            for proc in children:
                proc.suspend()
                suspended.append(proc)
                known.add(proc.pid)
        else:
            raise RuntimeError("process tree did not stabilise; refusing termination")
        for proc in reversed(suspended):
            try:
                proc.kill()
            except ps.NoSuchProcess:
                pass
        _, alive = ps.wait_procs(suspended, timeout=10)
        if alive:
            raise RuntimeError(f"processes still alive after termination: {[p.pid for p in alive]}")
    finally:
        for proc in suspended:
            try:
                if proc.is_running():
                    proc.resume()
            except ps.NoSuchProcess:
                pass


def children_gone(paths, run):
    """Dead parent alone is insufficient: recorded agents may still be alive."""
    with sqlite3.connect(paths.tracer_db.resolve().as_uri() + "?mode=ro", uri=True,
                         timeout=5) as db:
        pids = db.execute("SELECT pid FROM processes WHERE adw_id=? AND ended_at IS NULL",
                          (run.adw_id,)).fetchall()
    return not any(alive(pid) for (pid,) in pids)


def other_driver(paths):
    """Catch legacy drivers that predate the lifetime driver lease."""
    ps = _processes()
    for proc in ps.process_iter(["pid", "name"]):
        if "python" not in (proc.info["name"] or "").lower():
            continue
        try:
            argv = proc.cmdline()
            driving = (any(Path(a).name == "highspeed.py" for a in argv)
                       and not any(a in argv for a in ("--watch", "--once", "--stop-loop")))
            driving = driving or ("adws_factory" in argv and "loop" in argv)
            if driving and Path(proc.cwd()).resolve() == paths.root.resolve():
                return proc.pid
        except ps.NoSuchProcess:
            continue
        # AccessDenied must propagate: cannot inspect != no driver.
    return None


def start_driver(paths):
    return launcher._spawn_detached(
        [sys.executable, "-X", "utf8", "-u", str(paths.root / "highspeed.py"),
         "--config", str(paths.config_file.relative_to(paths.root))],
        paths.root, paths.factory_dir / "recovery-driver.log").pid


def run_recovery(config, paths, now):
    """One cheap poll; repeats and idle clocks survive supervisor restarts."""
    result = {"paused": paused(paths), "runs": [], "actions": [], "errors": []}
    cfg = config.supervisor.recovery
    enabled = cfg.restart_driver or cfg.stop_hung_runs or cfg.reap_dead_runs
    if result["paused"] or not enabled:
        # Maintenance/disabling breaks the observation sequence. Time spent deliberately
        # paused must not count as proof of a hung run after resuming.
        memory_path = paths.factory_dir / "recovery-state.json"
        if memory_path.exists():
            try:
                memory = json.loads(memory_path.read_text(encoding="utf-8"))
                memory["runs"] = {}
                memory.setdefault("restart", {})["repeats"] = 0
                _save(memory_path, memory)
            except Exception as error:
                result["errors"].append(f"cannot reset paused recovery observations: {error}")
        return result
    memory_path = paths.factory_dir / "recovery-state.json"
    stamp = now.timestamp()
    try:
        _processes()
        memory = (json.loads(memory_path.read_text(encoding="utf-8"))
                  if memory_path.exists() else {"runs": {}, "restart": {}})
        if not isinstance(memory.get("runs"), dict) or not isinstance(memory.get("restart"), dict):
            raise ValueError("recovery memory unreadable")
        state = _state(paths)
    except Exception as error:
        result["errors"].append(f"recovery cannot read evidence: {error}")
        return result

    def audit(action, **details):
        record = {"at": now.isoformat(), "action": action, **details}
        path = paths.factory_dir / "recovery-actions" / f"{uuid.uuid4().hex}.json"
        _save(path, record)  # durable BEFORE the side effect
        result["actions"].append(record)
        return path, record

    from .tick import TickLock, TickLockHeld, TickReport, reap
    from .supervisor import read_pid_owner

    active_ids = {run.adw_id for run in state.tracked}
    memory["runs"] = {k: v for k, v in memory["runs"].items() if k in active_ids}
    for run in state.tracked:
        try:
            if not cfg.stop_hung_runs or not alive(run.pid):
                memory["runs"].pop(run.adw_id, None)
                continue
            identity = run_identity(paths, config, run)
            fingerprint = progress(paths, run)
            old = memory["runs"].get(run.adw_id, {})
            unchanged = old.get("identity") == identity and old.get("progress") == fingerprint
            since = old["since"] if unchanged else stamp
            idle = max(0, stamp - since)
            age = stamp - _seconds(run.launched_at)
            candidate = age >= cfg.hung_min_age_seconds and idle >= cfg.hung_idle_seconds
            repeats = (old.get("repeats", 0) + 1) if candidate and unchanged else int(candidate)
            entry = {"identity": identity, "progress": fingerprint, "since": since, "repeats": repeats}
            memory["runs"][run.adw_id] = entry
            result["runs"].append({"adw_id": run.adw_id, "item": run.item,
                                   "age_seconds": age, "idle_seconds": idle,
                                   "candidate": candidate, "repeats": repeats})
            if repeats < config.supervisor.repeat_polls:
                continue
            with TickLock(paths.tick_lock):
                fresh = _state(paths)
                current = next((r for r in fresh.tracked if r == run), None)
                if paused(paths) or current is None or progress(paths, run) != fingerprint:
                    memory["runs"].pop(run.adw_id, None)
                    continue
                audit_path, record = audit("stop_hung_run", adw_id=run.adw_id,
                                           identity=identity, outcome="prepared")
                stop_tree(paths, config, run, identity)
                record["outcome"] = "stopped"
                _save(audit_path, record)
        except TickLockHeld:
            result["errors"].append(f"{run.adw_id}: recovery deferred: tick busy")
        except Exception as error:
            memory["runs"].pop(run.adw_id, None)  # unreadable breaks consecutive observations
            result["errors"].append(f"{run.adw_id}: recovery refused: {error}")

    if cfg.reap_dead_runs:
        try:
            with TickLock(paths.tick_lock):
                fresh = _state(paths)
                dead = {r.adw_id for r in fresh.tracked
                        if not alive(r.pid) and children_gone(paths, r)}
                if dead and not paused(paths):
                    path, record = audit("reap_dead_runs", adw_ids=sorted(dead), outcome="prepared")
                    report = TickReport(now.isoformat())
                    reap(config, paths, fresh, report, only_ids=dead)
                    fresh.save_action(paths.state_file)  # no counterfeit tick heartbeat
                    record.update(outcome="reaped", reaped=report.reaped, errors=report.errors)
                    _save(path, record)
                    result["errors"].extend(report.errors)
        except TickLockHeld:
            pass  # the real tick already owns recovery of terminal runs
        except Exception as error:
            result["errors"].append(f"dead-run cleanup refused: {error}")

    if cfg.restart_driver:
        try:
            with TickLock(paths.tick_lock):
                fresh = _state(paths)
                restarting = memory["restart"]
                # A post-restart tick is the acknowledgement, not a successful spawn.
                if (restarting.get("at") and fresh.last_tick_at
                        and _seconds(fresh.last_tick_at) > restarting["at"]):
                    memory["restart"] = restarting = {}
                from . import labels
                needed = bool(fresh.tracked or labels.eligible_items(config, paths.root))
                driver = read_pid_owner(paths.driver_pid)
                old = fresh.last_tick_at and stamp - _seconds(fresh.last_tick_at) > config.supervisor.silence_window_seconds
                if not paused(paths) and needed and old and not driver.alive and not other_driver(paths):
                    repeats = restarting.get("repeats", 0) + 1
                    restarting["repeats"] = repeats
                    tries = restarting.get("attempts", 0)
                    if tries >= cfg.max_restart_attempts:
                        raise RuntimeError("driver restart ceiling reached; inspect recovery-driver.log")
                    cooling = stamp - restarting.get("at", 0) < cfg.restart_cooldown_seconds
                    if repeats >= config.supervisor.repeat_polls and not cooling:
                        restarting.update(at=stamp, attempts=tries + 1)
                        _save(memory_path, memory)  # reserve across crash/startup race
                        path, record = audit("restart_driver", outcome="prepared")
                        record.update(pid=start_driver(paths), outcome="spawned")
                        _save(path, record)
                else:
                    restarting["repeats"] = 0
        except TickLockHeld:
            pass
        except Exception as error:
            memory["restart"]["repeats"] = 0
            result["errors"].append(f"driver restart refused: {error}")
    _save(memory_path, memory)
    return result
