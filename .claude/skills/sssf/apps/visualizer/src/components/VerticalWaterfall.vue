<script setup lang="ts">
/**
 * The waterfall turned on its side: lanes are columns, time runs down.
 *
 * The trade this makes against the horizontal layout: horizontal space is
 * scarce and fixed (six lanes must share one screen width), while vertical
 * space is free — the page just gets taller. So blocks are never squeezed to
 * fit, and a description can finally wrap onto three lines instead of being
 * ellipsed into nothing.
 *
 * There is deliberately no time axis. Blocks are pushed down to clear a
 * readable minimum height, which breaks the exact time→pixel mapping an axis
 * would claim; each block carries its own start clock and duration instead,
 * which is the honest version of the same information.
 */
import { computed } from 'vue'
import type { EventRow, Phase } from '../lib/types'
import { fmtClock, payloadOk, ts } from '../lib/format'
import { modelIcon, modelName } from '../lib/models'
import { hexAlpha } from '../lib/events'
import { KIND_ICONS, contextFill, contextLabel, type Lane } from '../lib/lanes'
import StatChip from './StatChip.vue'

const props = defineProps<{
  lanes: Lane[]
  phases: Phase[]
  events: EventRow[]
  nowMs: number
  phaseId: string | null
}>()

const emit = defineEmits<{ select: [phase: Phase] }>()

const NUM = new Intl.NumberFormat('en-US')

const STATUS_GLYPH: Record<string, string> = {
  success: '✓',
  fail: '✗',
  running: '●',
  queued: '○',
}

// ── Geometry ─────────────────────────────────────────────────────────────────

/** Enough for a name, two or three wrapped description lines, and the ticks. */
const MIN_BLOCK_PX = 92
/** Gap the push-down keeps between consecutive blocks. */
const GUTTER_PX = 10
/** Height the run is scaled to before the minimums push it taller. */
const BASE_HEIGHT_PX = 900

function endOf(p: Phase, start: number): number {
  const end = ts(p.ended_at)
  if (Number.isFinite(end)) return Math.max(end, start)
  return p.status === 'running' ? Math.max(props.nowMs, start) : start
}

const timed = computed(() =>
  props.phases
    .filter((p) => Number.isFinite(ts(p.started_at)))
    .map((p) => {
      const start = ts(p.started_at)
      return { phase: p, start, end: endOf(p, start) }
    })
    .toSorted((a, b) => a.start - b.start),
)

const span = computed(() => {
  const rows = timed.value
  if (!rows.length) return 1000
  const t0 = rows[0]!.start
  const t1 = rows.reduce((acc, r) => Math.max(acc, r.end), t0)
  return Math.max(t1 - t0, 1000)
})

/**
 * Top and height per phase, in px.
 *
 * Same doctrine as the horizontal layout — phases are sequential, so a block
 * widened to the readable minimum pushes every later block down rather than
 * overlapping it. Unlike the horizontal layout there is no final squeeze back
 * into a fixed track: the column simply grows and the page scrolls.
 */
const layout = computed<Record<string, { top: number; height: number }>>(() => {
  const rows = timed.value
  const out: Record<string, { top: number; height: number }> = {}
  if (!rows.length) return out

  const t0 = rows[0]!.start
  const scale = BASE_HEIGHT_PX / span.value

  let shift = 0
  let prevEdge = 0
  for (const r of rows) {
    let top = (r.start - t0) * scale + shift
    if (top < prevEdge) {
      shift += prevEdge - top
      top = prevEdge
    }
    const natural = (r.end - r.start) * scale
    const height = Math.max(natural, MIN_BLOCK_PX)
    shift += height - natural
    prevEdge = top + height + GUTTER_PX
    out[r.phase.phase_id] = { top, height }
  }
  return out
})

const bodyHeight = computed(() => {
  let bottom = 0
  for (const g of Object.values(layout.value)) bottom = Math.max(bottom, g.top + g.height)
  const queued = props.phases.filter((p) => !p.started_at).length
  return bottom + queued * (MIN_BLOCK_PX + GUTTER_PX) + 24
})

function blockStyle(p: Phase, lane: Lane): Record<string, string> | undefined {
  const geom = layout.value[p.phase_id]
  if (!geom) return undefined
  return {
    top: `${geom.top}px`,
    height: `${geom.height}px`,
    background: `linear-gradient(160deg, ${hexAlpha(lane.color, 0.2)}, ${hexAlpha(lane.color, 0.05)})`,
    borderColor: p.status === 'fail' ? 'rgba(217, 123, 115, 0.8)' : hexAlpha(lane.color, 0.55),
    '--lane-glow': hexAlpha(lane.color, 0.28),
  }
}

/** Queued phases have no place on the time line — they stack under it. */
function queuedStyle(lane: Lane, i: number): Record<string, string> {
  let bottom = 0
  for (const p of lane.phases) {
    const geom = layout.value[p.phase_id]
    if (geom) bottom = Math.max(bottom, geom.top + geom.height)
  }
  return {
    top: `${bottom + GUTTER_PX + i * (MIN_BLOCK_PX + GUTTER_PX)}px`,
    height: `${MIN_BLOCK_PX}px`,
  }
}

function durationMs(p: Phase): number {
  const start = ts(p.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = p.status === 'running' ? props.nowMs : ts(p.ended_at)
  if (!Number.isFinite(end)) return NaN
  return end - start
}

// Tool-call marks, laid along the block's bottom edge within its own span.
const toolTicks = computed(() => {
  const map: Record<string, { t: number; ok: boolean }[]> = {}
  for (const e of props.events) {
    if (e.type !== 'tool_call' || !e.phase_id) continue
    map[e.phase_id] ??= []
    map[e.phase_id]?.push({ t: ts(e.started_at), ok: payloadOk(e.payload_json) })
  }
  return map
})

function ticksFor(p: Phase): { x: number; ok: boolean }[] {
  const start = ts(p.started_at)
  if (!Number.isFinite(start)) return []
  const width = Math.max(endOf(p, start) - start, 1)
  return (toolTicks.value[p.phase_id] ?? [])
    .filter((m) => Number.isFinite(m.t))
    .map((m) => ({ x: Math.min(Math.max(((m.t - start) / width) * 100, 1), 99), ok: m.ok }))
}

const queuedByLane = computed(() => {
  const map: Record<string, Phase[]> = {}
  for (const lane of props.lanes) map[lane.id] = lane.phases.filter((p) => !p.started_at)
  return map
})

/**
 * Column widths.
 *
 * The engineer and code lanes hold short, mostly self-explanatory blocks (a
 * request, a handful of commits) and they bracket the run rather than filling
 * it. The agent lanes carry the descriptions worth reading, so the narrow lanes
 * give up ~30% of their share to them.
 */
const NARROW_FR = 0.7

const gridCols = computed(
  () =>
    `var(--gutter-w) ${props.lanes
      .map((l) => `minmax(0, ${l.kind === 'agent' ? 1 : NARROW_FR}fr)`)
      .join(' ')}`,
)

/** The clock gutter labels each block where it actually sits. */
const gutterMarks = computed(() =>
  timed.value
    .map((r) => ({
      id: r.phase.phase_id,
      top: layout.value[r.phase.phase_id]?.top ?? 0,
      label: fmtClock(r.phase.started_at),
    }))
    .filter((m) => m.label),
)
</script>

<template>
  <div class="vwaterfall" :style="{ '--grid-cols': gridCols }">
    <div class="vhead">
      <div class="gutter-head" />
      <div v-for="lane in lanes" :key="lane.id" class="col-head">
        <span class="lane-name" :style="{ color: lane.color }">
          <component :is="KIND_ICONS[lane.kind]" class="lane-icon" :size="16" :stroke-width="2" />
          <span class="lane-label">{{ lane.label }}</span>
        </span>
        <span v-if="lane.model" class="lane-meta lane-model" :title="lane.model">
          <img v-if="modelIcon(lane.model)" class="model-icon" :src="modelIcon(lane.model)!" alt="" />
          {{ modelName(lane.model) }}
        </span>
        <span v-for="(line, i) in lane.metaLines" :key="i" class="lane-meta">{{ line }}</span>
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
                boxShadow: `0 0 8px ${hexAlpha(lane.color, 0.45)}`,
              }"
            />
          </span>
        </span>
      </div>
    </div>

    <div class="vbody" :style="{ height: `${bodyHeight}px` }">
      <div class="gutter">
        <span v-for="m in gutterMarks" :key="m.id" class="gutter-mark" :style="{ top: `${m.top}px` }">
          {{ m.label }}
        </span>
      </div>
      <div v-for="lane in lanes" :key="lane.id" class="col" :class="`kind-${lane.kind}`">
        <button
          v-for="p in lane.phases.filter((x) => layout[x.phase_id])"
          :key="p.phase_id"
          class="vblock"
          :class="[p.status, { selected: p.phase_id === phaseId }]"
          :style="blockStyle(p, lane)"
          :title="`${p.name} — ${p.status}${p.description ? `\n${p.description}` : ''}`"
          @click="emit('select', p)"
        >
          <span class="b-top">
            <span class="b-status" :class="p.status">{{ STATUS_GLYPH[p.status ?? ''] ?? '○' }}</span>
            <span class="b-name">{{ p.name }}</span>
            <StatChip
              v-if="Number.isFinite(durationMs(p))"
              class="b-dur"
              kind="runtime"
              compact
              :value="durationMs(p)"
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

        <button
          v-for="(p, i) in queuedByLane[lane.id]"
          :key="p.phase_id"
          class="vblock queued"
          :class="{ selected: p.phase_id === phaseId }"
          :style="queuedStyle(lane, i)"
          :title="`${p.name} — queued`"
          @click="emit('select', p)"
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
</template>

<style scoped>
.vwaterfall {
  --gutter-w: 58px;
  margin: 14px 28px;
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  /* NOT overflow:hidden. That makes this element a scroll container, which
     becomes the sticky header's scrollport — and since it never scrolls, the
     header renders permanently pushed down by its own `top` offset (the empty
     band above the columns) while overlapping the first rows beneath it (the
     first blocks unreachable however far you scroll back up). The page must be
     the scrollport. Nothing overflows here anyway: blocks are inset from the
     column edges, so no clipping is needed. */
}

.vhead,
.vbody {
  display: grid;
  grid-template-columns: var(--grid-cols);
}

.vhead {
  position: sticky;
  top: var(--topbar-h);
  z-index: 4;
  background: var(--panel-2);
  border-bottom: 1px solid var(--border);
  border-radius: 15px 15px 0 0;
}

.gutter-head {
  border-right: 1px solid var(--border);
}

.col-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 11px;
  border-right: 1px solid var(--border);
  min-width: 0;
}

.col-head:last-child {
  border-right: none;
}

.lane-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 700;
  min-width: 0;
}

.lane-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lane-icon {
  flex: none;
  opacity: 0.85;
}

.lane-meta {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lane-model {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.model-icon {
  width: 13px;
  height: 13px;
  flex: none;
  object-fit: contain;
}

.lane-ctx {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-top: 1px;
}

.ctx-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
}

.ctx-label {
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--faint);
}

.ctx-pct {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--dim);
}

.ctx-bar {
  height: 4px;
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

.vbody {
  position: relative;
  padding-top: 12px;
}

.gutter {
  position: relative;
  border-right: 1px solid var(--border);
}

.gutter-mark {
  position: absolute;
  right: 8px;
  transform: translateY(-2px);
  font-family: var(--mono);
  font-size: 11px;
  color: var(--faint);
  white-space: nowrap;
}

.col {
  position: relative;
  border-right: 1px dashed rgba(174, 191, 212, 0.14);
}

.col:last-child {
  border-right: none;
}

.vblock {
  position: absolute;
  left: 7px;
  right: 7px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px 14px;
  border-radius: 8px;
  border: 1px solid;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
  overflow: hidden;
  text-align: left;
  transition: box-shadow 0.16s ease;
}

.vblock:hover {
  box-shadow: 0 0 18px var(--lane-glow, rgba(127, 166, 212, 0.2));
}

.b-top {
  display: flex;
  align-items: baseline;
  gap: 7px;
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

.b-name {
  font-size: 14px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.b-dur {
  margin-left: auto;
  flex: none;
}

/* The whole point of the vertical layout: the description gets to wrap. One
   size below the block name — it is supporting text, and smaller means more of
   it survives inside the block. */
.b-desc {
  color: var(--dim);
  font-size: 11px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 5;
  line-clamp: 5;
  overflow: hidden;
}

.vblock.running {
  animation: pulse 1.6s ease-in-out infinite;
}

.vblock.queued {
  background: transparent;
  border-style: dashed;
  border-color: var(--faint);
  color: var(--dim);
}

.vblock.selected {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
  box-shadow: 0 0 22px var(--lane-glow, rgba(127, 166, 212, 0.25));
}

.tool-tick {
  position: absolute;
  bottom: 4px;
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
