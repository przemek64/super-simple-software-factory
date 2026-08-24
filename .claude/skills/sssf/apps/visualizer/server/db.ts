/**
 * SQLite reader over a target repo's sssf.db.
 *
 * The read connection is opened readonly and every query on it is a SELECT —
 * the writers are the tracers of running ADW processes, and WAL lets us read
 * straight through their inserts.
 *
 * ONE exception, opened lazily on its own connection: `setArchived`. Archiving
 * is review triage — "I have looked at this run" — which has to outlive a
 * browser, so it lives on the session row rather than in localStorage. It is
 * the only write this process can make, it touches exactly one column, and it
 * never runs unless a human clicks the button.
 */
import { Database } from "bun:sqlite";
import { classifyFailure } from "./failure.ts";
import { existsSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import type {
  AgentSession,
  AgentStartPayload,
  Envelope,
  Event,
  EventsPage,
  GateResult,
  Phase,
  Session,
  SessionDetail,
  SessionRef,
  SessionSummary,
  SessionUsage,
} from "../shared/types.ts";

const DEFAULT_DB_RELATIVE = "adws/adw_runtime/sssf.db";
const MAX_LIMIT = 1000;
const DEFAULT_LIMIT = 500;

/**
 * Resolve the db path: --db arg wins, then SSSF_DB, then <cwd>/adws/adw_runtime/sssf.db.
 * The db lives in the TARGET repo, so cwd is the repo the visualizer is pointed at.
 */
export function resolveDbPath(argv: string[] = Bun.argv): string {
  const flagIndex = argv.indexOf("--db");
  const inline = argv.find((a) => a.startsWith("--db="));
  const raw =
    (flagIndex !== -1 ? argv[flagIndex + 1] : undefined) ??
    inline?.slice("--db=".length) ??
    process.env.SSSF_DB ??
    DEFAULT_DB_RELATIVE;

  return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
}

export class SssfDb {
  readonly path: string;
  /**
   * Where the ADW session dirs live: `{data_dir}/sessions/{adw_id}/{agent}/`.
   * The db sits in the same data_dir (config's `observability.db` defaults to
   * `adws/adw_runtime/sssf.db`), so deriving it as a sibling of the db file keeps
   * working when the whole data_dir is relocated.
   */
  readonly sessionsDir: string;
  readonly journalMode: string;
  private readonly db: Database;
  /** Opened on first archive and kept; null until then. */
  private writer: Database | null = null;
  /** Cache for optionalColumn(), keyed "table.column". Only ever false → true. */
  private readonly columnCache = new Map<string, boolean>();

  constructor(path: string) {
    if (!existsSync(path)) {
      throw new Error(
        `sssf.db not found at ${path}\n` +
          `Point the visualizer at a target repo: --db <path> or SSSF_DB=<path>, ` +
          `or run it from a repo root containing ${DEFAULT_DB_RELATIVE}`,
      );
    }
    this.path = path;
    this.sessionsDir = resolve(dirname(path), "sessions");
    this.db = new Database(path, { readonly: true });

    // WAL is set by the tracer when it creates the db; a readonly connection
    // cannot change it, so we assert rather than set, and always take the
    // busy_timeout so a concurrent writer never turns into a failed request.
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.db.exec("PRAGMA synchronous = NORMAL");
    const mode = this.db
      .query<{ journal_mode: string }, []>("PRAGMA journal_mode")
      .get();
    this.journalMode = mode?.journal_mode ?? "unknown";
    if (this.journalMode.toLowerCase() !== "wal") {
      console.warn(
        `[db] journal_mode is "${this.journalMode}", expected "wal" — ` +
          `live reads during agent writes may block`,
      );
    }

  }

  /**
   * A SELECT fragment for a column the tracer adds by migration.
   *
   * We open readonly and cannot run those ALTERs ourselves, so selecting one
   * blindly would throw "no such column" on every request against a db an older
   * tracer wrote. Instead we probe and substitute NULL, which reads downstream
   * as "this db predates the column" — the same thing the UI shows for a row
   * the migration didn't backfill.
   *
   * The probe re-runs while the column is missing, because the tracer's ALTER
   * can land while we're serving: a startup-only check would keep returning
   * NULL for the rest of the process even after the data arrived. Once seen,
   * a column never goes away, so it latches.
   */
  private hasColumn(table: string, column: string): boolean {
    const key = `${table}.${column}`;
    if (!this.columnCache.get(key)) {
      const cols = this.db
        .query<{ name: string }, []>(`PRAGMA table_info(${table})`)
        .all();
      this.columnCache.set(key, cols.some((c) => c.name === column));
    }
    return this.columnCache.get(key) ?? false;
  }

  private optionalColumn(table: string, column: string): string {
    return this.hasColumn(table, column) ? column : `NULL AS ${column}`;
  }

  close(): void {
    this.writer?.close();
    this.db.close();
  }

  /**
   * Archive or restore a session — the only write in this process.
   *
   * busy_timeout matters: a run may be mid-insert on the same WAL db, and a
   * click should wait its turn rather than fail. Returns false when the id
   * does not exist, so the route can 404 instead of silently succeeding.
   */
  setArchived(adwId: string, archived: boolean): boolean {
    if (!this.hasColumn("sessions", "archived")) {
      throw new Error("this db predates the archived column — run any ADW once to migrate it");
    }
    if (!this.writer) {
      this.writer = new Database(this.path);
      this.writer.exec("PRAGMA busy_timeout=5000;");
    }
    this.writer
      .query("UPDATE sessions SET archived = ? WHERE adw_id = ?")
      .run(archived ? 1 : 0, adwId);
    return this.session(adwId) !== null;
  }

  /** Sessions, most recent first, each with its phase statuses for the progress dots. */
  sessions(limit = 200): SessionSummary[] {
    const rows = this.db
      .query<Session, [number]>(
        `SELECT adw_id, ${this.optionalColumn("sessions", "adw_name")}, request,
                status, engineer, started_at, ended_at,
                total_tokens, total_cost,
                ${this.optionalColumn("sessions", "archived")}
           FROM sessions
          WHERE COALESCE(${this.hasColumn("sessions", "archived") ? "archived" : "0"}, 0) = 0
          ORDER BY started_at DESC, rowid DESC
          LIMIT ?`,
      )
      .all(clamp(limit, 1, MAX_LIMIT));

    if (rows.length === 0) return [];

    // Embed each session's phases so the L1 progress dots cost no extra request.
    const ids = rows.map((row) => row.adw_id);
    const placeholders = ids.map(() => "?").join(", ");
    const phaseRows = this.db
      .query<Phase, string[]>(
        `SELECT phase_id, adw_id, seq, name, kind, owner, description, status,
                attempt, retries, error, started_at, ended_at
           FROM phases WHERE adw_id IN (${placeholders}) ORDER BY seq, rowid`,
      )
      .all(...ids);

    const byAdw = new Map<string, Phase[]>();
    for (const phase of phaseRows) {
      const list = byAdw.get(phase.adw_id);
      if (list) list.push(phase);
      else byAdw.set(phase.adw_id, [phase]);
    }

    // Agents come along too: an L1 card draws a per-agent dot timeline, and its
    // dots are colored per agent — without this it would be one request per card.
    const agentsByAdw = this.agentsFor(ids);

    // Gate rows are the fallback evidence when a failed phase left no error
    // text. Only those sessions need them, and on a healthy list that is none —
    // so the extra query is skipped entirely rather than paid for per card.
    const needGates = rows
      .map((r) => r.adw_id)
      .filter((id) =>
        (byAdw.get(id) ?? []).some((p) => p.status === "fail" && !(p.error ?? "").trim()),
      );
    const gatesByAdw = this.failedGatesFor(needGates);

    const summaries: SessionSummary[] = [];
    for (const session of rows) {
      const phases = byAdw.get(session.adw_id) ?? [];
      summaries.push(
        Object.assign(session, {
          phases,
          phase_count: phases.length,
          agents: agentsByAdw.get(session.adw_id) ?? [],
          failure: classifyFailure(session, phases, gatesByAdw.get(session.adw_id) ?? []),
        }),
      );
    }
    return summaries;
  }

  /** Failed gate rows for a set of sessions, empty map for an empty input. */
  private failedGatesFor(adwIds: string[]): Map<string, GateResult[]> {
    const byAdw = new Map<string, GateResult[]>();
    if (adwIds.length === 0) return byAdw;
    const placeholders = adwIds.map(() => "?").join(", ");
    const checks = this.optionalColumn("gate_results", "checks_json");
    const rows = this.db
      .query<GateResult, string[]>(
        `SELECT id, adw_id, phase_id, attempt, gate, passed, violations_json,
                ${checks}, created_at
           FROM gate_results
          WHERE adw_id IN (${placeholders}) AND passed = 0
          ORDER BY id`,
      )
      .all(...adwIds);
    for (const row of rows) {
      const list = byAdw.get(row.adw_id);
      if (list) list.push(row);
      else byAdw.set(row.adw_id, [row]);
    }
    return byAdw;
  }

  session(adwId: string): Session | null {
    return (
      this.db
        .query<Session, [string]>(
          `SELECT adw_id, ${this.optionalColumn("sessions", "adw_name")}, request,
                  status, engineer, started_at, ended_at,
                  total_tokens, total_cost
             FROM sessions WHERE adw_id = ?`,
        )
        .get(adwId) ?? null
    );
  }

  phases(adwId: string): Phase[] {
    return this.db
      .query<Phase, [string]>(
        `SELECT phase_id, adw_id, seq, name, kind, owner, description, status,
                attempt, retries, error, started_at, ended_at
           FROM phases WHERE adw_id = ? ORDER BY seq, rowid`,
      )
      .all(adwId);
  }

  agentSessions(adwId: string): AgentSession[] {
    return this.agentsFor([adwId]).get(adwId) ?? [];
  }

  /**
   * Agents per session, for a set of ids at once: the agent_sessions rows plus
   * anything that has started but not finished.
   *
   * agents.py writes the agent_sessions row only after the envelope persists, so
   * a running agent has no row there — precisely the case the live view exists
   * for. Its model, color and session_id are already on the agent_start event,
   * so a lane is labelled and colored from the moment the agent spawns.
   */
  private agentsFor(adwIds: string[]): Map<string, AgentSession[]> {
    const byAdw = new Map<string, AgentSession[]>();
    if (adwIds.length === 0) return byAdw;
    const placeholders = adwIds.map(() => "?").join(", ");

    const append = (adwId: string, agent: AgentSession) => {
      const list = byAdw.get(adwId);
      if (list) list.push(agent);
      else byAdw.set(adwId, [agent]);
    };

    const color = this.optionalColumn("agent_sessions", "color");
    const ctxUsed = this.optionalColumn("agent_sessions", "context_tokens");
    const ctxWindow = this.optionalColumn("agent_sessions", "context_window");

    const completed = this.db
      .query<AgentSession, string[]>(
        `SELECT adw_id, agent, coding_agent, model, session_id, ${color},
                ${ctxUsed}, ${ctxWindow}, created_at, last_used_at
           FROM agent_sessions WHERE adw_id IN (${placeholders})
          ORDER BY created_at, agent`,
      )
      .all(...adwIds);
    for (const row of completed) append(row.adw_id, row);

    const started = this.db
      .query<
        {
          adw_id: string;
          agent: string | null;
          payload_json: string | null;
          started_at: string | null;
        },
        string[]
      >(
        `SELECT e.adw_id, p.owner AS agent, e.payload_json, e.started_at
           FROM events e JOIN phases p ON p.phase_id = e.phase_id
          WHERE e.adw_id IN (${placeholders}) AND e.type = 'agent_start'
          ORDER BY e.rowid`,
      )
      .all(...adwIds);

    for (const row of started) {
      if (!row.agent) continue;
      // A finished row is authoritative; only fill genuine gaps.
      if (byAdw.get(row.adw_id)?.some((a) => a.agent === row.agent)) continue;
      let payload: AgentStartPayload = {};
      try {
        payload = JSON.parse(row.payload_json ?? "{}") as AgentStartPayload;
      } catch {
        // A malformed payload just means no label — never a failed request.
      }
      append(row.adw_id, {
        adw_id: row.adw_id,
        agent: row.agent,
        coding_agent: null,
        model: payload.model ?? null,
        session_id: payload.session_id ?? null,
        color: payload.color ?? null,
        // Occupancy is only known once the agent's turn closes.
        context_tokens: null,
        context_window: null,
        created_at: row.started_at,
        last_used_at: row.started_at,
      });
    }
    return byAdw;
  }

  /** Session + phases + agents in one shot — L2 needs all three to draw lanes. */
  sessionDetail(adwId: string): SessionDetail | null {
    const session = this.session(adwId);
    if (!session) return null;

    const phases = this.phases(adwId);
    return {
      session,
      usage: this.usage(adwId),
      phases,
      agents: this.agentSessions(adwId),
      failure: classifyFailure(session, phases, this.gates(adwId)),
    };
  }

  /**
   * Raw tokens read and written, beside the billed headline.
   *
   * Derived from the `agent_end` payloads rather than stored, so every run
   * already in the db gets the split without a migration or a re-run.
   *
   * `total_tokens` is a SPEND number: every turn re-sends the whole
   * conversation, so an 86k conversation over 49 turns bills millions. These
   * two say what actually moved — material read for the first time, and
   * material generated. The gap between them and the headline is cached
   * re-reads, which is usually most of it.
   */
  usage(adwId: string): SessionUsage {
    const rows = this.db
      .query<{ payload_json: string | null }, [string]>(
        "SELECT payload_json FROM events WHERE adw_id = ? AND type = 'agent_end'",
      )
      .all(adwId);

    let read = 0;
    let written = 0;
    for (const row of rows) {
      if (!row.payload_json) continue;
      try {
        const u = (JSON.parse(row.payload_json) as { usage?: Record<string, number> }).usage;
        if (!u) continue;
        // RAW reads only: material entering the context for the first time,
        // billed either as uncached input or as a cache write. Cache reads are
        // the same tokens served again on later turns — counting them here
        // would rebuild the very inflation this split exists to expose.
        read += (u.input_tokens ?? 0) + (u.cache_write_tokens ?? 0);
        written += u.output_tokens ?? 0;
      } catch {
        /* a payload written by an older tracer simply contributes nothing */
      }
    }
    return { read, written };
  }

  /**
   * The polling query. Rowid cursor, insertion order, bounded page — the same
   * mechanism serves the live tail and lazy-paged history.
   */
  events(adwId: string, after = 0, limit = DEFAULT_LIMIT): EventsPage {
    const cappedLimit = clamp(limit, 1, MAX_LIMIT);
    const events = this.db
      .query<Event, [string, number, number]>(
        `SELECT rowid, event_id, adw_id, phase_id, parent_id, type, name,
                payload_json, tokens, started_at, ended_at
           FROM events
          WHERE adw_id = ? AND rowid > ?
          ORDER BY rowid
          LIMIT ?`,
      )
      .all(adwId, Math.max(0, after), cappedLimit);

    return {
      events,
      cursor: events.length > 0 ? events[events.length - 1]!.rowid : Math.max(0, after),
      has_more: events.length === cappedLimit,
    };
  }

  envelopes(adwId: string): Envelope[] {
    return this.db
      .query<Envelope, [string]>(
        `SELECT envelope_id, adw_id, phase_id, agent, output_type, payload_json,
                valid, attempt, created_at
           FROM envelopes WHERE adw_id = ? ORDER BY created_at, rowid`,
      )
      .all(adwId);
  }

  gates(adwId: string): GateResult[] {
    const checks = this.optionalColumn("gate_results", "checks_json");
    return this.db
      .query<GateResult, [string]>(
        `SELECT id, adw_id, phase_id, attempt, gate, passed, violations_json,
                ${checks}, created_at
           FROM gate_results WHERE adw_id = ? ORDER BY id`,
      )
      .all(adwId);
  }

  /**
   * The issue/pr each session belongs to, for the issues ledger's stage trail.
   * Mirrors the extraction SessionRow.vue already does client-side (structured
   * payload field first, "issue #N" in the request text as a fallback) so the
   * ledger and a session row never disagree about which issue a run was for.
   *
   * Two queries total, not one per session: the events query is narrowed to
   * rows that could plausibly hold a pr/issue field, then the first match per
   * session wins — same "best source first" rule the client uses.
   */
  sessionRefs(): SessionRef[] {
    const sessions = this.db
      .query<
        {
          adw_id: string;
          adw_name: string | null;
          status: string | null;
          started_at: string | null;
          request: string | null;
        },
        []
      >(
        `SELECT adw_id, ${this.optionalColumn("sessions", "adw_name")}, status, started_at, request
           FROM sessions`,
      )
      .all();
    if (sessions.length === 0) return [];

    const ids = sessions.map((s) => s.adw_id);
    const placeholders = ids.map(() => "?").join(", ");
    const events = this.db
      .query<{ adw_id: string; payload_json: string | null }, string[]>(
        `SELECT adw_id, payload_json FROM events
          WHERE adw_id IN (${placeholders})
            AND (payload_json LIKE '%pr%' OR payload_json LIKE '%issue%')
          ORDER BY rowid`,
      )
      .all(...ids);

    const NUM = /(\d+)/;
    function digits(v: unknown): string | null {
      if (typeof v === "number" && Number.isFinite(v)) return String(v);
      if (typeof v !== "string") return null;
      return NUM.exec(v)?.[1] ?? null;
    }

    const byAdw = new Map<string, { pr: string | null; issue: string | null }>();
    for (const row of events) {
      const cur = byAdw.get(row.adw_id) ?? { pr: null, issue: null };
      if (cur.pr && cur.issue) continue;
      if (!row.payload_json) continue;
      try {
        const p = JSON.parse(row.payload_json) as Record<string, unknown>;
        cur.pr ??= digits(p.pr ?? p.pr_number);
        cur.issue ??= digits(p.issue ?? p.issue_number);
        byAdw.set(row.adw_id, cur);
      } catch {
        /* a payload written by an older tracer just contributes nothing */
      }
    }

    // p2/p3 stages take a PR, not an issue (config.py Stage.arg == "pr"), so
    // their runs never log an issue number at all — only the caller (which
    // already builds a PR→issue map off "Fixes #N" for the ledger) can
    // resolve those. Kept in the output with issue: null rather than dropped,
    // so a p3 run is not silently missing from the stage trail.
    const ISSUE_TEXT_RE = /issue\s*#?(\d+)/i;
    const out: SessionRef[] = [];
    for (const s of sessions) {
      const refs = byAdw.get(s.adw_id) ?? { pr: null, issue: null };
      const issueText = refs.issue ?? ISSUE_TEXT_RE.exec(s.request ?? "")?.[1] ?? null;
      const issue = issueText === null ? null : Number(issueText);
      if (issue === null && refs.pr === null) continue;
      out.push({
        adw_id: s.adw_id,
        adw_name: s.adw_name,
        status: s.status,
        started_at: s.started_at,
        issue,
        pr: refs.pr !== null ? Number(refs.pr) : null,
      });
    }
    return out;
  }

  sessionCount(): number {
    const row = this.db
      .query<{ n: number }, []>("SELECT COUNT(*) AS n FROM sessions")
      .get();
    return row?.n ?? 0;
  }
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.trunc(value)));
}
