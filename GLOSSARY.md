# Glossary

The language of the factory. Terms only — no implementation details, no decisions.
Decisions live in `docs/adr/`.

## Workflow

One end-to-end sequence of work the factory can perform, such as taking a request
all the way to an opened pull request. A workflow is a named, fixed chain of
phases; it is not a single agent and not a single run.

## Run

One execution of a workflow, identified by its ADW id. A run has its own branch,
its own worktree, and its own trace of phases. Two runs of the same workflow are
independent.

## Phase

One step inside a run — a unit of work with a name, an owner, and a status. A
phase is either performed by an agent or by code.

## Agent

A coding-agent persona with its own model, purpose, and permissions, that owns
agent phases.

## Launcher

One startable entry offered by the dashboard: a workflow together with a fixed
set of parameters. Two launchers may name the same workflow with different
parameters, and each is a separate entry with its own label.

## Dashboard

The screen that answers "what can I start, and how do I start it". It lists
launchers and starts runs, and shows only enough about runs in progress to keep
you from starting one twice. Everything else about a run belongs to the session
view.

## Session view

The screen that answers "what is happening in this run". It shows a run's phases,
spend, and events as they arrive.

## Workflow diagram

A fixed picture of a workflow's phase chain, shown beside its launcher for
reference. It describes the workflow, never the state of any particular run — a
run's actual progress is only ever shown in the session view.

## Orchestration

The unattended layer above workflows: it decides which run to start next, watches
for it to finish, and carries an item of work forward without a person driving
each step. Runs and workflows exist with or without it.

## Tick

One pass of the orchestration layer: read the world, judge what finished, start
at most what the limits allow, and stop. A tick is short-lived and holds no state
of its own between passes.

## Stage

One position in the ordered sequence an item of work passes through, bound to the
workflow that advances it. Stages are named and ordered; the sequence is data, not
code.

## Artifact

The durable thing a stage leaves behind that proves it happened — a pull request,
a published review, a ledger entry. An artifact outlives the run that made it, and
is the evidence the orchestration layer trusts about progress.

## Reap

Judging finished work at the start of a tick: for each run in flight, decide
whether its stage is now done, failed, or still going, and record it.

## Round

One review-then-fix pass over a single revision of the work. A later round judges
a different revision, so findings from an earlier round say nothing about the
current one.

## Writer run

A run that commits to the branch under review. Two writer runs on one branch
contend for the same history, so they are never in flight together.

## Reader run

A run that inspects the work and publishes an opinion without committing to the
branch. Reader runs do not contend with each other.

## Decider

The judgement layer that acts where a person otherwise would: it verifies what
the reviews claim against the real code, accepts or dismisses their findings, and
either concludes the work or asks for a human. It never generates the work it
judges.

## Blocked

The state of an item the orchestration layer has stopped attempting, because a
budget was spent without producing the artifact. Blocked is a request for a
person; it is never cleared by another attempt.
