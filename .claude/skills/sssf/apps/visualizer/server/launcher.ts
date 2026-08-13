/**
 * Launchers — the one thing this server does besides read.
 *
 * A launcher is a workflow plus a fixed parameter set, declared per repository
 * in adws/adw_sssf_config/launchers.yaml. The dashboard renders one row per
 * launcher and posts back here to start a run. See docs/adr/0001 in the skill
 * repo for why an observability UI is allowed to start anything at all.
 *
 * Two things this module owns that the trace db cannot answer on its own:
 *
 *   - the run id. The workflow invents one when it is not given `--adw-id`,
 *     which would leave the caller with no handle on what it just started —
 *     and a run that dies before its first insert would be an invisible click.
 *     We mint it, pass it in, and record the launch.
 *   - what a launch WAS. `sessions.request` is the engineer's ask, not the
 *     parameters it came from, so "is issue 187 already running" is not a
 *     question the db can be asked. launches.json is that record.
 */
import { existsSync, mkdirSync, openSync } from "node:fs";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";
import type {
  LaunchRecord,
  Launcher,
  LauncherParam,
  LaunchRequest,
} from "../shared/types.ts";

/** 8 lowercase hex chars — the same shape utils.new_id(8) produces in Python. */
function newAdwId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(4)))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * The repository whose runs we launch. SSSF_REPO is set by start-visualizer.ps1;
 * the fallback derives it from the db path (<repo>/adws/adw_runtime/sssf.db) so
 * an older start script still lands on the right checkout.
 */
export function resolveRepoRoot(dbPath: string): string {
  const fromEnv = process.env.SSSF_REPO;
  if (fromEnv && fromEnv.trim() !== "") {
    return isAbsolute(fromEnv) ? resolve(fromEnv) : resolve(process.cwd(), fromEnv);
  }
  return resolve(dirname(dbPath), "..", "..");
}

const LAUNCHERS_RELATIVE = "adws/adw_sssf_config/launchers.yaml";

export class LauncherError extends Error {
  constructor(
    message: string,
    readonly status = 400,
  ) {
    super(message);
  }
}

function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function asParam(raw: unknown, launcherLabel: string): LauncherParam {
  const p = raw as Partial<LauncherParam>;
  if (!p || typeof p.name !== "string" || typeof p.flag !== "string") {
    throw new LauncherError(`launcher "${launcherLabel}": every param needs a name and a flag`, 500);
  }
  const type = p.type === "int" ? "int" : "string";
  return {
    name: p.name,
    flag: p.flag,
    type,
    label: typeof p.label === "string" ? p.label : p.name,
    hint: typeof p.hint === "string" ? p.hint : null,
    required: p.required !== false,
  };
}

export class Launchers {
  /** Absolute path of the repo whose workflows this server may start. */
  readonly repoRoot: string;
  /** Absolute path of launches.json — beside the trace db, i.e. untracked runtime. */
  private readonly recordFile: string;
  private readonly sessionsDir: string;

  constructor(dbPath: string) {
    this.repoRoot = resolveRepoRoot(dbPath);
    this.recordFile = resolve(dirname(dbPath), "launches.json");
    this.sessionsDir = resolve(dirname(dbPath), "sessions");
  }

  get file(): string {
    return join(this.repoRoot, LAUNCHERS_RELATIVE);
  }

  /**
   * Read the launcher list fresh on every request. It is a handful of lines and
   * it is edited by hand, so caching it would only mean restarting the server to
   * see an edit — the one thing a config file should never require.
   */
  async list(): Promise<Launcher[]> {
    if (!existsSync(this.file)) return [];
    const parsed = Bun.YAML.parse(await Bun.file(this.file).text()) as {
      launchers?: unknown[];
    } | null;
    const raw = Array.isArray(parsed?.launchers) ? parsed.launchers : [];

    return raw.map((entry, index) => {
      const e = entry as Record<string, unknown>;
      const label = typeof e.label === "string" ? e.label : `launcher ${index + 1}`;
      const workflow = typeof e.workflow === "string" ? e.workflow : "";
      if (!workflow) {
        throw new LauncherError(`launcher "${label}" declares no workflow`, 500);
      }
      return {
        id: typeof e.id === "string" && e.id ? e.id : slug(label),
        label,
        description: typeof e.description === "string" ? e.description : null,
        workflow,
        diagram: typeof e.diagram === "string" ? e.diagram : null,
        params: Array.isArray(e.params) ? e.params.map((p) => asParam(p, label)) : [],
      } satisfies Launcher;
    });
  }

  async get(id: string): Promise<Launcher> {
    const found = (await this.list()).find((l) => l.id === id);
    if (!found) throw new LauncherError(`no launcher ${id}`, 404);
    return found;
  }

  /**
   * Resolve a repo-relative path and refuse anything that climbs out of the
   * checkout. Both the workflow script and the diagram come from a config file,
   * which is trusted — but a file the server executes or serves is worth
   * checking twice.
   */
  private within(relative: string): string {
    const candidate = resolve(this.repoRoot, relative);
    if (candidate !== this.repoRoot && !candidate.startsWith(this.repoRoot + sep)) {
      throw new LauncherError(`path escapes the repository: ${relative}`, 400);
    }
    return candidate;
  }

  async diagramFile(id: string): Promise<string | null> {
    const launcher = await this.get(id);
    if (!launcher.diagram) return null;
    const file = this.within(launcher.diagram);
    return existsSync(file) ? file : null;
  }

  // ── the launch record ─────────────────────────────────────────────────────

  async records(): Promise<LaunchRecord[]> {
    if (!existsSync(this.recordFile)) return [];
    try {
      const parsed = JSON.parse(await Bun.file(this.recordFile).text()) as unknown;
      return Array.isArray(parsed) ? (parsed as LaunchRecord[]) : [];
    } catch {
      // A truncated file must not stop the dashboard from working: the record is
      // a convenience, the runs themselves are in the db.
      return [];
    }
  }

  private async append(record: LaunchRecord): Promise<void> {
    const all = [record, ...(await this.records())].slice(0, 200);
    mkdirSync(dirname(this.recordFile), { recursive: true });
    await Bun.write(this.recordFile, JSON.stringify(all, null, 2));
  }

  // ── starting a run ────────────────────────────────────────────────────────

  /**
   * Validate the posted values against the launcher's declared params and turn
   * them into command-line arguments, in declaration order.
   */
  private argsFor(launcher: Launcher, values: Record<string, unknown>): string[] {
    const args: string[] = [];
    for (const param of launcher.params) {
      const raw = values[param.name];
      const given = raw !== undefined && raw !== null && String(raw).trim() !== "";
      if (!given) {
        if (param.required) throw new LauncherError(`${param.label} is required`);
        continue;
      }
      const value = String(raw).trim();
      if (param.type === "int" && !/^\d+$/.test(value)) {
        throw new LauncherError(`${param.label} must be a number, got "${value}"`);
      }
      args.push(param.flag, value);
    }
    return args;
  }

  /** The signature a duplicate would share: same launcher, same values. */
  private signature(launcher: Launcher, args: string[]): string {
    return `${launcher.id} ${args.join(" ")}`;
  }

  /**
   * Start a run, unless an identical one is already going.
   *
   * `isActive` is passed in rather than queried here: liveness is a fact about
   * the sessions table, which this module deliberately does not open.
   */
  async launch(
    request: LaunchRequest,
    isActive: (record: LaunchRecord) => boolean,
  ): Promise<LaunchRecord> {
    const launcher = await this.get(request.launcher_id);
    const args = this.argsFor(launcher, request.values ?? {});
    const signature = this.signature(launcher, args);

    const clash = (await this.records()).find(
      (r) => r.signature === signature && isActive(r),
    );
    if (clash) {
      throw new LauncherError(
        `already running as ${clash.adw_id} — open that run instead of starting a second one`,
        409,
      );
    }

    const workflow = this.within(launcher.workflow);
    if (!existsSync(workflow)) {
      throw new LauncherError(`workflow not found: ${launcher.workflow}`, 500);
    }

    // A launcher may take the run id as a PARAMETER — that is what resuming is:
    // rejoining a session that already exists. Minting one there would create an
    // empty new run instead of continuing the old one, so the typed value wins
    // and nothing is appended.
    const idParam = launcher.params.find((p) => p.flag === "--adw-id");
    const typedId = idParam ? String(request.values?.[idParam.name] ?? "").trim() : "";
    if (idParam && !/^[A-Za-z0-9_-]+$/.test(typedId)) {
      throw new LauncherError(`${idParam.label} must be an existing run id`);
    }
    const adwId = typedId || newAdwId();
    // The log lives in the run's own session dir, which the run itself will also
    // write into. Nothing else can hold this output: the process is detached and
    // has no console of its own.
    const logDir = resolve(this.sessionsDir, adwId);
    mkdirSync(logDir, { recursive: true });
    const logFile = join(logDir, "launch.log");
    const fd = openSync(logFile, "a");

    const cmd = ["uv", "run", launcher.workflow, ...args, "--adw-id", adwId];
    const child = Bun.spawn({
      cmd,
      cwd: this.repoRoot,
      stdin: "ignore",
      stdout: fd,
      stderr: fd,
      env: { ...process.env, PYTHONUTF8: "1" },
    });
    // Detached: the run outlives the visualizer. Restarting the UI must never
    // take a four-hour run down with it.
    child.unref();

    const record: LaunchRecord = {
      adw_id: adwId,
      launcher_id: launcher.id,
      label: launcher.label,
      signature,
      command: cmd.join(" "),
      log: logFile,
      pid: child.pid,
      started_at: new Date().toISOString(),
    };
    await this.append(record);
    return record;
  }

  /** The tail of a launch log — what to show when a run died before it traced. */
  async logTail(adwId: string, lines = 20): Promise<string | null> {
    const file = resolve(this.sessionsDir, adwId, "launch.log");
    if (!existsSync(file)) return null;
    const text = await Bun.file(file).text();
    return text.split(/\r?\n/).slice(-lines).join("\n").trim() || null;
  }
}
