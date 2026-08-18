<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watchEffect } from 'vue'
import type {
  AgentSession,
  AgentStartPayload,
  Envelope,
  EventRow,
  FailureReason,
  GateResult,
  Phase,
  Session,
  SessionUsage,
} from '../lib/types'
import { fetchEnvelopes, fetchEvents, fetchGates, fetchSession } from '../lib/api'
import { axisTicks, fmtDate, payloadOk, ts } from '../lib/format'
import { modelIcon, modelName } from '../lib/models'
import { agentColor, hexAlpha, parseAgentStart } from '../lib/events'
import {
  CODE_COLOR,
  ENGINEER_COLOR,
  KIND_ICONS,
  contextFill,
  contextLabel,
  laneContext,
  type Lane,
} from '../lib/lanes'
import { layoutMode } from '../lib/view'
import { navigate, phaseCrumb } from '../lib/router'
import StatusChip from './StatusChip.vue'
import StatChip from './StatChip.vue'
import PhaseDetail from './PhaseDetail.vue'
import VerticalWaterfall from './VerticalWaterfall.vue'
import FailureBar from './FailureBar.vue'

const props = defineProps<{ adwId: string; phaseId: string | null }>()

const session = ref<Session | null>(null)
const phases = ref<Phase[]>([])
const agents = ref<AgentSession[]>([])
const usage = ref<SessionUsage>({ read: 0, written: 0 })
const failure = ref<FailureReason | null>(null)
const events = ref<EventRow[]>([])
const envelopes = ref<Envelope[]>([])
const gates = ref<GateResult[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let cursor = 0
let inflight = false
let timer: ReturnType<typeof setInterval> | undefined

const SIDE_TABLE_TYPES = new Set(['gate_pass', 'gate_fail', 'handoff', 'agent_end', 'phase_end', 'error'])

async function tick() {
  if (inflight) return
  inflight = true
  try {
    const detail = await fetchSession(props.adwId)
    session.value = detail.session
    phases.value = detail.phases.toSorted((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
    agents.value = detail.agents
    usage.value = detail.usage
    failure.value = detail.failure

    const fresh: EventRow[] = []
    let page
    do {
      // Cursor pagination is inherently sequential: each request needs the previous cursor.
      // oxlint-disable-next-line no-await-in-loop
      page = await fetchEvents(props.adwId, cursor, 1000)
      cursor = Math.max(cursor, page.cursor)
      fresh.push(...page.events)
    } while (page.has_more)
    if (fresh.length) events.value = [...events.value, ...fresh]

    // Envelopes and gates only gain rows around phase/agent boundaries — refetch
    // on those events instead of every tick.
    if (!loaded.value || fresh.some((e) => e.type !== null && SIDE_TABLE_TYPES.has(e.type))) {
      const [env, g] = await Promise.all([fetchEnvelopes(props.adwId), fetchGates(props.adwId)])
      envelopes.value = env
      gates.value = g
    }

    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 500)
})

onUnmounted(() => {
  clearInterval(timer)
  phaseCrumb.value = null
})

const selectedPhase = computed(
  () => phases.value.find((p) => p.phase_id === props.phaseId) ?? null,
)

watchEffect(() => {
  phaseCrumb.value = selectedPhase.value?.name ?? null
})

// ── Lanes ────────────────────────────────────────────────────────────────────
// The lane model itself lives in lib/lanes.ts — both layouts build the same
// lanes and only differ in how they draw them.

const NUM = new Intl.NumberFormat('en-US')

// A live agent's model/thinking/color arrive on its agent_start event before
// any agent_sessions row exists; attribute each start to its phase's owner.
const ownerStart = computed<Record<string, AgentStartPayload>>(() => {
  const ownerByPhase = new Map<string, string | null>(
    phases.value.map((p) => [p.phase_id, p.owner]),
  )
  const meta: Record<string, AgentStartPayload> = {}
  for (const e of events.value) {
    if (e.type !== 'agent_start') continue
    const owner = (e.phase_id ? ownerByPhase.get(e.phase_id) : null) ?? e.name
    if (!owner || meta[owner]) continue
    const payload = parseAgentStart(e)
    if (payload) meta[owner] = payload
  }
  return meta
})

const lanes = computed<Lane[]>(() => {
  const ph = phases.value
  const agentOwners: string[] = []
  for (const p of ph) {
    if (p.kind === 'agent' && p.owner && !agentOwners.includes(p.owner)) agentOwners.push(p.owner)
  }
  const codePhases = ph.filter((p) => p.kind === 'code')
  const out: Lane[] = [
    {
      id: 'engineer',
      label: session.value?.engineer ?? 'engineer',
      model: null,
      context: null,
      metaLines: ['engineer'],
      color: ENGINEER_COLOR,
      kind: 'engineer' as const,
      phases: ph.filter((p) => p.kind === 'engineer'),
    },
  ]
  if (codePhases.length) {
    out.push({
      id: 'code',
      label: 'code',
      model: null,
      context: null,
      metaLines: ['workspace'],
      color: CODE_COLOR,
      kind: 'code' as const,
      phases: codePhases,
    })
  }
  for (const [i, owner] of agentOwners.entries()) {
    const info = agents.value.find((a) => a.agent === owner)
    const start = ownerStart.value[owner]
    out.push({
      id: `agent:${owner}`,
      label: owner,
      // The model is the lane's whole story; thinking level lives in the
      // phase detail's agent config section.
      model: info?.model ?? start?.model ?? null,
      context: laneContext(info),
      metaLines: [],
      color: agentColor(info?.color, start?.color, i),
      kind: 'agent' as const,
      phases: ph.filter((p) => p.kind === 'agent' && p.owner === owner),
    })
  }
  return out
})

// ── Timeline geometry ────────────────────────────────────────────────────────

const range = computed(() => {
  let t0 = Infinity
  let t1 = -Infinity
  const s = session.value
  const sStart = ts(s?.started_at)
  const sEnd = ts(s?.ended_at)
  if (Number.isFinite(sStart)) t0 = Math.min(t0, sStart)
  if (Number.isFinite(sEnd)) t1 = Math.max(t1, sEnd)
  for (const p of phases.value) {
    const a = ts(p.started_at)
    const b = ts(p.ended_at)
    if (Number.isFinite(a)) {
      t0 = Math.min(t0, a)
      t1 = Math.max(t1, a)
    }
    if (Number.isFinite(b)) t1 = Math.max(t1, b)
  }
  if (s?.status === 'running') t1 = Math.max(t1, nowMs.value)
  if (!Number.isFinite(t0)) {
    t0 = nowMs.value
    t1 = t0 + 1000
  }
  if (t1 - t0 < 1000) t1 = t0 + 1000
  return { t0, t1, span: t1 - t0 }
})

// The engineer's request opens the run and owns the start of the timeline: it
// gets an exclusive leading zone, and every later phase maps into the rest —
// nothing can render on top of it.
const REQ_ZONE_PCT = 16

const requestPhase = computed(
  () => phases.value.find((p) => p.kind === 'engineer' && p.started_at) ?? null,
)

const zonePct = computed(() => (requestPhase.value ? REQ_ZONE_PCT : 0))

/**
 * Where the post-request timeline begins, in ms.
 *
 * The earliest non-engineer phase start, not the request phase's end: a later
 * ADW joining the session pushes the request row's ended_at forward, which
 * would otherwise throw every already-finished phase behind the origin.
 */
const originMs = computed(() => {
  const { t0 } = range.value
  const req = requestPhase.value
  if (!req) return t0
  let earliest = Infinity
  for (const p of phases.value) {
    if (p.kind === 'engineer') continue
    const s = ts(p.started_at)
    if (Number.isFinite(s)) earliest = Math.min(earliest, s)
  }
  if (Number.isFinite(earliest)) return Math.max(earliest, t0)
  const end = ts(req.ended_at ?? req.started_at)
  return Number.isFinite(end) ? Math.max(end, t0) : t0
})

const postSpan = computed(() => Math.max(range.value.t1 - originMs.value, 1000))

const ticks = computed(() => {
  const zone = zonePct.value
  return axisTicks(postSpan.value, 7).map((t) => ({
    pct: zone + (t.pct * (100 - zone)) / 100,
    label: t.label,
  }))
})

/**
 * Adjusted layout for every timed phase, in track-%.
 *
 * Phases are sequential by doctrine, and the render must say so: when a
 * near-zero phase (a git commit) is widened to a readable floor, every later
 * block shifts right by the same amount instead of being overlapped, and the
 * whole layout is normalized back into the track. Blocks may squeeze a hair;
 * they never stack.
 */
const MIN_BLOCK_PCT = 3.5

const blockLayout = computed<Record<string, { left: number; width: number }>>(() => {
  const zone = zonePct.value
  const avail = 100 - zone - 0.4 // hair of right margin
  const t0 = originMs.value
  const span = postSpan.value
  const reqId = requestPhase.value?.phase_id

  const timed = phases.value
    .filter((p) => p.phase_id !== reqId && Number.isFinite(ts(p.started_at)))
    .map((p) => {
      const start = ts(p.started_at)
      let end = ts(p.ended_at)
      if (!Number.isFinite(end)) end = p.status === 'running' ? nowMs.value : start
      return {
        id: p.phase_id,
        start,
        left: ((start - t0) / span) * avail,
        width: ((Math.max(end, start) - start) / span) * avail,
      }
    })
    .toSorted((a, b) => a.start - b.start)

  let shift = 0
  let prevEdge = 0
  const rows: { id: string; left: number; width: number }[] = []
  for (const b of timed) {
    let left = b.left + shift
    if (left < prevEdge) {
      shift += prevEdge - left
      left = prevEdge
    }
    const width = Math.max(b.width, MIN_BLOCK_PCT)
    shift += width - b.width
    prevEdge = left + width
    rows.push({ id: b.id, left, width })
  }

  const scale = avail / Math.max(prevEdge, avail)
  const out: Record<string, { left: number; width: number }> = {}
  for (const r of rows) out[r.id] = { left: zone + r.left * scale, width: r.width * scale }
  return out
})

function blockGeom(p: Phase): { left: string; width: string } | null {
  // The request block fills its reserved zone, nothing else ever enters it.
  if (p.phase_id === requestPhase.value?.phase_id && zonePct.value > 0) {
    return { left: '0.4%', width: `${zonePct.value - 0.8}%` }
  }
  const geom = blockLayout.value[p.phase_id]
  if (!geom) return null
  return { left: `${geom.left}%`, width: `${geom.width}%` }
}

function blockStyle(p: Phase, lane: Lane): Record<string, string> | undefined {
  const geom = blockGeom(p)
  if (!geom) return undefined
  return {
    left: geom.left,
    width: geom.width,
    background: `linear-gradient(180deg, ${hexAlpha(lane.color, 0.2)}, ${hexAlpha(lane.color, 0.05)})`,
    borderColor: p.status === 'fail' ? 'rgba(217, 123, 115, 0.8)' : hexAlpha(lane.color, 0.55),
    '--lane-glow': hexAlpha(lane.color, 0.28),
  }
}

function blockDurationMs(p: Phase): number {
  const start = ts(p.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = p.status === 'running' ? nowMs.value : ts(p.ended_at)
  if (!Number.isFinite(end)) return NaN
  return end - start
}

const STATUS_GLYPH: Record<string, string> = {
  success: '✓',
  fail: '✗',
  running: '●',
  queued: '○',
}

// Tool-call tick marks inside a phase block, positioned within the block's own span.
interface ToolTick {
  t: number
  ok: boolean
}

const toolTicks = computed(() => {
  const map: Record<string, ToolTick[]> = {}
  for (const e of events.value) {
    if (e.type !== 'tool_call' || !e.phase_id) continue
    map[e.phase_id] ??= []
    map[e.phase_id]?.push({ t: ts(e.started_at), ok: payloadOk(e.payload_json) })
  }
  return map
})

function ticksFor(p: Phase): { x: number; ok: boolean }[] {
  const start = ts(p.started_at)
  if (!Number.isFinite(start)) return []
  let end = ts(p.ended_at)
  if (!Number.isFinite(end)) end = p.status === 'running' ? nowMs.value : start
  const width = Math.max(end - start, 1)
  return (toolTicks.value[p.phase_id] ?? [])
    .filter((mark) => Number.isFinite(mark.t))
    .map((mark) => ({
      x: Math.min(Math.max(((mark.t - start) / width) * 100, 1), 99),
      ok: mark.ok,
    }))
}

/**
 * Minimum track width, in px.
 *
 * Block widths are percentages of the track, floored at MIN_BLOCK_PCT — so on a
 * viewport-wide track a busy run squeezes every block down to ~50px and the
 * names and descriptions ellipse away to nothing. Give the track a pixel floor
 * that grows with the number of blocks instead, and let the waterfall scroll
 * horizontally: MIN_BLOCK_PCT of that floor stays wide enough to read.
 */
const TRACK_MIN_PX = 1200
const PX_PER_BLOCK = 136

const trackMinPx = computed(() => {
  const timed = Object.keys(blockLayout.value).length + (requestPhase.value ? 1 : 0)
  const queued = phases.value.filter((p) => !p.started_at).length
  return Math.max(TRACK_MIN_PX, (timed + queued) * PX_PER_BLOCK)
})

// ── Top scrollbar ────────────────────────────────────────────────────────────
//
// The chart is tall enough that a scrollbar under it sits off-screen, so the
// affordance is mirrored above: an empty strip whose spacer matches the row's
// max-content width, with scrollLeft mirrored both ways. `syncing` breaks the
// feedback loop — setting one element's scrollLeft fires the other's handler.
const topScrollEl = ref<HTMLElement | null>(null)
const waterfallEl = ref<HTMLElement | null>(null)
let syncing = false

function mirrorScroll(from: HTMLElement | null, to: HTMLElement | null) {
  if (syncing || !from || !to) return
  syncing = true
  to.scrollLeft = from.scrollLeft
  // Release after the paint that the assignment schedules, not this tick.
  requestAnimationFrame(() => {
    syncing = false
  })
}

const queuedByLane = computed(() => {
  const map: Record<string, Phase[]> = {}
  for (const lane of lanes.value) {
    map[lane.id] = lane.phases.filter((p) => !p.started_at)
  }
  return map
})

const sessionDurationMs = computed(() => {
  const s = session.value
  if (!s) return NaN
  const start = ts(s.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = s.status === 'running' ? nowMs.value : ts(s.ended_at)
  return (Number.isFinite(end) ? end : nowMs.value) - start
})

function selectPhase(p: Phase) {
  navigate(props.adwId, p.phase_id === props.phaseId ? null : p.phase_id)
}
</script>

<template>
  <div class="trace">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="session" class="run-strip">
      <span class="request" :title="session.request ?? ''">{{ session.request }}</span>
      <StatusChip :status="session.status ?? 'fail'" />
      <span class="dim">started {{ fmtDate(session.started_at) }}</span>
      <span class="run-stats">
        <StatChip kind="cost" :value="session.total_cost" />
        <StatChip kind="runtime" :value="sessionDurationMs" />
        <StatChip kind="tokens" :value="session.total_tokens" />
        <StatChip kind="read" :value="usage.read" />
        <StatChip kind="written" :value="usage.written" />
      </span>
    </div>

    <FailureBar v-if="failure" :failure="failure" />

    <VerticalWaterfall
      v-if="phases.length && layoutMode === 'vertical'"
      :lanes="lanes"
      :phases="phases"
      :events="events"
      :now-ms="nowMs"
      :phase-id="phaseId"
      @select="selectPhase"
    />

    <div
      v-else-if="phases.length"
      class="chart"
      :style="{ '--track-min': `${trackMinPx}px` }"
    >
      <div
        ref="topScrollEl"
        class="hscroll-top"
        @scroll="mirrorScroll(topScrollEl, waterfallEl)"
      >
        <div class="hscroll-spacer" />
      </div>
      <div ref="waterfallEl" class="waterfall" @scroll="mirrorScroll(waterfallEl, topScrollEl)">
        <div class="row axis-row">
          <div class="label" />
          <div class="track">
            <span v-if="zonePct" class="zone-head" :style="{ width: `${zonePct}%` }">request</span>
            <span
              v-for="(t, i) in ticks"
              :key="i"
              class="axis-label"
              :style="{ left: `${t.pct}%` }"
              >{{ t.label }}</span
            >
          </div>
        </div>

        <div v-for="lane in lanes" :key="lane.id" class="row lane" :class="`kind-${lane.kind}`">
          <div class="label">
            <span class="lane-name" :style="{ color: lane.color }">
              <component :is="KIND_ICONS[lane.kind]" class="lane-icon" :size="18" :stroke-width="2" />
              {{ lane.label }}
            </span>
            <span v-if="lane.model" class="lane-meta lane-model" :title="lane.model">
              <img v-if="modelIcon(lane.model)" class="model-icon" :src="modelIcon(lane.model)!" alt="" />
              {{ modelName(lane.model) }}
            </span>
            <span
              v-if="lane.context"
              class="lane-ctx"
              :title="`${NUM.format(lane.context.used)} / ${NUM.format(lane.context.window)} tokens used · ${NUM.format(lane.context.window - lane.context.used)} remaining`"
            >
              <span class="ctx-head">
                <span class="ctx-label">Context</span>
                <span class="ctx-pct">{{ contextLabel(lane.context) }}</span>
              </span>
              <span class="ctx-bar">
                <span
                  class="ctx-fill"
                  :style="{
                    width: contextFill(lane.context),
                    background: `linear-gradient(90deg, ${hexAlpha(lane.color, 0.55)}, ${lane.color})`,
                    boxShadow: `0 0 10px ${hexAlpha(lane.color, 0.45)}`,
                  }"
                />
              </span>
            </span>
            <span v-for="(line, i) in lane.metaLines" :key="i" class="lane-meta">{{ line }}</span>
          </div>
          <div class="track">
            <span v-if="zonePct" class="zone-divider" :style="{ left: `${zonePct}%` }" />
            <span v-for="(t, i) in ticks" :key="i" class="gridline" :style="{ left: `${t.pct}%` }" />
            <template v-for="p in lane.phases" :key="p.phase_id">
              <button
                v-if="blockGeom(p)"
                class="block"
                :class="[p.status, { selected: p.phase_id === phaseId }]"
                :style="blockStyle(p, lane)"
                :title="`${p.name} — ${p.status}${p.description ? `\n${p.description}` : ''}`"
                @click="selectPhase(p)"
              >
                <span class="b-top">
                  <span class="b-status" :class="p.status">{{
                    STATUS_GLYPH[p.status ?? ''] ?? '○'
                  }}</span>
                  <span class="b-name">{{ p.name }}</span>
                  <StatChip
                    v-if="Number.isFinite(blockDurationMs(p))"
                    class="b-dur"
                    kind="runtime"
                    compact
                    :value="blockDurationMs(p)"
                  />
                </span>
                <span class="b-desc">{{ p.description }}</span>
                <span
                  v-for="(tick, i) in ticksFor(p)"
                  :key="i"
                  class="tool-tick"
                  :class="{ err: !tick.ok }"
                  :style="{ left: `${tick.x}%` }"
                />
              </button>
            </template>
            <button
              v-for="(p, i) in queuedByLane[lane.id]"
              :key="p.phase_id"
              class="block queued"
              :class="{ selected: p.phase_id === phaseId }"
              :style="{ right: `${8 + i * 4}px`, width: '136px' }"
              :title="`${p.name} — queued`"
              @click="selectPhase(p)"
            >
              <span class="b-top">
                <span class="b-status queued">○</span>
                <span class="b-name">{{ p.name }}</span>
              </span>
              <span class="b-desc">queued</span>
            </button>
          </div>
        </div>
      </div>
    </div>
    <div v-else-if="loaded" class="empty-state">no phases recorded for this session</div>
    <div v-else-if="!apiError" class="empty-state">loading trace…</div>

    <!-- Horizontal keeps the detail under the chart; vertical would put it a
         very long scroll away from the block you just clicked, so there it
         becomes a docked side panel that stays in view. -->
    <aside v-if="selectedPhase && layoutMode === 'vertical'" class="drawer">
      <PhaseDetail
        :phase="selectedPhase"
        :events="events"
        :envelopes="envelopes"
        :gates="gates"
        @close="navigate(props.adwId)"
      />
    </aside>
    <PhaseDetail
      v-else-if="selectedPhase"
      :phase="selectedPhase"
      :events="events"
      :envelopes="envelopes"
      :gates="gates"
      @close="navigate(props.adwId)"
    />
  </div>
</template>

<style scoped>
.trace {
  padding: 0 0 40px;
}

.run-strip {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 20px;
  border-bottom: 1px solid var(--border-soft);
  flex-wrap: wrap;
  font-size: 12px;
}

.run-strip .request {
  font-size: 13px;
  color: var(--text);
  max-width: 52ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-stats {
  display: inline-flex;
  gap: 8px;
  flex-wrap: wrap;
}

/* The run strip is a header, not a reading surface — its chips shrink with it
   rather than holding the app-wide 16px floor. */
.run-strip :deep(.stat) {
  padding: 1px 9px;
  gap: 5px;
  font-size: 12px;
}

.run-strip :deep(.chip) {
  padding: 1px 10px;
  gap: 5px;
  font-size: 12px;
}

/* lucide sizes itself with width/height attributes; CSS wins over them. */
.run-strip :deep(.stat-icon),
.run-strip :deep(.chip-icon) {
  width: 14px;
  height: 14px;
}

/* Docked detail for the vertical layout: fixed to the right edge so it stays
   put however far down the chart you have scrolled, and scrolls its own body. */
.drawer {
  position: fixed;
  top: var(--topbar-h);
  right: 0;
  bottom: 0;
  width: min(810px, 60vw);
  z-index: 20;
  overflow-y: auto;
  overscroll-behavior: contain;
  background: rgba(9, 13, 23, 0.97);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border-left: 1px solid var(--border);
  box-shadow: -18px 0 40px rgba(0, 0, 0, 0.45);
}

.drawer :deep(.detail) {
  margin: 14px;
}

.chart {
  --label-w: 224px;
  margin: 20px 28px;
}

/* The visible scrollbar, mirrored above the chart. */
.hscroll-top {
  overflow-x: auto;
  overflow-y: hidden;
  /* Explicit, or the box collapses to the 1px spacer and clips its own bar. */
  height: 16px;
  margin-bottom: 7px;
  border-radius: 999px;
  /* An overlay hairline in --border is invisible against the panel — the bar is
     the only affordance saying the timeline continues, so it stays lit. */
  scrollbar-width: auto;
  scrollbar-color: var(--violet) rgba(6, 8, 15, 0.75);
}

.hscroll-spacer {
  /* Exactly the row's max-content width, so the two bars travel in step. */
  width: calc(var(--label-w) + var(--track-min, 1200px));
  height: 1px;
}

.hscroll-top::-webkit-scrollbar {
  height: 14px;
}

.hscroll-top::-webkit-scrollbar-track {
  background: rgba(6, 8, 15, 0.75);
  border-radius: 999px;
}

.hscroll-top::-webkit-scrollbar-thumb {
  background: var(--violet);
  border: 3px solid rgba(6, 8, 15, 0.75);
  border-radius: 999px;
}

.hscroll-top::-webkit-scrollbar-thumb:hover {
  background: var(--purple);
}

.waterfall {
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  overflow-x: auto;
  overflow-y: hidden;
  overscroll-behavior-x: contain;
  /* Scrolls (shift+wheel, drag, keyboard) but shows no bar of its own — the
     strip above is the one handle. */
  scrollbar-width: none;
}

.waterfall::-webkit-scrollbar {
  display: none;
}

/* max-content so the track can exceed the viewport; min-width so a short run
   still fills it. */
.row {
  display: grid;
  grid-template-columns: var(--label-w) minmax(var(--track-min, 1200px), 1fr);
  width: max-content;
  min-width: 100%;
}

.axis-row {
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
}

/* The lane names stay put while the timeline scrolls under them. */
.label {
  position: sticky;
  left: 0;
  z-index: 2;
  background: var(--panel);
}

.axis-row .label {
  z-index: 3;
  background: var(--panel-2);
}

.axis-row .track {
  height: 32px;
  overflow: hidden;
}

.zone-head {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: var(--amber);
  border-right: 1px solid var(--border);
}

.axis-label {
  position: absolute;
  bottom: 6px;
  transform: translateX(-50%);
  font-family: var(--mono);
  font-size: 13px;
  color: var(--dim);
  white-space: nowrap;
}

.label {
  padding: 10px 13px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
  border-right: 1px solid var(--border);
  overflow: hidden;
  white-space: nowrap;
}

.lane-name {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 14px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
}

.lane-icon {
  flex: none;
  opacity: 0.85;
}

.lane-meta {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
}

.lane-model {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.model-icon {
  width: 14px;
  height: 14px;
  flex: none;
  object-fit: contain;
}

/* Context occupancy — label row over a thin track, under the model. */
.lane-ctx {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 2px;
  max-width: 152px;
}

.ctx-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.ctx-label {
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--faint);
}

.ctx-pct {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--dim);
}

.ctx-bar {
  height: 5px;
  border-radius: 999px;
  background: rgba(6, 8, 15, 0.75);
  border: 1px solid var(--border-soft);
  overflow: hidden;
}

.ctx-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 300ms ease;
}

.lane {
  border-bottom: 1px solid var(--border-soft);
}

.lane:last-child {
  border-bottom: none;
}

.track {
  position: relative;
  height: 94px;
  overflow: hidden;
}

.zone-divider {
  position: absolute;
  top: 0;
  bottom: 0;
  border-left: 1px solid var(--border);
}

.gridline {
  position: absolute;
  top: 0;
  bottom: 0;
  border-left: 1px dashed rgba(174, 191, 212, 0.14);
}

.block {
  position: absolute;
  top: 10px;
  height: 74px;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
  gap: 3px;
  padding: 8px 10px 13px;
  border-radius: 8px;
  border: 1px solid;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
  overflow: hidden;
  white-space: nowrap;
  text-align: left;
  transition: box-shadow 0.16s ease;
}

.block:hover {
  box-shadow: 0 0 18px var(--lane-glow, rgba(127, 166, 212, 0.2));
}

.b-top {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.b-status {
  flex: none;
  font-size: 13px;
}

.b-status.success {
  color: var(--green);
}

.b-status.fail {
  color: var(--red);
}

.b-status.running {
  color: var(--blue);
  animation: pulse 1.2s ease-in-out infinite;
}

.b-status.queued {
  color: var(--faint);
}

.block .b-name {
  font-size: 14px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
}

.block .b-dur {
  margin-left: auto;
  flex: none;
}

.block .b-desc {
  color: var(--dim);
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.block.running {
  animation: pulse 1.6s ease-in-out infinite;
}

.block.queued {
  background: transparent;
  border-style: dashed;
  border-color: var(--faint);
  color: var(--dim);
}

.block.selected {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
  box-shadow: 0 0 22px var(--lane-glow, rgba(127, 166, 212, 0.25));
}

.tool-tick {
  position: absolute;
  bottom: 3px;
  width: 3px;
  height: 7px;
  background: currentColor;
  opacity: 0.55;
  border-radius: 1px;
}

.tool-tick.err {
  background: var(--red);
  opacity: 1;
}
</style>
