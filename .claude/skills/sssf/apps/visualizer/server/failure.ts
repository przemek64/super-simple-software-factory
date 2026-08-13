/**
 * Why a run failed, decided from what the tracer already wrote.
 *
 * Everything needed is in the db: `phases.error` holds the text that killed the
 * phase, `gate_results.violations_json` holds what a gate refused. The dashboard
 * simply never asked. This turns those rows into one line a human can act on.
 *
 * It is a rule table, not a model. Every failure seen so far is an exact string
 * the ADW itself formats — the guards, the snapshot budget and the pre-commit
 * hook all print a fixed prefix — so matching them is a regex, and a regex is
 * free, instant, and gives the same answer twice. The value is not in naming the
 * failure (the error text already does that) but in the `hint`: for a class we
 * recognise we know the repair, and "run `git worktree prune`" is the difference
 * between a red card and a restarted run.
 *
 * Rules are ordered and first-match-wins. Anything unmatched falls back to the
 * raw error verbatim under kind "error" — never swallowed, because an
 * unrecognised failure is exactly the one worth reading. `unknown` is reserved
 * for a run that failed with no error text at all, which is a tracer gap rather
 * than a run problem.
 */
import type { FailureKind, FailureReason, GateResult, Phase, Session } from "../shared/types.ts";

interface Rule {
  kind: FailureKind;
  match: RegExp;
  /** `m` is the match; `paths` are the "  - <path>" lines under the message. */
  headline: (m: RegExpMatchArray, paths: string[]) => string;
  hint: string | null;
}

/**
 * The guards report every path they saw on its own line, prefixed "  - " and
 * suffixed with a separator and a verb ("→ deleted"). That separator is written
 * as an arrow but reaches the db however the run's console encoded it — an
 * em dash, a replacement char, mojibake — so it is matched by anchoring on the
 * VERB at end of line and dropping whatever non-word run precedes it. The verb
 * list is closed and short; a line without one keeps its whole body.
 */
function violationPaths(error: string): string[] {
  const paths: string[] = [];
  for (const line of error.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("- ")) continue;
    const body = trimmed
      .slice(2)
      .replace(/\s+\W*\s*(deleted|added|created|modified|changed)\s*$/i, "");
    const leaf = body.trim().replace(/[\\/]+$/, "").split(/[\\/]/).pop();
    if (leaf) paths.push(leaf);
  }
  return paths;
}

/** "launchers.yaml, diagrams +2 more" — the names, not the absolute paths. */
function namePaths(paths: string[], show = 2): string {
  if (paths.length === 0) return "no paths reported";
  const head = paths.slice(0, show).join(", ");
  const rest = paths.length - show;
  return rest > 0 ? `${head} +${rest} more` : head;
}

function bytesGiB(value: string): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n / 1024 ** 3).toFixed(2)} GiB` : value;
}

const RULES: Rule[] = [
  {
    // Git still lists a worktree whose directory is gone, so the safety snapshot
    // walks a path that does not exist. Seconds into the run, never later.
    kind: "stale_worktree",
    match: /\[WinError 267\]|The directory name is invalid/,
    headline: () => "stale worktree — snapshot walked a directory git lists but disk lacks",
    hint: "run `git worktree prune`, then relaunch",
  },
  {
    kind: "snapshot_budget",
    match: /permission snapshot byte limit exceeded \((\d+)>(\d+)\)/,
    headline: (m) => `snapshot budget exceeded — ${bytesGiB(m[1]!)} of ${bytesGiB(m[2]!)}`,
    hint: "prune old worktrees (~400 MB each), or raise permissions max_bytes",
  },
  {
    // Written by adw_kill.py's reconcile(), and by session.ensure's sweep, onto
    // any phase left "running" by a process that never reached its finalizer.
    // It is a record of the cleanup, not of the cause — say so, or it reads as
    // if the kill were the failure.
    kind: "hard_kill",
    match: /process died without finalizing/,
    headline: () => "process died or was killed before it could finalize",
    hint: "the cause is not in the trace — read the launch log for this adw_id",
  },
  {
    kind: "precommit_hook",
    match: /PRE-COMMIT BLOCK: (.+)/,
    headline: (m) => `pre-commit hook refused the commit — ${(m[1] ?? "").trim()}`,
    hint: "the hook prints its own repair steps in the full error",
  },
  {
    kind: "guard_canonical",
    match: /agent modified canonical or sibling checkout state/,
    headline: (_m, paths) => `guard: canonical checkout changed — ${namePaths(paths)}`,
    // Every instance of this so far was a human or a tool writing to the repo
    // during the run, not the agent. Say so, because the error does not.
    hint: "was anything else writing to the repo? never edit the checkout while a run is live",
  },
  {
    kind: "guard_allowlist",
    match: /^(\S+) is limited to \[(.*?)\] but modified (\d+) path/m,
    headline: (m, paths) => `guard: ${m[1]} wrote outside ${m[2] || "its allowlist"} — ${namePaths(paths)}`,
    hint: "if the path is gitignored state, it belongs in the guard's exclusions",
  },
  {
    kind: "guard_readonly",
    match: /^(\S+) is read-only but modified (\d+) path/m,
    headline: (m, paths) => `guard: read-only ${m[1]} wrote ${m[2]} path(s) — ${namePaths(paths)}`,
    hint: "if the path is gitignored state, it belongs in the guard's exclusions",
  },
  {
    kind: "test_gate",
    match: /No module named|ModuleNotFoundError|error during collection|collected 0 items/,
    headline: () => "test gate could not collect — the run never measured its own regression",
    hint: "verify the test command from INSIDE the worktree; `uv run --with` lacks the repo's deps",
  },
  {
    kind: "timeout",
    match: /\b(timed out|timeout|TimeoutError|deadline exceeded)\b/i,
    headline: () => "timed out",
    hint: null,
  },
];

/** First line of a message, trimmed and clamped — a card gets one line. */
function firstLine(text: string, max = 140): string {
  const line = (text.split("\n")[0] ?? "").trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function classifyError(error: string): { kind: FailureKind; headline: string; hint: string | null } {
  const paths = violationPaths(error);
  for (const rule of RULES) {
    const m = error.match(rule.match);
    if (m) return { kind: rule.kind, headline: rule.headline(m, paths), hint: rule.hint };
  }
  return { kind: "error", headline: firstLine(error), hint: null };
}

/**
 * The one line the workflows view shows, or null when there is nothing wrong.
 *
 * Deliberately not keyed off `sessions.status`. A run that dies hard leaves the
 * session row saying "running" forever while a phase row records the real
 * failure — the status column is written by an exit path that a crash skips, so
 * trusting it would hide precisely the failures worth surfacing. The phases are
 * the evidence; the status is a summary that may never have been written.
 */
export function classifyFailure(
  session: Session,
  phases: Phase[],
  gates: GateResult[] = [],
): FailureReason | null {
  const ordered = phases.toSorted((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  const failed = ordered.filter((p) => p.status === "fail");
  const last = failed[failed.length - 1];

  const at = (phase: Phase | undefined): Pick<FailureReason, "phase" | "phase_seq" | "phase_count"> => ({
    phase: phase?.name ?? null,
    phase_seq: phase?.seq ?? null,
    phase_count: ordered.length,
  });

  if (last) {
    const error = (last.error ?? "").trim();
    if (error) {
      return { ...classifyError(error), detail: error, ...at(last) };
    }
    // A phase can fail on a gate that wrote its verdict to gate_results and
    // left phases.error empty, so the gate rows are the fallback evidence.
    const gate = gates.find((g) => g.phase_id === last.phase_id && g.passed === 0);
    if (gate) {
      const violations = parseViolations(gate.violations_json);
      return {
        kind: "gate",
        headline: `gate "${gate.gate ?? "unnamed"}" failed — ${
          violations.length ? firstLine(violations[0]!) : "no violations recorded"
        }`,
        detail: violations.join("\n") || null,
        hint: violations.length > 1 ? `${violations.length} violations in total` : null,
        ...at(last),
      };
    }
    return {
      kind: "unknown",
      headline: `phase "${last.name ?? "?"}" failed with no error recorded`,
      detail: null,
      hint: null,
      ...at(last),
    };
  }

  // No failed phase. Two remaining shapes: a session that declared failure
  // elsewhere, and one that stopped without ever declaring anything.
  if (session.status === "fail") {
    return {
      kind: "unknown",
      headline: "run reported failure with no failed phase",
      detail: null,
      hint: "the failure happened outside a traced phase — check the launch log",
      phase: null,
      phase_seq: null,
      phase_count: ordered.length,
    };
  }

  if (session.status === "running" && session.ended_at) {
    return {
      kind: "abandoned",
      headline: "run ended without recording a status",
      detail: null,
      hint: "the process died between its last phase and its exit path",
      phase: ordered[ordered.length - 1]?.name ?? null,
      phase_seq: ordered[ordered.length - 1]?.seq ?? null,
      phase_count: ordered.length,
    };
  }

  return null;
}

/** violations_json is a JSON array of strings; an older or truncated row is not. */
function parseViolations(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map((v) => String(v)) : [];
  } catch {
    return [];
  }
}
