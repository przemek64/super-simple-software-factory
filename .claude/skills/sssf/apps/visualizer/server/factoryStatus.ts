/**
 * The factory's item status — running | held | escalated | merged | parked —
 * derived on demand from adws_factory's own sources, never persisted here.
 *
 * Two stores have to agree (see docs the user already has): state.json
 * (adws/adw_runtime/factory/state.json, sibling of the trace db's "factory/"
 * dir) has blocked_stage; GitHub has the durable projection of that plus
 * factory-hold / factory-done; the PR itself has draft/merged. None of the
 * three alone is enough — decider.py's own verdict ("held" while a PR is a
 * draft or awaiting review) is the one case this intentionally does NOT
 * reproduce in full, since that would mean re-running review logic here. A
 * draft PR is enough to call it "held" for display purposes.
 *
 * One `gh` round trip per repo, cached briefly — a row-per-item UI polling
 * this would otherwise shell out to `gh` dozens of times a second. The
 * snapshot is also the source for the issues ledger below, so both features
 * share the same two `gh` calls instead of doubling them.
 */
import { dirname, join } from "node:path";

export type FactoryStatus = "running" | "held" | "escalated" | "merged" | "parked";

interface IssueInfo {
  number: number;
  title: string;
  state: string; // OPEN | CLOSED
  labels: Set<string>;
}

interface PrInfo {
  number: number;
  title: string;
  state: string; // OPEN | CLOSED | MERGED
  isDraft: boolean;
  body: string;
}

interface Snapshot {
  budgets: Record<string, { blocked_stage: string | null }>;
  issues: Map<number, IssueInfo>;
  prs: Map<number, PrInfo>;
  fetchedAt: number;
}

const TTL_MS = 15_000;
const cache = new Map<string, Promise<Snapshot>>();

async function ghJson(args: string[], cwd: string): Promise<unknown> {
  const proc = Bun.spawn(["gh", ...args], { cwd, stdout: "pipe", stderr: "pipe" });
  const [out, code] = await Promise.all([proc.stdout ? new Response(proc.stdout).text() : "", proc.exited]);
  if (code !== 0) return null;
  try {
    return JSON.parse(out);
  } catch {
    return null;
  }
}

async function fetchSnapshot(repoRoot: string, factoryStateFile: string): Promise<Snapshot> {
  const [stateText, issuesRaw, prsRaw] = await Promise.all([
    Bun.file(factoryStateFile)
      .text()
      .catch(() => null),
    ghJson(
      ["issue", "list", "--state", "all", "--limit", "500", "--json", "number,title,state,labels"],
      repoRoot,
    ),
    ghJson(
      ["pr", "list", "--state", "all", "--limit", "500", "--json", "number,title,state,isDraft,body"],
      repoRoot,
    ),
  ]);

  let budgets: Snapshot["budgets"] = {};
  if (stateText) {
    try {
      budgets = (JSON.parse(stateText).budgets ?? {}) as Snapshot["budgets"];
    } catch {
      /* corrupt state.json costs status accuracy, not the request */
    }
  }

  const issues = new Map<number, IssueInfo>();
  for (const entry of (issuesRaw as Array<{
    number: number;
    title: string;
    state: string;
    labels: { name: string }[];
  }>) ?? []) {
    issues.set(entry.number, {
      number: entry.number,
      title: entry.title,
      state: entry.state,
      labels: new Set(entry.labels.map((l) => l.name)),
    });
  }

  const prs = new Map<number, PrInfo>();
  for (const entry of (prsRaw as Array<{
    number: number;
    title: string;
    state: string;
    isDraft: boolean;
    body: string;
  }>) ?? []) {
    prs.set(entry.number, {
      number: entry.number,
      title: entry.title,
      state: entry.state,
      isDraft: entry.isDraft,
      body: entry.body ?? "",
    });
  }

  return { budgets, issues, prs, fetchedAt: Date.now() };
}

function snapshotFor(repoRoot: string, dbPath: string): Promise<Snapshot> {
  const cached = cache.get(repoRoot);
  if (cached) return cached;
  const factoryStateFile = join(dirname(dbPath), "factory", "state.json");
  const promise = fetchSnapshot(repoRoot, factoryStateFile);
  cache.set(repoRoot, promise);
  promise.then(
    () => setTimeout(() => cache.delete(repoRoot), TTL_MS),
    () => cache.delete(repoRoot),
  );
  return promise;
}

function statusOf(
  snap: Snapshot,
  issue: number,
  pr: number | null,
): FactoryStatus | null {
  const labels = snap.issues.get(issue)?.labels ?? new Set<string>();
  if (labels.has("factory-done")) return "merged";

  const budget = snap.budgets[String(issue)];
  if (budget?.blocked_stage) return "escalated";
  if (labels.has("factory-hold")) return "parked";

  if (pr !== null) {
    const info = snap.prs.get(pr);
    if (info?.state === "MERGED") return "merged";
    if (info?.isDraft) return "held";
  }

  return budget ? "running" : null;
}

/** null when there is nothing to show — the item has no factory history yet. */
export async function factoryStatusFor(
  repoRoot: string,
  dbPath: string,
  issue: number | null,
  pr: number | null,
): Promise<FactoryStatus | null> {
  if (issue === null) return null;
  const snap = await snapshotFor(repoRoot, dbPath);
  return statusOf(snap, issue, pr);
}

// ── the issues ledger ────────────────────────────────────────────────────────
// "Fixes/Closes/Resolves #N" in a PR body is GitHub's own closing-keyword
// syntax — the same link rule adws_factory itself relies on (merge_and_close
// in decider.py) and the one the predecessor Archon ledger had to recover by
// regex because its runs never logged the issue directly. Kept simple on
// purpose: unlike the Archon ledger this does not need branch-name or
// prompt-text heuristics, because there is exactly one authoritative source.
const CLOSES_RE = /(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)/gi;

export interface LedgerPr {
  number: number;
  title: string;
  state: string;
  isDraft: boolean;
  live: boolean;
}

export interface LedgerIssue {
  number: number;
  title: string;
  status: FactoryStatus | null;
  stage: string | null;
  prs: LedgerPr[];
}

export async function listIssues(repoRoot: string, dbPath: string): Promise<LedgerIssue[]> {
  const snap = await snapshotFor(repoRoot, dbPath);

  const prsByIssue = new Map<number, PrInfo[]>();
  for (const pr of snap.prs.values()) {
    CLOSES_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = CLOSES_RE.exec(pr.body))) {
      const issueNum = Number(match[1]);
      const list = prsByIssue.get(issueNum) ?? [];
      list.push(pr);
      prsByIssue.set(issueNum, list);
    }
  }

  const out: LedgerIssue[] = [];
  for (const issue of snap.issues.values()) {
    const linkedPrs = prsByIssue.get(issue.number) ?? [];
    const hasBudget = Boolean(snap.budgets[String(issue.number)]);
    // Old, untouched history clutters a "what's in flight" view without
    // adding anything the closed issue itself doesn't already say.
    if (issue.state !== "OPEN" && !hasBudget && linkedPrs.length === 0) continue;

    // Live = newest OPEN PR; failing that, newest MERGED; failing that,
    // newest overall. Everything else on the issue is superseded.
    const byNewest = [...linkedPrs].sort((a, b) => b.number - a.number);
    const live =
      byNewest.find((p) => p.state === "OPEN") ??
      byNewest.find((p) => p.state === "MERGED") ??
      byNewest[0];

    const stageLabel = [...issue.labels].find((l) => l.startsWith("stage: "));

    out.push({
      number: issue.number,
      title: issue.title,
      status: statusOf(snap, issue.number, live?.number ?? null),
      stage: stageLabel ? stageLabel.slice("stage: ".length) : null,
      prs: byNewest.map((p) => ({
        number: p.number,
        title: p.title,
        state: p.state,
        isDraft: p.isDraft,
        live: p.number === live?.number,
      })),
    });
  }

  return out.toSorted((a, b) => b.number - a.number);
}
