<script setup lang="ts">
/**
 * The row form of a session on the L1 list — same data as SessionCard, laid out
 * for density instead of for a grid.
 *
 * Two differences that matter, both asked for:
 *  - ONE timeline track, not one per agent. Dot COLOR is the agent, dot SHAPE
 *    is the event type; the agent legend sits in the row's gutter. A card has
 *    room for four lanes, a row does not, and "who is working now" is the thing
 *    you scan a list for.
 *  - No cost chip, and no status chip either: the frame is green on success and
 *    red on failure, which is the whole of what the chip said. Runtime and
 *    tokens stay.
 *  - The request column is the run's GitHub refs, not its prompt. A review run
 *    is about a PR and the issue behind it; a development run is about an
 *    issue. "PR #239 · #233" identifies a run; the first 60 characters of an
 *    issue body do not.
 * Errors keep the red event color rather than their agent's — a failing dot
 * that blends into its lane is the one dot you must not miss.
 */
import { computed, onMounted, onUnmounted, shallowRef, watch } from 'vue'
import type { EventRow, SessionSummary } from '../lib/types'
import { archiveSession, fetchEvents, fetchFactoryStatus } from '../lib/api'
import { axisTicks, fmtDate, fmtOffset, ts } from '../lib/format'
import { agentColor, eventLabel, parsePayload } from '../lib/events'
import { hrefFor } from '../lib/router'
import StatChip from './StatChip.vue'
import PhaseBars from './PhaseBars.vue'
import FailureBar from './FailureBar.vue'

const props = defineProps<{ session: SessionSummary; nowMs: number }>()
const emit = defineEmits<{ archived: [adwId: string] }>()

// The row is an <a>; the button lives inside it, so the click must not
// navigate. Told the parent optimistically — the poll would take up to half a
// second to drop the row, and a triage click should feel instant.
async function archive(event: MouseEvent) {
  event.preventDefault()
  event.stopPropagation()
  emit('archived', props.session.adw_id)
  try {
    await archiveSession(props.session.adw_id)
  } catch {
    emit('archived', '') // signals the parent to re-sync from the server
  }
}

// Each row tails its own event stream: one full fetch on mount, then the same
// rowid-cursor poll as the trace view — but only while the run is live.
const events = shallowRef<EventRow[]>([])
let cursor = 0
let inflight = false
let timer: ReturnType<typeof setInterval> | undefined

function stopPolling() {
  clearInterval(timer)
  timer = undefined
}

async function pull() {
  if (inflight) return
  inflight = true
  try {
    const fresh: EventRow[] = []
    let page
    do {
      // Cursor pagination is inherently sequential: each request needs the previous cursor.
      // oxlint-disable-next-line no-await-in-loop
      page = await fetchEvents(props.session.adw_id, cursor, 1000)
      cursor = Math.max(cursor, page.cursor)
      fresh.push(...page.events)
    } while (page.has_more)
    if (fresh.length) events.value = [...events.value, ...fresh]
    if (props.session.status !== 'running') stopPolling()
  } catch {
    /* the list view surfaces api errors; a row just retries next poll */
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void pull()
  if (props.session.status === 'running') timer = setInterval(() => void pull(), 500)
})

onUnmounted(stopPolling)

watch(
  () => props.session.status,
  (status) => {
    if (status === 'running' && !timer) timer = setInterval(() => void pull(), 500)
    // On the transition out of running, one last pull drains the tail and stops the timer.
    else if (status !== 'running') void pull()
  },
)

const running = computed(() => props.session.status === 'running')
const failure = computed(() => props.session.failure ?? null)

// ── GitHub refs ──────────────────────────────────────────────────────────────
// Runs do not carry pr/issue columns, so the numbers are recovered from what a
// run does write. Best source first: the workflows log them as structured
// fields ({"pr": "PR #239", "issue": "#233"}) in an early phase, which is exact.
// The fallbacks cover runs that never logged them — a development run states
// its issue in the request text, and every worktree branch is named after it.

const NUM = /(\d+)/

function digits(v: unknown): string | null {
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  if (typeof v !== 'string') return null
  return NUM.exec(v)?.[1] ?? null
}

const refs = computed(() => {
  let pr: string | null = null
  let issue: string | null = null

  for (const e of events.value) {
    if (pr && issue) break
    const p = parsePayload(e.payload_json)
    if (!p) continue
    pr ??= digits(p.pr ?? p.pr_number)
    issue ??= digits(p.issue ?? p.issue_number)
  }

  // "--- BEGIN ISSUE #233 TITLE ---" — how a development run states its
  // subject when it logged no structured field. Review runs always log one, so
  // there is no matching text fallback for the PR.
  issue ??= /issue\s*#?(\d+)/i.exec(props.session.request ?? '')?.[1] ?? null

  return { pr, issue }
})

const hasRefs = computed(() => refs.value.pr !== null || refs.value.issue !== null)

// ── factory status ───────────────────────────────────────────────────────────
// Separate from the run's own status: this is what adws_factory thinks of the
// ISSUE (running/held/escalated/merged/parked), not this session. It only
// exists once an issue number is known, and it changes slowly, so it is
// fetched once (and again every few seconds while the run is live) rather
// than on the 500ms event-poll cadence.
const factoryStatus = shallowRef<string | null>(null)
// "running" is the common case and goes stale the instant the tick moves on —
// the user already knows which run is running, so it is worth fetching (other
// states still need it) but not worth displaying.
const visibleFactoryStatus = computed(() =>
  factoryStatus.value && factoryStatus.value !== 'running' ? factoryStatus.value : null,
)
let factoryTimer: ReturnType<typeof setInterval> | undefined

async function pullFactoryStatus() {
  const issue = refs.value.issue
  if (issue === null) return
  try {
    factoryStatus.value = await fetchFactoryStatus(Number(issue), refs.value.pr ? Number(refs.value.pr) : null)
  } catch {
    /* stays at its last known value; not worth surfacing as a row error */
  }
}

watch(
  () => refs.value.issue,
  (issue) => {
    if (issue === null) return
    void pullFactoryStatus()
    if (running.value && !factoryTimer) {
      factoryTimer = setInterval(() => void pullFactoryStatus(), 5000)
    }
  },
  { immediate: true },
)

watch(running, (isRunning) => {
  if (!isRunning && factoryTimer) {
    clearInterval(factoryTimer)
    factoryTimer = undefined
  }
})

onUnmounted(() => {
  if (factoryTimer) clearInterval(factoryTimer)
})

const range = computed(() => {
  const s = props.session
  let t0 = ts(s.started_at)
  if (!Number.isFinite(t0)) {
    t0 = Math.min(...events.value.map((e) => ts(e.started_at)).filter(Number.isFinite))
  }
  if (!Number.isFinite(t0)) t0 = props.nowMs
  let t1 = running.value ? props.nowMs : ts(s.ended_at)
  if (!Number.isFinite(t1)) {
    t1 = Math.max(...events.value.map((e) => ts(e.started_at)).filter(Number.isFinite))
  }
  if (!Number.isFinite(t1)) t1 = t0 + 1000
  return { t0, span: Math.max(t1 - t0, 1000) }
})

const ticks = computed(() => axisTicks(range.value.span, 6))

/** Agents in first-appearance order, with the color the whole row keys off. */
const agents = computed(() => {
  const owners: string[] = []
  for (const p of props.session.phases ?? []) {
    if (p.kind !== 'agent' || !p.owner) continue
    if (!owners.includes(p.owner)) owners.push(p.owner)
  }
  return owners.map((owner, i) => {
    // /api/sessions embeds agents so the labels can use config colors with no
    // extra request; historical sessions return color null → fallback palette.
    const info = (props.session.agents ?? []).find((a) => a.agent === owner)
    return {
      owner,
      color: agentColor(info?.color, null, i),
      title: info?.model ? `${owner} — ${info.model}` : owner,
    }
  })
})

const ownerByPhase = computed(() => {
  const map = new Map<string, string>()
  for (const p of props.session.phases ?? []) {
    if (p.kind === 'agent' && p.owner) map.set(p.phase_id, p.owner)
  }
  return map
})

// Shape carries the event type now that color carries the agent. Only these
// types get a mark; anything else the tracer writes stays off the track, the
// same set the card drew.
const EVENT_SHAPE: Record<string, 'start' | 'call' | 'handoff' | 'end' | 'error'> = {
  agent_start: 'start',
  tool_call: 'call',
  handoff: 'handoff',
  agent_end: 'end',
  error: 'error',
  gate_fail: 'error',
}

const ERROR_COLOR = '#d97b73'

interface Mark {
  id: string
  xPct: number
  color: string
  shape: string
  title: string
  latest: boolean
}

const marks = computed<Mark[]>(() => {
  const { t0, span } = range.value
  const colorOf = new Map(agents.value.map((a) => [a.owner, a.color]))
  const out: Mark[] = []
  let latest: Mark | null = null
  let latestT = -Infinity

  for (const e of events.value) {
    const shape = e.type ? EVENT_SHAPE[e.type] : undefined
    if (!shape) continue
    const owner = e.phase_id ? ownerByPhase.value.get(e.phase_id) : undefined
    const t = ts(e.started_at)
    if (!Number.isFinite(t)) continue
    const mark: Mark = {
      id: e.event_id,
      xPct: Math.min(Math.max(((t - t0) / span) * 100, 0), 100),
      color: shape === 'error' ? ERROR_COLOR : (colorOf.get(owner ?? '') ?? 'var(--faint)'),
      shape,
      title: `${owner ? `${owner} · ` : ''}${e.type} ${eventLabel(e)} at ${fmtOffset(t - t0)}`,
      latest: false,
    }
    out.push(mark)
    if (t >= latestT) {
      latestT = t
      latest = mark
    }
  }
  if (running.value && latest) latest.latest = true
  return out
})

const durationMs = computed(() => {
  const s = props.session
  const start = ts(s.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = running.value ? props.nowMs : ts(s.ended_at)
  return (Number.isFinite(end) ? end : props.nowMs) - start
})
</script>

<template>
  <a class="row" :class="session.status" :href="hrefFor(session.adw_id)">
    <!-- Line 1: what the run is, and how it went. -->
    <div class="line-meta">
      <span class="r-date dim">{{ fmtDate(session.started_at) }}</span>
      <span class="r-id">{{ session.adw_id }}</span>
      <span class="r-adw" :title="session.adw_name ?? ''">{{ session.adw_name ?? '—' }}</span>
      <!-- The prompt is gone from the row, so it hangs on the refs as a title -
           the one place you would look for "what was this run actually asked". -->
      <span class="r-refs" :title="session.request ?? ''">
        <template v-if="hasRefs">
          <span v-if="refs.pr" class="ref pr">PR #{{ refs.pr }}</span>
          <span v-if="refs.issue" class="ref issue">#{{ refs.issue }}</span>
          <span
            v-if="visibleFactoryStatus"
            class="ref factory-status"
            :class="visibleFactoryStatus"
            title="Factory item status — from adws_factory, not this run"
            >{{ visibleFactoryStatus }}</span
          >
        </template>
        <span v-else class="faint">no ref</span>
      </span>
      <PhaseBars :phases="session.phases ?? []" />
      <StatChip kind="runtime" :value="durationMs" />
      <StatChip kind="tokens" :value="session.total_tokens" />
      <button
        class="r-archive"
        type="button"
        title="Archive — remove this run from review"
        aria-label="Archive run"
        @click="archive"
      >
        ×
      </button>
    </div>

    <!-- Line 2: every agent's activity on one track. -->
    <div class="line-tl">
      <span class="tl-legend">
        <span v-for="a in agents" :key="a.owner" class="lg" :style="{ color: a.color }" :title="a.title">
          <span class="lg-swatch" :style="{ background: a.color }" />{{ a.owner }}
        </span>
        <span v-if="!agents.length" class="faint">no agents</span>
      </span>
      <span class="tl-track">
        <span
          v-for="t in ticks"
          :key="`t${t.pct}`"
          class="tl-tick"
          :class="{ edge: t.pct === 0 }"
          :style="{ left: `${t.pct}%` }"
          >{{ t.label }}</span
        >
        <span
          v-for="m in marks"
          :key="m.id"
          class="tl-mark"
          :class="[m.shape, { latest: m.latest }]"
          :style="{ left: `${m.xPct}%`, background: m.color, color: m.color }"
          :title="m.title"
        />
        <span v-if="!marks.length" class="tl-empty faint">no agent activity yet</span>
      </span>
    </div>

    <!-- Only when it failed, so a healthy row stays two lines. -->
    <FailureBar v-if="failure" :failure="failure" compact />
  </a>
</template>

<style scoped>
.row {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 18px;
  position: relative; /* anchors the archive button */
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  transition:
    border-color 0.18s ease,
    background 0.18s ease;
}

.row:hover {
  border-color: rgba(135, 144, 194, 0.45);
  background: rgba(135, 144, 194, 0.05);
}

/* The frame IS the verdict — this is what replaced the status chip. Left
   border thickened so the color reads down a long list without the whole row
   glowing. */
.row.running {
  border-color: rgba(127, 166, 212, 0.6);
  border-left: 3px solid var(--blue);
}

.row.success {
  border-color: rgba(108, 186, 143, 0.45);
  border-left: 3px solid var(--green);
}

.row.fail {
  border-color: rgba(217, 123, 115, 0.6);
  border-left: 3px solid var(--red);
}

/* ── line 1 ─────────────────────────────────────────────────────────────── */

.line-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  font-size: 16px;
}

.r-id {
  flex: none;
  font-family: var(--mono);
  font-weight: 700;
  color: var(--purple);
}

.r-adw {
  /* Fixed so ids, names and requests line up down the list — a ragged left
     edge on the request column is what makes a row list unreadable. */
  flex: none;
  width: 190px;
  font-family: var(--mono);
  color: var(--cyan);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* The refs are the elastic column now; everything right of them is fixed, so
   they absorb the whole window width and the phase strip stays put. */
.r-refs {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--mono);
  font-size: 15px;
  white-space: nowrap;
}

.ref {
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
}

.ref.pr {
  color: var(--green);
  border-color: rgba(108, 186, 143, 0.35);
}

.ref.issue {
  color: var(--dim);
}

.ref.factory-status {
  text-transform: capitalize;
  font-size: 13px;
}

.ref.factory-status.held {
  color: var(--yellow, #eab308);
  border-color: rgba(234, 179, 8, 0.35);
}

.ref.factory-status.escalated {
  color: var(--red);
  border-color: rgba(217, 123, 115, 0.35);
}

.ref.factory-status.merged {
  color: var(--green);
  border-color: rgba(108, 186, 143, 0.35);
}

.ref.factory-status.parked {
  color: var(--dim);
  border-color: var(--border-soft);
}

/* Leads the row: when the run happened is the first thing you scan for. */
.r-date {
  flex: none;
  font-family: var(--mono);
  color: var(--dim);
  white-space: nowrap;
}

.r-archive {
  flex: none;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--dim);
  font-family: inherit;
  font-size: 19px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 0.15s ease,
    background 0.15s ease,
    color 0.15s ease;
}

/* Hidden until the row is hovered — 50 runs should read as runs, not as a
   column of close buttons. Focus reveals it too, so keyboards are not excluded. */
.row:hover .r-archive,
.r-archive:focus-visible {
  opacity: 1;
}

.r-archive:hover {
  background: rgba(217, 123, 115, 0.16);
  color: #d97b73;
}

/* ── line 2 ─────────────────────────────────────────────────────────────── */

.line-tl {
  display: flex;
  align-items: stretch;
  gap: 14px;
  height: 30px;
}

.tl-legend {
  /* Same width as the request column's left edge is not worth chasing; a fixed
     legend gutter is enough to keep every track starting at one x. */
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 300px;
  overflow: hidden;
  white-space: nowrap;
  font-size: 14px;
}

.lg {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.lg-swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.tl-track {
  position: relative;
  flex: 1;
  min-width: 0;
  border-bottom: 1px solid var(--border-soft);
}

.tl-tick {
  position: absolute;
  bottom: 1px;
  transform: translateX(-50%);
  font-family: var(--mono);
  font-size: 12px;
  color: var(--faint);
  white-space: nowrap;
}

.tl-tick.edge {
  transform: none;
}

.tl-empty {
  position: absolute;
  top: 50%;
  left: 0;
  transform: translateY(-50%);
  font-size: 14px;
}

/* Marks sit on a line above the tick labels, so the two never collide. */
.tl-mark {
  position: absolute;
  top: 9px;
  width: 9px;
  height: 9px;
  transform: translate(-50%, -50%);
}

/* Shape = event type. Round is the common case (a tool call); the rarer
   lifecycle events get an outline or a corner so they stand out in a dense
   run without needing a color of their own. */
.tl-mark.call {
  border-radius: 50%;
}

.tl-mark.start {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: transparent !important;
  border: 2px solid currentColor;
}

.tl-mark.end {
  border-radius: 2px;
}

.tl-mark.handoff {
  border-radius: 1px;
  transform: translate(-50%, -50%) rotate(45deg);
}

.tl-mark.error {
  width: 11px;
  height: 11px;
  border-radius: 2px;
  transform: translate(-50%, -50%) rotate(45deg);
}

.tl-mark.latest {
  width: 13px;
  height: 13px;
  box-shadow: 0 0 10px currentColor;
  animation: pulse 1.4s ease-in-out infinite;
}

.fail-bar {
  flex: none;
}
</style>
