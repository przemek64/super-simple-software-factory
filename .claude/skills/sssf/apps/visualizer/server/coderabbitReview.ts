/**
 * CodeRabbit review counts for one p3 run — accepted vs rejected findings,
 * broken down by severity.
 *
 * adw_coderabbit.py writes two files per run under the session dir's
 * context_handoff/: coderabbit_findings.md (every finding CodeRabbit posted,
 * each with a "severity (unreliable): 🟠 Major" line) and
 * coderabbit_triage.md (the fix/reject decision, as "### [id]" entries under
 * "## Fix these" and "- [id]" entries under "## Refused"). Joining the two on
 * the finding id gives severity-per-decision without re-deriving anything —
 * this reads the record adw_coderabbit.py already produced.
 *
 * adw_modules/coderabbit.py is explicit that CodeRabbit's own severity label
 * is NOT reliable (the same bug graded Major on one pass, Minor on another) —
 * it is shown here as a rough signal for triage, not a hard classifier.
 */
import { resolve } from "node:path";

export interface ReviewCounts {
  accepted: number;
  rejected: number;
  critical: number;
  high: number;
  medium: number;
  low: number;
}

const FINDING_HEADING_RE = /^##\s*\[([0-9a-f]{6,})\]/i;
const SEVERITY_LINE_RE = /severity \(unreliable\):\s*(.+)$/i;
const ID_BRACKET_RE = /\[([0-9a-f]{6,})\]/gi;

/** CodeRabbit's own 4-tier vocabulary (Critical/Major/Minor/Trivial), mapped
 *  onto the tier names the user asked for. Matched on substring, not the
 *  emoji, since the emoji set has drifted across CodeRabbit versions. */
export type Severity = "critical" | "high" | "medium" | "low";

function severityTier(raw: string): Severity | null {
  const s = raw.toLowerCase();
  if (s.includes("critical")) return "critical";
  if (s.includes("major") || s.includes("high")) return "high";
  if (s.includes("minor") || s.includes("medium")) return "medium";
  if (s.includes("trivial") || s.includes("low") || s.includes("nit")) return "low";
  return null;
}

function parseSeverityById(findingsText: string): Map<string, Severity> {
  const bySeverity = new Map<string, Severity>();
  let currentId: string | null = null;
  // \r?\n, not "\n": a CRLF file leaves a trailing \r on every line, and `.`
  // in SEVERITY_LINE_RE excludes line terminators — including \r — so a
  // bare split("\n") would silently match zero severities on Windows.
  for (const line of findingsText.split(/\r?\n/)) {
    const heading = FINDING_HEADING_RE.exec(line);
    if (heading) {
      currentId = heading[1]!.toLowerCase();
      continue;
    }
    const sev = SEVERITY_LINE_RE.exec(line);
    if (sev && currentId) {
      const tier = severityTier(sev[1]!);
      if (tier) bySeverity.set(currentId, tier);
    }
  }
  return bySeverity;
}

/** Ids under one triage section — "### [id]" (Fix) or "- [id]" (Refused)
 *  both match the same bracket pattern, so splitting on the heading first is
 *  what tells the two apart. */
function idsIn(sectionText: string): string[] {
  const ids: string[] = [];
  ID_BRACKET_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ID_BRACKET_RE.exec(sectionText))) ids.push(m[1]!.toLowerCase());
  return ids;
}

export async function reviewCountsFor(
  sessionsDir: string,
  adwId: string,
): Promise<ReviewCounts | null> {
  const dir = resolve(sessionsDir, adwId, "context_handoff");
  const [findingsText, triageText] = await Promise.all([
    Bun.file(resolve(dir, "coderabbit_findings.md"))
      .text()
      .catch(() => null),
    Bun.file(resolve(dir, "coderabbit_triage.md"))
      .text()
      .catch(() => null),
  ]);
  if (triageText === null) return null;

  const refusedSplit = triageText.indexOf("## Refused");
  const acceptedIds = idsIn(refusedSplit === -1 ? triageText : triageText.slice(0, refusedSplit));
  const rejectedIds = refusedSplit === -1 ? [] : idsIn(triageText.slice(refusedSplit));

  const severityById = findingsText ? parseSeverityById(findingsText) : new Map<string, Severity>();

  const counts: ReviewCounts = { accepted: 0, rejected: 0, critical: 0, high: 0, medium: 0, low: 0 };
  for (const id of acceptedIds) {
    counts.accepted += 1;
    const tier = severityById.get(id);
    if (tier) counts[tier] += 1;
  }
  for (const id of rejectedIds) {
    counts.rejected += 1;
    const tier = severityById.get(id);
    if (tier) counts[tier] += 1;
  }
  return counts;
}
