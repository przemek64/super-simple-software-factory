# Orchestration — design notes

How the unattended layer above workflows is meant to work, and why. Terms are in
[GLOSSARY.md](../GLOSSARY.md); the decisions that were genuine trade-offs are in
[docs/adr/](./adr/) 0002–0004. This file is the connective tissue: the shape of a
tick, the limits, and the first deployment it was designed against.

Ancestry: this is a port of `grey-factory`, an orchestrator built on a different
workflow engine (Archon) that drove a real repository for roughly two months. The
scheduling ideas are carried over; the execution layer is not, because SSSF runs
are local processes with a trace database rather than remote workflow runs. Where
a rule below looks over-careful, it is usually paying for a specific failure that
happened there.

## The tick

One pass, then exit:

```
reap      for each tracked run: artifact present? -> done
                                terminal, no artifact? -> classify the failure
                                still going? -> leave it
supervise any decision point for the active item -> wake the decider
select    eligible (item, next stage) pairs, filtered and ordered
launch    up to the limits, detached, then record and exit
```

Driven by an event loop rather than a fixed timer: while any run is active the
loop watches and does nothing; the moment nothing is running it ticks. A fixed
interval wastes the gap between a stage finishing and the next tick — on the
predecessor project a single item lost hours that way. One scheduled entry
restarts the loop after a reboot, and a tick lock keeps a manual run from
colliding with the loop.

## Limits

- **One item at a time.** The historical cause of lost work was several items in
  flight sharing branches and worktrees. With one, there is nothing to collide
  with.
- **Many reader runs, one writer run.** Reader runs publish opinions and do not
  contend. Writer runs commit to the branch, and two of those on one branch is
  the collision class again in miniature.
- **Agent phases are already serialised** by the factory's own execution lock, so
  concurrency buys overlap between waiting and working, not parallel agent work.
  Do not size limits as though it were parallelism.
- **Credit gate.** Before launching, read the roster the run will actually use and
  require every metered provider in it to be above threshold, with a freshness
  guard on the quota file. Deriving providers from the roster keeps the gate
  honest when a seat is swapped; a hand-maintained list per stage silently rots.
  The decider spends quota too, and is gated the same way.
- **Disk floor.** Worktrees accumulate and are not always cleaned; below the floor,
  launch nothing.

## Two budgets, because failures are not alike

An attempt counter that counts everything parks healthy items on infrastructure
noise. One that counts nothing loops forever on a broken environment. So:

| Class | Examples | Effect |
|---|---|---|
| Work | acceptance gate violated, findings untouched, suite genuinely red | spends the attempt budget; park **blocked** when exhausted |
| Infrastructure | permission guard refusal, missing test dependency, provider timeout or overload, worktree lock, disk floor | does not spend it |
| Either | every launch, whatever happened | spends a hard launch ceiling |

The classification is mechanical, read from the run's own recorded phase error and
gate results — not an LLM's opinion about its own failure.

Blocked is cleared by a person deleting the label, which resets the budget. It is
never cleared by another attempt.

## Rounds and convergence

A round is review-then-fix over one revision. Fixing changes the revision, which
by [ADR-0003](./adr/0003-progress-is-read-from-artifacts.md) makes the preceding
review artifact stale and pulls the item back a stage — so without a stopping rule
the pair cycles indefinitely, and a reviewer that always finds something will
always find something.

The rule: stop when a round produces nothing worth fixing, or after two rounds
regardless, and hand the current revision to the decider with the round history.

## First deployment

Built against `labeltool-test2`, whose stamped `adws/` provides the workflows:

| Stage | Workflow | Class | Artifact |
|---|---|---|---|
| p1 build | `adw_simple_sdlc.py --issue N` | writer | open pull request on the run's branch |
| p2 review | `adw_pr_review_2axis_m3.py --pr N` (plus the external reviewer) | reader | published review pinned to the head revision |
| p3 fix | `adw_coderabbit.py --pr N` | writer | ledger entry naming the review it consumed |
| decide | the decider | — | merged pull request, closed item |

Code lives in `adws_factory/` in that repository, state in the excluded runtime
directory per [ADR-0004](./adr/0004-orchestration-state-lives-outside-the-monitored-tree.md),
and the whole thing moves into the skill templates once it has taken one item end
to end.

Labels are minimal, and only the first three are inputs: an entry ticket, a hold
that skips an item entirely, a target branch, and then — written by the factory —
a cosmetic position, a blocked marker per stage, and a terminal marker. Any label
the factory writes must exist before it writes it; on the predecessor project a
missing label crashed the entire tick.

## Deliberately not carried over

The predecessor's automatic review-remediation gate, its plan-phase adjudicator,
and its recovered-failure hint label. The first discarded good work on false
findings and is superseded by the decider holding that authority; the second and
third answer questions this pipeline does not ask.
