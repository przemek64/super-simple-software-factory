# Orchestration — design notes

How the unattended layer above workflows is meant to work, and why. Terms are in
[GLOSSARY.md](../GLOSSARY.md); the decisions that were genuine trade-offs are in
[docs/adr/](./adr/) 0002–0005. This file is the connective tissue: the shape of a
tick, the limits, and the first deployment it was designed against.

**The code lives in another repository.** `adws_factory/` is in `labeltool-test2`;
the workflows it launches are that repository's stamped `adws/`. This file is
authoritative for *intent*. Where it and the code disagree, the disagreement is a
finding, not something to resolve quietly in the code's favour — the concurrent
reader/writer pair sat unimplemented for exactly that reason, described here and
absent there, for long enough that reading the code alone taught the wrong model.
`docs/adws-factory-implementation-report.md` is a point-in-time record of one build
session; read it as history, not as current truth — its note that "`tick` launches at
most one run" is no longer true.

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

`select` yields **pairs**, plural, and `launch` starts as many as the limits allow.
In practice that is the review stage and the fix stage together — see
[Concurrency](#concurrency-what-is-meant-and-what-is-built).

Position is never remembered between ticks. `current_position` walks the stage
sequence from the start and returns the first stage whose artifact is missing, so
a fix that moves the head pulls the item back to the earliest stage that head
invalidated, automatically and without any stored cursor. This is the mechanical
consequence of [ADR-0003](./adr/0003-progress-is-read-from-artifacts.md) and it is
the single behaviour that most surprises a reader of the logs: a stage running for
the second time is normal convergence, not a retry.

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

## Concurrency: what is meant, and what is built

This is the part of the design most easily misread, and the code currently
disagrees with it. Read both halves before changing either.

**What "many reader runs, one writer run" is for.** The review stage and the fix
stage are meant to be *in flight together on the same head*. The fix workflow does
not begin by working; it begins by waiting for reviews to arrive. Starting it
alongside the reviewer turns that wait into overlap instead of dead time. The
limits above are sized for exactly one such pair — one reader plus one writer, on
one item.

There is no race in that pair, and the reason is worth stating because it is not
obvious: the writer cannot move the head before the reader has published, because
the writer's own first step is to block until the reader's review exists. See
[The external reviewer](#the-external-reviewer) for that contract.

**How it is built.** `launch_stage` enforces both class limits per launch —
`max_writer_runs` against `state.writer_runs()`, `max_reader_runs` against
`state.reader_runs()` — and refuses a launch that would exceed either. That half was
always complete. What was missing was anything that ever *proposed* a second launch:
`current_position` reported only the first unsatisfied stage and `select` returned a
single pair, so the limits guarded a situation that could not arise.

`unsatisfied_stages` now reports the whole set and `select` returns every stage that
can run for the chosen item, in sequence order. Two constraints shape it:

- **Selection is still item-level.** An item with any run in flight is skipped
  entirely. The pair is launched together from an idle item in one tick, so it never
  needs to join a run already going — and letting it would put a reader against a
  head an in-flight writer is still moving.
- **A stage needing a pull request is omitted while there is none**, so an item
  before its first build selects `p1` alone rather than erroring on a missing target.

**What the serialised version cost**, for the record: the fix stage could not start
until the review stage had finished and a further tick had run, and the review stage
was started against a head the external reviewer had not seen. On PR #99 the fix
stage pushed at 22:28, the review stage was launched immediately and spent five
minutes reviewing a head the external reviewer knew nothing about, and the decider
then had to ask for that review anyway — see
[ADR-0005](./adr/0005-the-factory-asks-the-external-reviewer-to-look-again.md), which
is why the request now goes out the moment a writer moves the head.

## The external reviewer

The external reviewer (CodeRabbit) is an input to the fix stage, not an artifact of
any stage, and it does not behave like the in-house reviewer. Two properties drive
most of the surrounding machinery:

**It is incremental, and will not re-review a commit it considers covered.** After a
push it frequently says nothing at all. Observed on PR #99: it reviewed the first
head unprompted, then stayed silent through a pushed fix until it was explicitly
asked, and answered that request in six minutes. So a rule that waits for a review
pinned to the current head waits forever on any such commit. The factory must *ask*
— posting `@coderabbitai full review` naming the new revision — and only a full
review will do, because the incremental form looks only at what came after and will
not revisit earlier findings.

**It is not deterministic.** The same commits reviewed twice produced eight findings
and four, overlapping on two. The fix workflow therefore freezes one review as a
contract, records its review id, and refuses to process that id twice. Re-reading is
not a refresh; it is a different opinion, and a loop that chases it never converges.

**The fix stage waits for both reviewers.** `coderabbit.await_reviews()` returns only
once the external review *and* the in-house review are both present on the head, then
freezes them as one ruling set — "both or nothing, deliberately", because triaging one
without the other produces a fix run that answers half the question and reports itself
finished. It polls with a bounded timeout (900 s default) and ends the run cleanly with
nothing recorded if either never arrives. This waiting contract is what makes the
concurrent pair above safe, and it is the fact most likely to be missed by someone
reading only `adws_factory/`: it lives in `adws/adw_modules/coderabbit.py`.

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

**"Nothing worth fixing" is not a signal anyone sends.** It is the absence of a
push. When triage refuses every finding, the fix workflow records the review as
handled and returns without committing. The head therefore does not move, every
review artifact stays pinned to it, the next tick finds all stages done, and the
decider runs. Convergence is detected by the same artifact probes as everything
else — there is no separate "clean round" flag to look for.

**A round is counted on the last stage completing**, in `reap`, against the
artifact rather than the run's status. Two rounds is therefore tight: the first
covers the initial review and the first fix, the second covers verifying that fix.
An item whose verification pass finds a regression has no round left to repair it
and escalates one pass short. If the escalation rate is dominated by that pattern,
the cap is the thing to raise, not the reviewer to soften.

## First deployment

Built against `labeltool-test2`, whose stamped `adws/` provides the workflows:

| Stage | Workflow | Class | Waits for | Artifact |
|---|---|---|---|---|
| p1 build | `adw_simple_sdlc.py --issue N` | writer | nothing | open pull request on the run's branch |
| p2 review | `adw_pr_review_2axis_m3.py --pr N` | reader | nothing | published review pinned to the head revision |
| p3 fix | `adw_coderabbit.py --pr N` | writer | **p2's review and the external review, both on the head** | ledger entry naming the review it consumed |
| decide | the decider | — | every stage done | merged pull request, closed item |

The "waits for" column is the whole basis of the concurrent pair: p3 can be
launched at the same moment as p2 because waiting is its first action.

The three artifact probes are not symmetrical, and the difference matters when
reading a tick log:

| Stage | Probe | When its input is missing |
|---|---|---|
| p1 | is there an open pull request? | — |
| p2 | is there an in-house review **pinned to the head**? | not done → p2 launches |
| p3 | is the newest external review **pinned to the head** already recorded as handled? | **done → p3 is skipped**, not blocked |

p3 reporting done can therefore mean either "the review was consumed" or "there is
no review for this head at all". Treating the second as *not done* would hang
forever, since the external reviewer may never volunteer one — so the probe passes
and the freshness question moves to the decider, which unlike a probe can act on it
by requesting a review. A tick log line reading "all stages done → decider" does not
imply the current head was externally reviewed.

The ledger is a plain JSON file, `adws/adw_runtime/coderabbit_processed.json`, keyed
`<repo>#<pr>#<review_id>`. Nothing accounting is implied by the name; it is a list of
review ids already handled. Keying by review rather than by pull request is what makes
a second review on the same pull request read as unconsumed work.

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
