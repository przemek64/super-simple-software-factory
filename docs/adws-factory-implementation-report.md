# adws_factory — implementation report

**Session:** 2026-08-15 → 2026-08-16
**Built in:** `C:\dev\projects\labeltool-test2`, branch `sync/adws-from-labeltool-2026-08-14`
**Design docs:** this repo, branch `worktree-isolation-port`

Written so the next session can pick up without re-deriving anything. It records
what was built, every problem hit and what caused it, and what is still open.
Where a decision looks over-careful, the reason is usually a specific failure
recorded below.

---

## 1. Where things stand

The orchestration layer exists, is committed, has 168 offline tests, and has
launched real runs. The decider rules on finished items but cannot yet verify a
blocking finding, so it escalates instead.

**As of the end of the session a live run is in flight**: `a62ee3c7`, stage p3
(`adw_coderabbit.py`) on issue #4 / PR #92, launched 2026-08-16T01:45Z. Its
outcome is not in this report.

### Commits

| Repo | Commit | What |
|---|---|---|
| labeltool-test2 | `b4e5ce0` | operator env + interpreter resolution on Windows |
| labeltool-test2 | `2b28319` | `adws_factory`, 12 modules |
| labeltool-test2 | `de7ba5b` | tests for the side-effecting paths |
| labeltool-test2 | `257b92b` | tests against real processes and real failures |
| labeltool-test2 | `d423ace` | label / path / CLI tests, time-bomb fixture fix |
| labeltool-test2 | `eb2297c` | raw stream cap + retention on landed artifact |
| labeltool-test2 | `498698d` | `CREATE_NO_WINDOW` launch flag |
| super-simple-software-factory | `1fae065` | orchestration.md, ADR-0002/3/4, glossary |

Nothing is pushed. Both branches are local.

### Modules

```
adws_factory/
  paths.py        data_dir read from the SSSF config, never hardcoded
  config.py       stage sequence as data, limits, label vocabulary
  state.py        atomic-write state in the guard's excluded directory
  run_records.py  read-only tracer queries + failure classification
  gates.py        disk floor + credit gate, both fail-closed
  artifacts.py    the three stage probes, all revision-pinned
  labels.py       ensure-exists, projection, FIFO eligibility
  launcher.py     detached spawn with a factory-chosen adw_id
  tick.py         reap -> enforce budgets -> select -> launch -> decide
  decider.py      merge and close, or escalate
  escalate.py     Signal via the signal-agent daemon
  __main__.py     status | plan | tick | loop
```

---

## 2. Problems hit, in order

Each of these cost real time. Several were only found because a number
disagreed with another number by one.

### 2.1 The disk was full, and it did not look like a disk problem

`tests/test_external_cwd_entrypoint.py` failed with `OSError: [Errno 28] No
space left on device` buried inside a subprocess assertion. Read as a code bug.

Cause: the permission guard snapshots the whole checkout around every agent
phase, and the run store (`adws/adw_runtime/sessions/`) lives inside the
checkout. One observed snapshot in `%TEMP%\sssf-guard-*` was **5.5 GB**, almost
all of it `raw_output.jsonl`. The checkout measured 6.1 GB against 12.95 GB
free — two snapshots from a full disk.

Fixed two ways in `eb2297c`:

- `agent_pi.py` now writes through a bounded writer: full fidelity to 256 MB,
  then the file stops growing and the last 32 MB are held in memory and appended
  at close behind a `sssf_truncation` marker. Bounding *while writing* means a
  looping agent cannot fill the disk at all.
- The tick drops a run's `raw_output.jsonl` once its artifact has landed.
  `events.jsonl`, envelopes, prompts and the context handoff are all kept.

Pruning the existing store took the checkout from **6.1 GB to 616 MB**, so every
future snapshot costs a tenth of what it did.

**Watch for:** the guard leaks its snapshots. 73 orphaned `sssf-guard-*`
directories were found in `%TEMP%`, dating back ten days. Mostly tiny, but the
mechanism that produced one 5.5 GB directory has no cleanup.

### 2.2 `\b` does not match `_Major_`

The decider read a 12,000-character CodeRabbit review as containing **nothing**
and returned `merged`.

Cause: the external reviewer emits its labels as markdown emphasis —
`_⚠️ Potential issue_ | _🔴 Major_` — and underscore is a word character, so a
`\b`-bounded pattern never fires. Found only because a `grep -c` said 1 and the
parser said 0.

Fixed with letter-only lookarounds. This was a merge-past-a-blocker bug, not a
cosmetic miss.

### 2.3 The findings were not in the review body

After 2.2 the decider found 1 blocking finding. The real number was 4.

Cause: the external reviewer puts its substantive findings in **inline review
comments**, not the body — 6 comments, ~21,000 characters, none of which were
being read. `artifacts.review_comments()` now reads them (paginated), and
replies are skipped so one objection is not counted several times.

Final reading of PR #92: 18 findings, 4 blocking, across two review bodies and
six inline comments.

### 2.4 A dry run that mutated the world

`plan` created 7 labels on GitHub. Fixed: `plan` now reports
`would create labels: …`.

### 2.5 A test fixture with a one-hour shelf life

Six credit-gate tests passed when written and failed 62 minutes later. First
read was cross-file pollution; it was not.

Cause: the fixture hardcoded `updated_at`, and the credit gate has a 60-minute
freshness bound. Fixed to stamp current time. The one test that needs a stale
file still pins an explicit 2020 date, which is stable by construction.

**Lesson worth keeping:** per-file green says nothing about suite green. This
only surfaced when the files were run together.

### 2.6 Editing the checkout during a live run killed it

Run `106602c5` died in triage with:

```
PermissionBreach: agent modified canonical or sibling checkout state:
  - adws_factory\launcher.py — restored exactly
```

Cause: a file was edited while the run was live. The guard snapshots around
every agent phase and cannot tell who wrote a file, so it attributed the edit to
the agent, restored the file — reverting the fix — and failed the phase.

**The discipline that works:** commit, verify a clean tree, then launch, then do
not touch that checkout until the run finishes. Check first with:

```powershell
Get-CimInstance Win32_Process | Where-Object {
  $_.Name -match 'python|uv' -and $_.CommandLine -match 'adws[/\\]adw_.*\.py' }
```

Note that a naive `*adw_*` command-line match is useless — it matches `tail -f`
log watchers and the matching command itself.

### 2.7 A console window per tool call

The first live run had 39 `conhost.exe` processes alive at once, flashing
windows on the desktop.

Cause: the launcher used `DETACHED_PROCESS`, which leaves the child with **no
console at all**, so every `bash` and `node` pi spawns for a tool call finds
nothing to inherit and allocates its own.

Changed to `CREATE_NO_WINDOW` in `498698d`. **Not yet confirmed fixed** — the
`conhost` count was unchanged after relaunch, and the windows come from pi's
grandchildren rather than the process whose flags we control. Needs a human to
say whether windows still appear.

---

## 3. Findings that are not ours to fix

Recorded because they will look like factory bugs from the outside.

- **`data_dir` is not excluded from the guard's snapshot.** ADR-0004 assumes it
  is. The snapshot at `%TEMP%\sssf-guard-o7qc1971\adws\adw_runtime\sessions\…`
  proves otherwise. Harmless for factory state, but it is why every snapshot was
  ten times larger than necessary.
- **A run can fail leaving no account of why.** Run `302b6e3d` recorded status
  `fail` with no phase error and no gate result. ADR-0003 assumes run status
  explains a failure; here nothing did.
- **The guard never cleans up its snapshots** (see 2.1).

---

## 4. Known gaps

**The decider cannot verify a blocking finding.** ADR-0002 wants it to check
each blocker against the code and dismiss the false ones. It escalates instead.
Fail-safe — it never merges something unverified — but it means a false blocker
stalls the line and waits for a human, which is the stalling ADR-0002 wanted
automated away. This is the largest remaining piece of work.

**`PermissionBreach` classifies as `unknown`.** The outcome is currently right
(unknown is treated as infrastructure, so no work budget is spent), but by
fallback rather than recognition. Genuinely ambiguous: a breach caused by a
human editing the tree is infrastructure, while an agent writing out of bounds
is a **work** failure, and the recorded message is identical. Suggested
resolution: treat it as work, and accept that human interference occasionally
costs an attempt.

**`cmd_loop` is untested.** It is an unbounded event loop; a test that breaks
out of it tests the break. Left deliberately uncovered rather than faked.

**`run_records` SQLite reads are only covered through synthetic objects**, never
a real database file.

**No end-to-end test.** 168 tests cover the parts in isolation and under fakes.
None of them prove the factory carries an item from p1 to a merge.

**No `--item` override.** There is no way to force a specific issue; selection
is FIFO over the entry label. This mattered once already, when a stray
`factory-done` label silently removed the intended item from the queue.

---

## 5. Operating notes

- Entry point: `python -m adws_factory {status|plan|tick|loop}` from the repo
  root. `plan` is side-effect free; `tick` launches at most one run.
- State: `adws/adw_runtime/factory/state.json`, with `tick.log` and
  `logs/<stage>_<target>_<adw_id>.log` beside it. All untracked, all inside the
  guard's exclusion. Verified: `adws/adw_runtime/` has **zero** tracked files.
- Stop switch: add `factory-hold` to an issue, or delete `approved-for-dev`.
- `blocked: <stage>` is cleared **only** by a person deleting the label.
- Credit gate reads `C:\dev\projects\grey-factory\credits.json` (remaining-%
  semantics). `status.json` in ai-usage-monitor is *used*-%, not remaining —
  do not read that one by mistake. `kimi` was `stale` all session; no stage in
  the current pipeline uses it, but any stage that gains a Kimi seat will be
  refused until its reader is fixed.
- MiniMax is unmetered by choice, so p2 (both seats MiniMax) never gates on
  quota.
- Disk floor is **7 GB**, warn at 12. Deliberately thinner than the worst case:
  a single snapshot was 5.5 GB, so one unlucky run can still cross the floor
  from above. Raise it once §3's `data_dir` exclusion is fixed.

---

## 6. Suggested next steps

1. Land the in-flight `a62ee3c7` and read what it did — the first genuine
   end-to-end exercise of p3.
2. Confirm or fix the console-window problem (2.7).
3. Build the decider's verification agent (§4). Largest remaining piece.
4. Close the `PermissionBreach` classification gap (§4).
5. File the three SSSF issues in §3, starting with the `data_dir` exclusion —
   it is one line of blast radius and a 10x disk saving.
6. Add `--item` to the CLI so a specific issue can be forced.
