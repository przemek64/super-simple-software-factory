# Orchestration state lives outside the tree the factory monitors

A run guards the repository by snapshotting the checkout around every agent
phase and rolling back writes it did not authorise. That guard cannot tell who
wrote a file — only that it changed. The orchestration layer writes constantly
while runs are in flight: which run is tracked, what has been attempted, when the
last tick fired. We decided all of that mutable state lives in the runtime
directory the guard already excludes, alongside the trace database, while the
orchestration code itself stays versioned in the repository like any other
source.

## Consequences

- The runtime directory must contain no tracked file, or the exclusion refuses to
  apply at all. State written there is deliberately outside version control.
- The orchestration code is read at launch and never written at runtime, so it can
  live safely in the monitored tree and be reviewed like the rest of the factory.
- Anyone tidying the state file into the repository "where it belongs" will
  silently reintroduce the failure: a live phase attributes the write to the
  agent, restores the tree, and fails the phase. The same mechanism has already
  eaten hand-written work, twice.
- A person editing the checkout while a run is in flight hits exactly the same
  guard. This is a property of the factory, not of orchestration, and the answer
  is the same: wait, or work in a copy.

## Considered alternatives

Putting the whole orchestration layer outside the repository, as an independent
tool driving it from a distance; rejected because it would then drift from the
workflows it launches and stop being stamped into new repositories with them.
Excluding a second directory from the guard for orchestration's own use; rejected
as widening a safety boundary for convenience, when an already-excluded
directory exists.
