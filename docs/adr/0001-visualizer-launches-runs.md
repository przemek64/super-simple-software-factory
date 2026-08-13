# The visualizer starts runs, it no longer only watches them

The visualizer was a read-only observer: it opened the run database and polled it,
and knew nothing about the repository the runs came from. Runs were started by
hand from a terminal. We decided the visualizer server may also **start** a run:
the dashboard posts a launcher and its parameters, and the server spawns the
workflow as a detached process in the target repository. Everything the run
produces still reaches the UI the old way, through the database — starting a run
is the only new direction of travel.

## Consequences

- The server needs the target repository, which it never had before. The start
  script passes it in alongside the database path, so the same shared visualizer
  code can serve whichever repository launched it.
- The server pins the run's id and passes it to the workflow instead of letting
  the workflow invent one. Without that the caller has no handle on what it just
  started, and a run that dies before writing to the database — a bad issue
  number, a missing tool — would be an invisible click.
- The spawned process has no console, so its output is captured to a log file
  under the run's own session directory.
- Only requests from the visualizer's own origin may start a run. The server
  listens on a local port that any page in the browser can reach, and starting a
  run now spends real money in a real repository.
- Starting the same issue twice is refused. Parallel runs on *different* issues
  stay allowed, because each run already gets its own worktree — the accident
  worth preventing is two agents on one issue, not concurrency itself.

## Considered alternatives

Writing a queue row for a separate long-lived runner to pick up, which survives
the visualizer restarting and would allow queueing several issues unattended;
rejected as more machinery than the way the factory is actually used, one
attended run at a time. Also rejected: having the dashboard merely hand over a
command to paste into a terminal, which is safe and useless.
