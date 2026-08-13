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
