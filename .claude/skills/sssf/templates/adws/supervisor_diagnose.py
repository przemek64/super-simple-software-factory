#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Supervisor diagnosis -- one signal, one read-only agent, one answer, exit.

Usage:
    uv run adws/supervisor_diagnose.py --kind item_stalled --item 4 \
        --signature ab12cd34 --reason "<what the poll observed>" \
        --observed-at 2026-08-25T12:00:00+00:00 --adw-id ab12cd34 \
        [--config adws/adw_sssf_config/sssf.config.yaml]

Phases: engineer(request) -> scout(diagnose)

This is deliberately NOT an `adw_*.py` workflow entrypoint. It is a reader the
supervisor wakes when a signal fires: it takes one incident, reasons about it
once, prints the complete diagnosis to stdout (the launcher's timestamped log
is the canonical output a person reads), and exits. It never watches, sleeps,
polls, or promises to check back -- the recurring poll is the watcher, and a
one-shot run cannot hold that promise anyway.

Read-only by construction, not by instruction alone: the run borrows the
existing `scout` seat and then locks its in-memory configuration down -- no
worktree, no branch, no harness extensions beyond the item-action tools, no
write/edit/bash tools, an empty `writes` list, and the diagnostician prompts.
The permission guard remains the second repository attribution boundary. No
protected roster entry or agent module is modified.

The one deliberate extension to that contract: five constrained item-action
tools (`clear_budget`, `relaunch_item`, `page_again`, `hold_item`,
`request_review`). Each spawns the factory's action request command with the
incident bound in THIS process's environment, so a model cannot choose
another item, and the command only writes the immutable audit record naming
the action, the item, the reason, and the flag holding it back. Every flag
defaults to dry-run: the record is the deliverable, and no action happens
without an explicit configuration change made by a person.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

# The action tools spawn `python -m adws_factory.item_actions`, and this
# runner reads the records that command writes. Running as a script puts only
# `adws/` on sys.path, so the checkout root -- where `adws_factory` lives --
# is added explicitly.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adw_modules import gates, session
from adw_modules.agents import resolve
from adw_modules.data_types import (
    AgentCall,
    GateReport,
    GenericOutput,
    PhaseParams,
    PromptEngineering,
)
from adws_factory import item_actions
from adws_factory.paths import FactoryPaths

REQUIRED_AGENTS = ["scout"]
# Every supervisor signal that may wake this agent: the three process
# signals, and the three infrastructure faults of issue #111 -- each carries
# its own facts on the command line, and each is diagnosed read-only.
SIGNAL_KINDS = (
    "loop_dead",
    "item_stalled",
    "escalation_unanswered",
    "disk_below_floor",
    "provider_credit_exhausted",
    "run_vanished",
)
# The safety contract, not a preference: with these four the agent can inspect
# local source and factory-owned artifacts but has no shell, edit, or write
# capability with which to mutate Git, GitHub, or factory files.
READ_ONLY_TOOLS = ("read", "grep", "find", "ls")
# The five constrained item-action tools (adws_factory/item_actions.py). Each
# one only RECORDS the action it names, bound to this incident's item; whether
# the record is ever performed is a configuration flag the tool cannot see.
ACTION_TOOLS = (
    "clear_budget",
    "relaunch_item",
    "page_again",
    "hold_item",
    "request_review",
)
DIAGNOSTICIAN_PROMPTS = "adws/adw_data/prompt_engineering/supervisor_diagnostician"
ACTION_TOOLS_EXTENSION = (
    "adws/adw_data/harness_engineering/supervisorActions.ts"
)


def diagnosis_complete(envelope, run) -> GateReport:
    """A successful diagnosis must contain one a person can read.

    `notes_for_next_agent` is the deliverable -- it is what the run log
    prints and what a paged person reads instead of re-doing the work -- and
    `summary` is the one line the trace shows. `GenericOutput` permits both
    to be empty, so a run that succeeded while saying nothing would look
    diagnosed and not be: cheaper than a failed run, and exactly as useless.
    The gate corrects the SAME agent session first; only a refusal to say
    anything fails the run.
    """
    report = GateReport()
    summary = (envelope.summary or "").strip()
    notes = (envelope.notes_for_next_agent or "").strip()
    report.check(
        "summary",
        bool(summary),
        "one sentence naming the signal and what is wrong"
        if summary
        else "a successful diagnosis needs a nonempty summary",
    )
    report.check(
        "notes_for_next_agent",
        bool(notes),
        f"the complete diagnosis, {len(notes)} characters"
        if notes
        else "the complete diagnosis is the deliverable; it cannot be empty",
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="supervisor_diagnose", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--kind", required=True, choices=SIGNAL_KINDS,
                        help="which supervisor signal fired")
    parser.add_argument("--item", type=int, default=None,
                        help="the item the signal names, if any")
    parser.add_argument("--signature", required=True,
                        help="the incident's stable signature")
    parser.add_argument("--reason", required=True,
                        help="what the poll observed, verbatim")
    parser.add_argument("--observed-at", required=True,
                        help="ISO timestamp of the observing poll")
    parser.add_argument("--adw-id", default=None,
                        help="pin the session id the supervisor chose")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    args = parser.parse_args(argv)
    if not args.signature.strip():
        parser.error("--signature must not be empty")
    if not args.reason.strip():
        parser.error("--reason must not be empty")
    if normalize_observed_at(args.observed_at) is None:
        parser.error(f"--observed-at is not an ISO timestamp: {args.observed_at!r}")
    return args


def normalize_observed_at(raw: str) -> str | None:
    """Canonicalize the observing poll's stamp; None when unparseable.

    Naive means UTC, `Z` is expanded, and the answer keeps its offset so the
    diagnosis names the same moment the supervisor signed the incident with.
    """
    try:
        moment = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def build_prompt(
    kind: str, item: int | None, signature: str, reason: str, observed_at: str
) -> str:
    """The one incident, stated once, with every fact the agent is given.

    Exactly one signal kind, item, signature, observed time and reason -- no
    second incident, no aggregate, nothing derived from the launch itself.
    """
    lines = [
        "Supervisor signal diagnosis request.",
        "",
        f"Signal kind: {kind}",
        f"Item: #{item}" if item is not None else "Item: none (factory-wide signal)",
        f"Incident signature: {signature}",
        f"Observed at: {observed_at}",
        f"Observed reason: {reason}",
        "",
        "The supervisor's recurring poll observed this signal firing and woke",
        "you to work out what is actually wrong. Diagnose this one incident:",
        "what the situation is, what the evidence says, and what a person",
        "should look at. Follow the system procedure exactly -- read-only, no",
        "ruling on review findings, no merge, no repair, and no promise to",
        "check back. The only non-observation permitted is one of the five",
        "constrained item-action tools, and only when your evidence justifies",
        "that action for THIS item; include your complete reason, and",
        "otherwise call none. If the right answer is that a run is in",
        "progress and the factory should wait, say that and stop.",
    ]
    return "\n".join(lines)


def configure_read_only_seat(cfg, repo_root: Path) -> None:
    """Lock the in-memory run configuration to the diagnostician contract.

    The seat is the existing `scout` -- already a read-only purpose -- but its
    configured tools, extensions and prompts belong to recon work, not this
    procedure. Only this run's in-memory copy changes: no roster file, no
    agent module, and no factory configuration is modified, and the agent
    never gets a worktree or a branch of its own.

    The one extension installed is the item-action tool set: five separately
    named tools, nothing generic -- no command, path, label, GitHub or
    subprocess surface a model could aim anywhere else.
    """
    cfg.defaults.worktree.enabled = False
    seat = resolve(cfg, "scout")
    seat.tools = [*READ_ONLY_TOOLS, *ACTION_TOOLS]
    seat.writes = []
    seat.harness_engineering = [
        str((repo_root / ACTION_TOOLS_EXTENSION).resolve())
    ]
    prompts = repo_root / DIAGNOSTICIAN_PROMPTS
    seat.prompt_engineering = PromptEngineering(
        system=str((prompts / "system.md").resolve()),
        user=str((prompts / "user.md").resolve()),
    )


@contextmanager
def bound_incident_context(
    kind: str, item: int | None, signature: str, adw_id: str
):
    """Bind this incident into the environment the agent's children inherit.

    The five action tools do not take an item argument: the request command
    they spawn reads the incident (item, signal kind, signature, this run's
    id) from the process environment set here, so a model cannot select
    another item by supplying an arbitrary number. Set for the agent call
    only, and restored exactly afterwards, whatever happens.

    `SSSF_SUPERVISOR_ITEM` is managed even for a factory-wide incident: a
    stale value inherited from an earlier run in this same environment
    would otherwise bind the item-less incident to whatever item that was,
    and the request command would happily record an action against it. The
    key is REMOVED for an item-less incident, and the previous value (or its
    absence) is restored on exit.
    """
    names = (
        item_actions.ENV_ITEM,
        item_actions.ENV_KIND,
        item_actions.ENV_SIGNATURE,
        item_actions.ENV_DIAGNOSIS,
    )
    values = {
        item_actions.ENV_KIND: kind,
        item_actions.ENV_SIGNATURE: signature,
        item_actions.ENV_DIAGNOSIS: adw_id,
    }
    if item is not None:
        values[item_actions.ENV_ITEM] = str(item)
    saved = {name: os.environ.get(name) for name in names}
    os.environ.update(values)
    if item is None:
        os.environ.pop(item_actions.ENV_ITEM, None)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def print_incident(args: argparse.Namespace, observed_at: str) -> None:
    """The incident block a person reads at the top of the run log."""
    item = f"#{args.item}" if args.item is not None else "none"
    print(f"supervisor diagnosis: kind={args.kind} item={item}")
    print(f"signature={args.signature} observed_at={observed_at}")


def print_diagnosis(envelope) -> None:
    """The complete diagnosis, printed so the log is the canonical output."""
    print()
    print("=" * 72)
    print("DIAGNOSIS")
    print("=" * 72)
    print(f"summary: {envelope.summary}")
    print()
    print(envelope.notes_for_next_agent or "(the agent returned no diagnosis text)")
    print("=" * 72)


def print_item_actions(config_rel: str, adw_id: str, signature: str) -> None:
    """Every action record THIS incident reached, printed in its own block.

    Matched on the diagnosis run id AND the incident signature: a run id can
    be reused by a later incident, and printing that incident's records into
    this run's log would attribute one incident's actions to another. Printed
    explicitly rather than relying on incidental tool rendering, so the run
    log carries the canonical sentence whatever the console did -- and printed
    on the failure path too, because a tool that succeeded before the
    envelope failed still produced a record a person must see. The records
    are the handoff to the next supervisor poll.
    """
    print()
    print("=" * 72)
    print("ITEM ACTIONS")
    print("=" * 72)
    try:
        paths = FactoryPaths(config_rel=config_rel)
        records = item_actions.records_for_diagnosis(
            paths.item_action_dir, adw_id, signature
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"(the action records could not be read: {error})")
        print("=" * 72)
        return
    if not records:
        print("(no item action was reached)")
    for record, _path in records:
        print(item_actions.render(record))
        print()
    print("=" * 72)


def run_diagnosis(args: argparse.Namespace) -> int:
    observed_at = normalize_observed_at(args.observed_at) or ""
    prompt = build_prompt(
        args.kind, args.item, args.signature, args.reason, observed_at
    )
    print_incident(args, observed_at)
    cfg, repo_root = session.bootstrap(args.config, REQUIRED_AGENTS)
    configure_read_only_seat(cfg, repo_root)
    # No --base: worktrees are disabled for this run, so the agent works in
    # the existing checkout and no branch is ever created or deleted.
    run = session.ensure(cfg, args.adw_id, repo_root=repo_root, prompt=prompt)

    with run.phase(
        PhaseParams(
            name="request",
            kind="engineer",
            owner=run.engineer,
            description="Capture the one signal incident being diagnosed",
        )
    ) as ph:
        ph.log(input=prompt)

    envelope = None
    try:
        with bound_incident_context(args.kind, args.item, args.signature, run.adw_id):
            with run.phase(
                PhaseParams(
                    name="diagnose",
                    kind="agent",
                    owner="scout",
                    description=(
                        "Work out what is actually wrong with this one incident, "
                        "read-only"
                    ),
                )
            ) as ph:
                # Exactly one agent call, then the run ends: no loop, no second
                # pass, no watcher. artifacts_exist keeps the empty-artifacts
                # claim honest; diagnosis_complete keeps a silent success from
                # passing as one.
                envelope = ph.call(
                    AgentCall(
                        output_type=GenericOutput,
                        prompt=prompt,
                        gates=[gates.artifacts_exist, diagnosis_complete],
                    )
                )
    except BaseException:
        # A tool may have recorded an action before the envelope failed; the
        # record outlives the failure and must reach the log either way.
        print_item_actions(args.config, run.adw_id, args.signature)
        raise

    code = run.finish()
    print_diagnosis(envelope)
    print_item_actions(args.config, run.adw_id, args.signature)
    return code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_diagnosis(args)
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - the log is the only witness
        # The run log is the only reader of a detached failure; a bare
        # traceback there is acceptable, a silent exit 0 is not.
        with suppress(Exception):
            print_diagnosis(_FailedDiagnosis(error))
        print(f"diagnosis run failed: {error}", file=sys.stderr)
        return 1


class _FailedDiagnosis:
    """The shape print_diagnosis needs when the agent call never returned."""

    def __init__(self, error: BaseException):
        self.summary = f"the diagnosis run failed: {error}"
        self.notes_for_next_agent = (
            "The agent call did not complete, so there is no diagnosis. "
            "Check the run trace and the errors above before re-waking it."
        )


if __name__ == "__main__":
    sys.exit(main())
