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
 * this would otherwise shell out to `gh` dozens of times a second.
 */
import { dirname, join } from "node:path";

export type FactoryStatus = "running" | "held" | "escalated" | "merged" | "parked";

interface Snapshot {
  budgets: Record<string, { blocked_stage: string | null }>;
  issueLabels: Map<number, Set<string>>;
  prs: Map<number, { isDraft: boolean; merged: boolean }>;
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
    ghJson(["issue", "list", "--state", "all", "--limit", "500", "--json", "number,labels"], repoRoot),
    ghJson(["pr", "list", "--state", "all", "--limit", "500", "--json", "number,isDraft,state"], repoRoot),
  ]);

  let budgets: Snapshot["budgets"] = {};
  if (stateText) {
    try {
      budgets = (JSON.parse(stateText).budgets ?? {}) as Snapshot["budgets"];
    } catch {
      /* corrupt state.json costs status accuracy, not the request */
    }
  }

  const issueLabels = new Map<number, Set<string>>();
  for (const entry of (issuesRaw as Array<{ number: number; labels: { name: string }[] }>) ?? []) {
    issueLabels.set(entry.number, new Set(entry.labels.map((l) => l.name)));
  }

  const prs = new Map<number, { isDraft: boolean; merged: boolean }>();
  for (const entry of (prsRaw as Array<{ number: number; isDraft: boolean; state: string }>) ?? []) {
    prs.set(entry.number, { isDraft: entry.isDraft, merged: entry.state === "MERGED" });
  }

  return { budgets, issueLabels, prs, fetchedAt: Date.now() };
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

/** null when there is nothing to show — the item has no factory history yet. */
export async function factoryStatusFor(
  repoRoot: string,
  dbPath: string,
  issue: number | null,
  pr: number | null,
): Promise<FactoryStatus | null> {
  if (issue === null) return null;
  const snap = await snapshotFor(repoRoot, dbPath);

  const labels = snap.issueLabels.get(issue) ?? new Set<string>();
  if (labels.has("factory-done")) return "merged";

  const budget = snap.budgets[String(issue)];
  if (budget?.blocked_stage) return "escalated";
  if (labels.has("factory-hold")) return "parked";

  if (pr !== null) {
    const info = snap.prs.get(pr);
    if (info?.merged) return "merged";
    if (info?.isDraft) return "held";
  }

  return budget ? "running" : null;
}
