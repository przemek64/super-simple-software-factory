import type {
  Envelope,
  EventRow,
  EventsPage,
  GateResult,
  HealthResponse,
  LaunchRecord,
  Launcher,
  LaunchStatus,
  PromptsResponse,
  SessionDetail,
  SessionSummary,
} from './types'

async function getJson(url: string): Promise<unknown> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`GET ${url} → ${res.status}`)
  return res.json()
}

export function fetchSessions(): Promise<SessionSummary[]> {
  return getJson('/api/sessions') as Promise<SessionSummary[]>
}

export async function fetchSession(adwId: string): Promise<SessionDetail> {
  const detail = (await getJson(`/api/sessions/${encodeURIComponent(adwId)}`)) as SessionDetail
  return {
    session: detail.session,
    usage: detail.usage ?? { read: 0, written: 0 },
    phases: detail.phases ?? [],
    agents: detail.agents ?? [],
    // Absent when the UI is newer than the server it is talking to; "no reason
    // to show" is the right reading of that, not an error.
    failure: detail.failure ?? null,
  }
}

export async function fetchEvents(adwId: string, after: number, limit = 500): Promise<EventsPage> {
  const page = (await getJson(
    `/api/sessions/${encodeURIComponent(adwId)}/events?after=${after}&limit=${limit}`,
  )) as EventsPage | EventRow[]
  if (Array.isArray(page)) {
    const cursor = page.reduce((max, e) => Math.max(max, e.rowid), after)
    return { events: page, cursor, has_more: page.length === limit }
  }
  return { events: page.events ?? [], cursor: page.cursor ?? after, has_more: page.has_more ?? false }
}

/** Archive a run out of the review list (or restore it with archived=false). */
export async function archiveSession(adwId: string, archived = true): Promise<void> {
  const url = `/api/sessions/${encodeURIComponent(adwId)}/archive`
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ archived }),
  })
  if (!res.ok) throw new Error(`POST ${url} → ${res.status}`)
}

// ── dashboard ───────────────────────────────────────────────────────────────

export function fetchLaunchers(): Promise<Launcher[]> {
  return getJson('/api/launchers') as Promise<Launcher[]>
}

export function fetchLaunches(): Promise<LaunchStatus[]> {
  return getJson('/api/launches') as Promise<LaunchStatus[]>
}

/**
 * Start a run and get its id back, so the caller can go straight to the trace.
 *
 * A refusal carries a sentence worth showing — "already running as a1b2c3d4",
 * "Issue number is required" — so the message is preferred over the status.
 */
export async function launchRun(
  launcherId: string,
  values: Record<string, string>,
): Promise<LaunchRecord> {
  const res = await fetch('/api/launch', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ launcher_id: launcherId, values }),
  })
  const data = (await res.json().catch(() => null)) as (LaunchRecord & { error?: string }) | null
  if (!res.ok) throw new Error(data?.error ?? `POST /api/launch → ${res.status}`)
  if (!data) throw new Error('launch returned no body')
  return data
}

export function diagramUrl(launcherId: string): string {
  return `/api/launchers/${encodeURIComponent(launcherId)}/diagram`
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson('/api/health') as Promise<HealthResponse>
}

// PhaseDetail imports the prompts type from here alongside fetchPrompts.
export type { PromptsResponse }

export async function fetchPrompts(adwId: string, agent: string): Promise<PromptsResponse> {
  const res = await fetch(
    `/api/sessions/${encodeURIComponent(adwId)}/agents/${encodeURIComponent(agent)}/prompts`,
  )
  // Not recorded (or endpoint not deployed yet) renders as "no prompts", not an error.
  if (res.status === 404) return { system: null, user: null }
  if (!res.ok) throw new Error(`GET prompts → ${res.status}`)
  const data = (await res.json()) as Partial<PromptsResponse>
  return { system: data.system ?? null, user: data.user ?? null }
}

export function fetchEnvelopes(adwId: string): Promise<Envelope[]> {
  return getJson(`/api/sessions/${encodeURIComponent(adwId)}/envelopes`) as Promise<Envelope[]>
}

export function fetchGates(adwId: string): Promise<GateResult[]> {
  return getJson(`/api/sessions/${encodeURIComponent(adwId)}/gates`) as Promise<GateResult[]>
}

/** null when the item has no factory history (never touched adws_factory). */
export async function fetchFactoryStatus(issue: number, pr: number | null): Promise<string | null> {
  const q = pr !== null ? `issue=${issue}&pr=${pr}` : `issue=${issue}`
  const data = (await getJson(`/api/factory-status?${q}`)) as { status: string | null }
  return data.status ?? null
}

export interface LedgerPr {
  number: number
  title: string
  state: string
  isDraft: boolean
  live: boolean
}

export interface LedgerIssue {
  number: number
  title: string
  status: string | null
  stage: string | null
  prs: LedgerPr[]
}

export function fetchIssues(): Promise<LedgerIssue[]> {
  return getJson('/api/issues') as Promise<LedgerIssue[]>
}
