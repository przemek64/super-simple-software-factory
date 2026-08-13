# Dashboard — design note

The visualizer gains a second screen, the **dashboard**, that starts runs. The
existing sessions list and session view are unchanged in purpose: the dashboard
answers "what can I start", the session view answers "what is happening".

The decision to let the visualizer start runs at all, and its guard rails, is
recorded in [ADR 0001](adr/0001-visualizer-launches-runs.md). This note covers
the parts that are cheap to change.

## Screens and routing

The sessions list stays the front page. Routes become explicit sections so the
dashboard is not a reserved word inside the run-id namespace:

| Route | Screen |
|---|---|
| `#/` | sessions list (unchanged) |
| `#/runs/<adw_id>` | session view |
| `#/runs/<adw_id>/<phase_id>` | session view, phase panel open |
| `#/dashboard` | dashboard |

A header switch moves between them. The `-AdwId` deep link in
`start-visualizer.ps1` moves to the `#/runs/...` form.

## Launchers

A launcher is one workflow plus a fixed parameter set, declared per repository in
`adws/adw_sssf_config/launchers.yaml`. Only declared launchers appear — the
seventeen `adw_*.py` scripts are not auto-discovered. Two launchers may name the
same workflow with different parameters; each is its own row.

Sketch:

```yaml
launchers:
  - label: Resolve a GitHub issue
    workflow: adws/adw_simple_sdlc.py
    diagram: adws/adw_sssf_config/diagrams/simple_sdlc.svg
    params:
      - name: issue
        flag: --issue
        type: int
        label: Issue number
        required: true
```

Parameters are declared with their types even though the first launcher has only
one, so a second row with different parameters needs no schema change.

## A row

Left: the parameter inputs, labelled so the box says what it wants ("Issue
number"), and a Run button. Right: the workflow diagram.

The diagram is a static SVG, drawn once per workflow and stored per repository
under `adws/adw_sssf_config/diagrams/`. It shows the workflow's phase chain and
never any run's state — a picture in the dashboard is never a progress bar. The
cost accepted here is that the same diagram is copied into each repository by
hand; the installer is not involved.

Each row shows a count of its currently active runs, linking into the sessions
list. That is the only run state the dashboard displays, and it exists so a
launch button is not pressed twice for want of feedback.

## Starting a run

`POST /launch` takes a launcher id and its parameter values. The server:

1. rejects the request unless its `Origin` is the visualizer itself. A missing
   `Origin` is allowed, since a browser always sends one on a cross-site POST —
   its absence means a non-browser caller, which was never the threat;
2. refuses if an identical launch — same launcher, same parameter values — is
   already active. Different issues may run in parallel, since each run has its
   own worktree;
3. mints the run id and spawns the workflow detached in the repository given by
   `SSSF_REPO`, passing `--adw-id`, with output captured to a log under that
   run's session directory;
4. returns the run id, and the UI navigates to `#/runs/<adw_id>`.

There is no pre-flight validation of the issue or the environment. A run that
dies before it writes to the database is reported as failed-to-start, with the
tail of its captured log.

What was launched is recorded in `launches.json` beside the trace database. The
database cannot answer it: `sessions.request` holds the engineer's ask, not the
parameters it came from, so "is this already running" has no other home. A
launch counts as active while its session says `running`, and for a grace period
before its first insert — otherwise a double-click in the first seconds, the
likeliest double-click there is, would slip past the check.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /api/launchers` | the launcher list, read fresh from the config each time |
| `GET /api/launchers/:id/diagram` | the launcher's static SVG |
| `GET /api/launches` | launches this server started, with what became of each |
| `POST /api/launch` | start a run; returns the run id |

## What the start script must pass

`start-visualizer.ps1` already resolves the database from the repo's config. It
additionally exports the repository root, since the visualizer code is shared
from the skill repo and otherwise has no idea which checkout it is serving.
