/**
 * SSSF visualizer server — JSON API over a target repo's sssf.db, plus the
 * built UI when ./dist exists. Reads are read-only; the one write to the db is
 * POST /api/sessions/:adw_id/archive, which sets one review flag on a row.
 *
 * It also STARTS runs: POST /api/launch spawns a workflow in the target repo
 * (see server/launcher.ts, and docs/adr/0001 for why). Nothing about a running
 * run comes back this way — the data path is still agents → sqlite → web ui,
 * and the UI gets there by polling.
 *
 *   bun run server/index.ts
 *   bun run server/index.ts --db /path/to/repo/adws/adw_runtime/sssf.db
 *   SSSF_DB=/path/to/sssf.db PORT=4600 bun run server/index.ts
 */
import { existsSync, statSync } from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";
import { SssfDb, resolveDbPath } from "./db.ts";
import { LauncherError, Launchers } from "./launcher.ts";
import { factoryStatusFor } from "./factoryStatus.ts";
import type {
  AgentPrompts,
  ApiError,
  HealthResponse,
  LaunchRequest,
  LaunchState,
  LaunchStatus,
} from "../shared/types.ts";

const PORT = Number(process.env.PORT ?? 4600);

/** The repo a db belongs to: <repo>/adws/adw_runtime/sssf.db → "<repo>".
 *  Derived from the db path rather than cwd, because the server is started from
 *  its own app directory and the db is pointed at with SSSF_DB. */
function repoNameFor(dbFile: string): string {
  return basename(resolve(dirname(dbFile), "..", "..")) || "repo";
}
const DIST_DIR = resolve(import.meta.dir, "..", "dist");

const dbPath = resolveDbPath();
let db: SssfDb;
try {
  db = new SssfDb(dbPath);
} catch (error) {
  console.error(`[sssf] ${(error as Error).message}`);
  process.exit(1);
}

const launchers = new Launchers(db.path);

/**
 * A run is alive if its session row says so. Before the run's first insert
 * there is no row at all, so a just-spawned launch counts as alive for a grace
 * period — otherwise a double-click inside the first few seconds, the most
 * likely double-click there is, would slip past the duplicate check.
 */
const START_GRACE_MS = 90_000;

function launchState(record: { adw_id: string; started_at: string }): LaunchState {
  const session = db.session(record.adw_id);
  if (session) {
    return (session.status ?? "running") as LaunchState;
  }
  const age = Date.now() - Date.parse(record.started_at);
  return Number.isFinite(age) && age < START_GRACE_MS ? "starting" : "failed_to_start";
}

function isActive(record: { adw_id: string; started_at: string }): boolean {
  const state = launchState(record);
  return state === "running" || state === "starting";
}

/**
 * Only the visualizer's own pages may start a run. The server listens on a
 * local port that any page in the browser can reach, and POST /launch spends
 * real money in a real repository.
 *
 * A missing Origin is allowed: a browser always sends one on a cross-site POST
 * (a JSON body forces a preflight), so the header's absence means a non-browser
 * caller — curl, a script — which was never the thing being guarded against.
 */
function originAllowed(req: Request): boolean {
  const origin = req.headers.get("origin");
  if (!origin) return true;
  let host: URL;
  try {
    host = new URL(origin);
  } catch {
    return false;
  }
  if (host.hostname !== "localhost" && host.hostname !== "127.0.0.1") return false;
  const port = Number(host.port || 80);
  // The API port, or the UI's dev-server port beside it, which proxies here and
  // forwards its own origin.
  return port === PORT || port === PORT + 1;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function notFound(message: string): Response {
  return json({ error: message } satisfies ApiError, 404);
}

/** Guard every handler so a malformed query can't take the server down mid-run. */
function safely(
  handler: (req: Request) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return async (req) => {
    try {
      return await handler(req);
    } catch (error) {
      console.error(`[sssf] ${req.method} ${new URL(req.url).pathname}:`, error);
      return json({ error: (error as Error).message } satisfies ApiError, 500);
    }
  };
}

/**
 * adw_ids and agent names are path segments on disk, so anything that isn't a
 * plain identifier is rejected outright rather than sanitized into something
 * that might still escape the sessions directory.
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;

function isSafeSegment(value: string): boolean {
  return SAFE_SEGMENT.test(value) && value !== "." && value !== "..";
}

function param(req: Request, key: string): string {
  return decodeURIComponent(
    (req as Request & { params: Record<string, string> }).params[key] ?? "",
  );
}

function intQuery(req: Request, key: string, fallback: number): number {
  const raw = new URL(req.url).searchParams.get(key);
  if (raw === null || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** Serve the built SPA if it has been built; otherwise point at the dev server. */
async function serveStatic(req: Request): Promise<Response> {
  const { pathname } = new URL(req.url);

  if (!existsSync(DIST_DIR)) {
    return new Response(
      `SSSF visualizer API is running on :${PORT}.\n\n` +
        `No ./dist build found. Run "bun run dev" for the Vite dev server ` +
        `(it proxies /api here), or "bun run build" to serve the UI from this process.\n`,
      { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  // Reject traversal before touching the filesystem.
  const candidate = resolve(join(DIST_DIR, pathname));
  if (candidate === DIST_DIR || candidate.startsWith(DIST_DIR + "/")) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return new Response(Bun.file(candidate));
    }
  }

  // SPA fallback: breadcrumb routes are client-side.
  const indexHtml = join(DIST_DIR, "index.html");
  if (existsSync(indexHtml)) {
    return new Response(Bun.file(indexHtml), {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  return notFound("not found");
}

const server = Bun.serve({
  port: PORT,
  routes: {
    "/api/health": safely(
      () =>
        json({
          ok: true,
          db: db.path,
          journal_mode: db.journalMode,
          sessions: db.sessionCount(),
          repo: repoNameFor(db.path),
        } satisfies HealthResponse),
    ),

    "/api/sessions": safely((req) => json(db.sessions(intQuery(req, "limit", 200)))),

    // ── the dashboard ───────────────────────────────────────────────────────

    "/api/launchers": safely(async () => json(await launchers.list())),

    /** The static diagram beside a launcher. It describes the workflow and
     *  never a run, so it is a file, cached like one. */
    "/api/launchers/:id/diagram": safely(async (req) => {
      const id = param(req, "id");
      if (!isSafeSegment(id)) return json({ error: "invalid launcher" } satisfies ApiError, 400);
      const file = await launchers.diagramFile(id);
      if (!file) return notFound(`no diagram for ${id}`);
      return new Response(Bun.file(file), {
        headers: { "content-type": "image/svg+xml; charset=utf-8" },
      });
    }),

    /** Every launch this server started, newest first, with what became of it. */
    "/api/launches": safely(async () =>
      json(
        await Promise.all(
          (await launchers.records()).map(async (record) => {
            const state = launchState(record);
            return {
              ...record,
              state,
              log_tail:
                state === "failed_to_start" ? await launchers.logTail(record.adw_id) : null,
            } satisfies LaunchStatus;
          }),
        ),
      ),
    ),

    "/api/launch": {
      POST: safely(async (req) => {
        if (!originAllowed(req)) {
          return json({ error: "cross-origin launch refused" } satisfies ApiError, 403);
        }
        const body = (await req.json().catch(() => null)) as LaunchRequest | null;
        if (!body || typeof body.launcher_id !== "string") {
          return json({ error: "launcher_id is required" } satisfies ApiError, 400);
        }
        try {
          return json(await launchers.launch(body, isActive), 201);
        } catch (error) {
          if (error instanceof LauncherError) {
            return json({ error: error.message } satisfies ApiError, error.status);
          }
          throw error;
        }
      }),
    },

    // Factory item status (running/held/escalated/merged/parked), derived from
    // adws_factory's state.json + GitHub — not a column in sssf.db, so it is
    // its own endpoint rather than something sessions() computes.
    "/api/factory-status": safely(async (req) => {
      const issue = intQuery(req, "issue", NaN);
      const pr = intQuery(req, "pr", NaN);
      if (!Number.isFinite(issue)) {
        return json({ error: "issue is required" } satisfies ApiError, 400);
      }
      const status = await factoryStatusFor(
        launchers.repoRoot,
        db.path,
        issue,
        Number.isFinite(pr) ? pr : null,
      );
      return json({ status });
    }),

    "/api/sessions/:adw_id": safely((req) => {
      const detail = db.sessionDetail(param(req, "adw_id"));
      return detail ? json(detail) : notFound(`no session ${param(req, "adw_id")}`);
    }),

    // The one write. Archiving is review triage — it belongs to the reader, not
    // to the run — so it never touches anything a tracer wrote.
    "/api/sessions/:adw_id/archive": {
      POST: safely(async (req) => {
        const adwId = param(req, "adw_id");
        if (!isSafeSegment(adwId)) {
          return json({ error: "invalid adw_id" } satisfies ApiError, 400);
        }
        const body = (await req.json().catch(() => ({}))) as { archived?: unknown };
        const archived = body.archived === undefined ? true : Boolean(body.archived);
        return db.setArchived(adwId, archived)
          ? json({ adw_id: adwId, archived })
          : notFound(`no session ${adwId}`);
      }),
    },

    "/api/sessions/:adw_id/events": safely((req) =>
      json(
        db.events(
          param(req, "adw_id"),
          intQuery(req, "after", 0),
          intQuery(req, "limit", 500),
        ),
      ),
    ),

    "/api/sessions/:adw_id/envelopes": safely((req) =>
      json(db.envelopes(param(req, "adw_id"))),
    ),

    "/api/sessions/:adw_id/gates": safely((req) => json(db.gates(param(req, "adw_id")))),

    // The exact prompts an agent was sent, read from the session dir. Files are
    // the raw record; the db has no copy of them.
    "/api/sessions/:adw_id/agents/:agent/prompts": safely(async (req) => {
      const adwId = param(req, "adw_id");
      const agent = param(req, "agent");
      if (!isSafeSegment(adwId) || !isSafeSegment(agent)) {
        return json({ error: "invalid adw_id or agent" } satisfies ApiError, 400);
      }
      if (!db.session(adwId)) return notFound(`no session ${adwId}`);

      const dir = resolve(db.sessionsDir, adwId, agent, "prompts");
      // Defense in depth: the segment check already forbids traversal.
      if (dir !== db.sessionsDir && !dir.startsWith(db.sessionsDir + sep)) {
        return json({ error: "invalid path" } satisfies ApiError, 400);
      }

      // A prompt file is absent whenever the agent never ran in this session —
      // a normal state, so it reads as null rather than an error.
      const read = async (name: string): Promise<string | null> => {
        const file = Bun.file(join(dir, `${name}.md`));
        return (await file.exists()) ? await file.text() : null;
      };
      return json({
        system: await read("system"),
        user: await read("user"),
      } satisfies AgentPrompts);
    }),
  },

  fetch(req) {
    const { pathname } = new URL(req.url);
    if (pathname.startsWith("/api/")) return notFound(`no route ${pathname}`);
    return serveStatic(req);
  },
});

console.log(`[sssf] visualizer api  http://localhost:${server.port}`);
console.log(`[sssf] db              ${db.path}  [journal_mode=${db.journalMode}]`);
console.log(
  `[sssf] launches into   ${launchers.repoRoot}` +
    (existsSync(launchers.file) ? "" : `  (no ${launchers.file} — dashboard will be empty)`),
);
console.log(
  existsSync(DIST_DIR)
    ? `[sssf] serving ui from  ${DIST_DIR}`
    : `[sssf] no ./dist — use "bun run dev" for the Vite dev server on :${process.env.VITE_PORT ?? 4601}`,
);

process.on("SIGINT", () => {
  db.close();
  process.exit(0);
});
