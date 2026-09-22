"""Entry point: `python -m adws_factory <command>`.

    status     read the world and print it; changes nothing
    tick       one pass
    plan       one pass that stops short of launching (dry run)
    loop       tick, wait for quiet, tick again
    supervisor one health poll; writes supervisor-status.json and exits

`loop` is event-driven rather than timed: while any run is active it watches and
does nothing, and ticks the moment nothing is running. A fixed interval wastes
the gap between a stage finishing and the next pass -- on the predecessor
project a single item lost hours that way.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import labels, launcher, supervisor, recovery
from .artifacts import GhError
from .config import FactoryConfig
from .gates import check_credits, check_disk
from .paths import FactoryPaths
from .state import FactoryState
from .tick import TickLockHeld, current_position, run_tick

__version__ = "0.1.3"


def cmd_status(config: FactoryConfig, paths: FactoryPaths) -> int:
    print(f"repo        {config.repo}")
    print(f"root        {paths.root}")
    print(f"state       {paths.state_file}")
    print(f"stages      {' -> '.join(stage.name for stage in config.stages)}")

    disk = check_disk(config, paths.root)
    print(f"disk        {'ok ' if disk.passed else 'BLOCK'} {disk.detail}")
    for stage in config.stages:
        gate = check_credits(config, stage, paths.root)
        print(f"credits {stage.name:3s} {'ok ' if gate.passed else 'BLOCK'} {gate.detail}")

    state = FactoryState.load(paths.state_file)
    print(f"active item {state.active_item}")
    if state.tracked:
        for run in state.tracked:
            alive = "alive" if launcher.is_alive(run.pid) else "gone"
            print(f"  tracked   {run.adw_id} {run.stage} #{run.item} pid={run.pid} {alive}")
    else:
        print("  tracked   none")

    for key, budget in sorted(state.budgets.items()):
        flag = f" BLOCKED at {budget.blocked_stage}" if budget.is_blocked else ""
        print(
            f"  budget #{key}  work={budget.work_attempts}/{config.limits.work_attempts}"
            f" launches={budget.launches}/{config.limits.launch_ceiling}"
            f" rounds={budget.rounds}/{config.limits.max_rounds}{flag}"
        )

    try:
        items = labels.eligible_items(config, paths.root)
    except labels.LabelError as error:
        print(f"eligible    unreadable: {error}")
        return 1

    print(f"eligible    {items or 'none'}")
    for item in items[:10]:
        try:
            stage, pr = current_position(config, paths, item)
        except GhError as error:
            print(f"  #{item:<5} unreadable: {error}")
            continue
        where = stage.name if stage else "all stages done -> decider"
        pr_note = f"PR #{pr.number} @{pr.revision[:8]}" if pr else "no PR"
        print(f"  #{item:<5} {where:<28} {pr_note}")
    return 0


def cmd_tick(config: FactoryConfig, paths: FactoryPaths, dry_run: bool) -> int:
    report = run_tick(config, paths, dry_run=dry_run)
    print(report.line())
    return 1 if report.errors else 0


def cmd_loop(config: FactoryConfig, paths: FactoryPaths, idle_seconds: int) -> int:
    """Tick, then wait until nothing is running, then tick again."""
    # The driver lease advertises this process for the supervisor's liveness
    # check. Without it, a healthy detached loop between ticks reads as "no
    # live driver" the moment the silence window passes -- and the poll that
    # exists to catch a dead loop pages about a live one.
    with supervisor.DriverLease(paths.driver_pid):
        while True:
            try:
                report = run_tick(config, paths)
            except TickLockHeld as error:
                # TickLock: another tick (a manual `tick`, a highspeed driver) holds the
                # lock. Harmless -- but an unhandled raise here kills the detached loop,
                # which reads afterwards as "the loop just stopped".
                print(f"tick skipped: {error}", flush=True)
                time.sleep(min(idle_seconds, 30))
                continue
            print(report.line(), flush=True)

            state = FactoryState.load(paths.state_file)
            if not state.tracked:
                time.sleep(idle_seconds)
                continue

            while True:
                time.sleep(min(idle_seconds, 30))
                state = FactoryState.load(paths.state_file)
                if not any(launcher.is_alive(run.pid) for run in state.tracked):
                    break


def cmd_supervisor(config: FactoryConfig, paths: FactoryPaths) -> int:
    """One poll. Detected factory trouble is a successful result here.

    Only the inability to complete or persist the poll exits nonzero, so Task
    Scheduler never confuses "the supervisor found something wrong" with
    "the supervisor crashed" -- the first needs a page, the second needs a
    reinstall, and they must stay distinguishable. A poll skipped because
    another poll holds the transaction lock is neither: the other poll IS
    this poll's work, already being done.
    """
    try:
        payload = supervisor.run_poll(config, paths)
    except supervisor.SupervisorPollLockHeld as error:
        print(f"supervisor poll skipped: {error}")
        return 0
    except OSError as error:
        print(f"supervisor poll failed to persist: {error}")
        return 1
    print(supervisor.summarize(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adws_factory", description=__doc__)
    parser.add_argument(
        "command",
        choices=["status", "tick", "plan", "loop", "supervisor"],
        help="what to do",
    )
    parser.add_argument("--repo", default=None, help="owner/name override")
    parser.add_argument(
        "--idle-seconds", type=int, default=60, help="loop: pause when nothing is eligible"
    )
    parser.add_argument(
        "--print-interval",
        action="store_true",
        help="supervisor: print the configured poll interval in minutes and exit",
    )
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--pause-recovery", action="store_true",
                             help="supervisor: pause recovery and live ticks for maintenance")
    maintenance.add_argument("--resume-recovery", action="store_true",
                             help="supervisor: clear the maintenance pause")
    args = parser.parse_args(argv)
    if (args.pause_recovery or args.resume_recovery) and args.command != "supervisor":
        parser.error("recovery maintenance flags require supervisor")

    paths = FactoryPaths()
    config = FactoryConfig.load()
    if args.repo:
        config.repo = args.repo

    if args.command == "status":
        return cmd_status(config, paths)
    if args.command == "tick":
        return cmd_tick(config, paths, dry_run=False)
    if args.command == "plan":
        return cmd_tick(config, paths, dry_run=True)
    if args.command == "supervisor":
        if args.pause_recovery or args.resume_recovery:
            # Serialize with both actors: after this returns no in-flight poll
            # or tick can still apply a pre-pause decision.
            with supervisor.SupervisorPollLock(paths.supervisor_poll_lock):
                from .tick import TickLock
                with TickLock(paths.tick_lock):
                    recovery.set_paused(paths, args.pause_recovery)
            print("recovery and ticks " + ("paused" if args.pause_recovery else "resumed"))
            return 0
        if args.print_interval:
            # The machine-readable half of the installer contract: the
            # scheduled task reads its interval from here, so there is one
            # place to change the schedule and no second copy in PowerShell.
            print(config.supervisor.poll_interval_minutes)
            return 0
        return cmd_supervisor(config, paths)
    try:
        return cmd_loop(config, paths, args.idle_seconds)
    except supervisor.DriverLeaseError as error:
        # The lease refused to start: an unadvertised loop reads as dead to
        # the supervisor, so driving on would manufacture false pages.
        print(f"loop cannot start: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
