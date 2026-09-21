<script setup lang="ts">
/**
 * Factory activity: what the orchestrator, the supervisor and recovery did.
 *
 * The run screens answer "what happened inside run X". This answers the
 * question that comes before it — which stage the orchestrator called on which
 * item and on which provider, what it refused to call and why, what the decider
 * ruled, and what the supervisor did about any of it. None of that is in
 * sssf.db; it lives in tick.log, recovery-actions/ and the supervisor's two
 * json files, which the server merges for /api/activity.
 *
 * Repeats are collapsed server-side. A blocked item writes the same `skipped:`
 * line once a minute for as long as it stays blocked, so a row here can stand
 * for thousands of identical lines and says so with a ×N badge.
 */
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { ActivityEvent, ActivityKind, ActivityResponse } from '../lib/api'
import { fetchActivity } from '../lib/api'
import { hrefFor } from '../lib/router'
import { fmtAgo, fmtClock } from '../lib/format'
import { modelIcon, modelName } from '../lib/models'

const data = shallowRef<ActivityResponse>({ snapshot: null, events: [], total: 0 })
const apiError = ref<string | null>(null)
const loaded = ref(false)

/** Kinds hidden by default. `skipped` is 99% of the log by volume and almost
 *  never the thing being looked for, so the screen opens without it and says
 *  how many it is holding back. */
const hidden = ref<Set<ActivityKind>>(new Set<ActivityKind>(['skipped']))

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    data.value = await fetchActivity()
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
  // The orchestrator ticks once a minute and the supervisor polls once a
  // minute, so anything faster than this re-reads files that have not changed.
  timer = setInterval(() => void tick(), 10_000)
})

onUnmounted(() => clearInterval(timer))

const KINDS: ActivityKind[] = [
  'launched',
  'decided',
  'recovery',
  'diagnosis',
  'reaped',
  'error',
  'note',
  'skipped',
]

const counts = computed(() => {
  const map = new Map<ActivityKind, number>()
  for (const event of data.value.events) {
    map.set(event.kind, (map.get(event.kind) ?? 0) + 1)
  }
  return map
})

const shown = computed(() => data.value.events.filter((e) => !hidden.value.has(e.kind)))

function toggle(kind: ActivityKind) {
  const next = new Set(hidden.value)
  if (next.has(kind)) next.delete(kind)
  else next.add(kind)
  hidden.value = next
}

/** The label on the left rail. The actor is implied by the kind everywhere
 *  except `note`, so the rail shows the kind and the actor rides in the title. */
function railFor(event: ActivityEvent): string {
  return event.kind
}

function titleFor(event: ActivityEvent): string {
  const parts = [`${event.actor} · ${event.kind}`]
  if (event.repeats > 1) {
    parts.push(`${event.repeats}× — first ${fmtClock(event.firstAt)}, last ${fmtClock(event.at)}`)
  }
  if (event.script) parts.push(event.script)
  if (event.pid) parts.push(`pid ${event.pid}`)
  if (event.log) parts.push(event.log)
  return parts.join('\n')
}
</script>

<template>
  <section class="activity">
    <p v-if="apiError" class="banner error">{{ apiError }}</p>

    <!-- The supervisor's latest poll. One overwritten file, so it is a state
         line rather than a row in the feed. -->
    <div v-if="data.snapshot" class="snapshot">
      <span class="chip" :class="data.snapshot.health === 'healthy' ? 'ok' : 'warn'">
        {{ data.snapshot.health ?? 'unknown' }}
      </span>
      <span
        class="chip"
        :class="data.snapshot.driverAlive ? 'ok' : 'bad'"
        :title="data.snapshot.driverDetail ?? ''"
      >
        driver {{ data.snapshot.driverAlive ? 'alive' : 'dead' }}
      </span>
      <span class="chip" :title="data.snapshot.lastTickAt ?? ''">
        last tick {{ fmtAgo(data.snapshot.lastTickAt) }}
      </span>
      <span class="chip">
        open {{ data.snapshot.openItems.length ? data.snapshot.openItems.map((n) => `#${n}`).join(' ') : 'none' }}
      </span>
      <span v-if="data.snapshot.recoveryPaused" class="chip warn">recovery paused</span>
      <span v-for="s in data.snapshot.fired" :key="`f-${s}`" class="chip bad">{{ s }} fired</span>
      <span v-for="s in data.snapshot.candidates" :key="`c-${s}`" class="chip warn">
        {{ s }} watching
      </span>
      <span v-for="e in data.snapshot.errors" :key="e" class="chip bad">{{ e }}</span>
      <span class="polled">polled {{ fmtAgo(data.snapshot.polledAt) }}</span>
    </div>

    <div class="filters">
      <button
        v-for="kind in KINDS"
        :key="kind"
        class="filter"
        :class="{ off: hidden.has(kind) }"
        @click="toggle(kind)"
      >
        {{ kind }}
        <span class="n">{{ counts.get(kind) ?? 0 }}</span>
      </button>
      <span class="total">{{ shown.length }} of {{ data.total }}</span>
    </div>

    <p v-if="loaded && !shown.length" class="empty">Nothing to show with these filters.</p>

    <ol class="feed">
      <li v-for="(event, i) in shown" :key="`${event.at}-${event.kind}-${event.item}-${i}`">
        <span class="when" :title="event.at ?? ''">{{ fmtClock(event.at) }}</span>
        <span class="rail" :class="event.kind">{{ railFor(event) }}</span>
        <span class="body" :title="titleFor(event)">
          <a v-if="event.item" class="item" :href="`#/issues`">#{{ event.item }}</a>
          <span v-if="event.stage" class="stage">{{ event.stage }}</span>
          <span class="summary">{{ event.summary }}</span>
          <a v-if="event.adwId" class="adw" :href="hrefFor(event.adwId)">{{ event.adwId }}</a>
          <span v-for="m in event.models" :key="m" class="model" :title="m">
            <img v-if="modelIcon(m)" :src="modelIcon(m) ?? ''" alt="" />
            {{ modelName(m) }}
          </span>
          <span v-if="event.repeats > 1" class="repeats">×{{ event.repeats }}</span>
          <span v-if="event.detail" class="detail">{{ event.detail }}</span>
        </span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.activity {
  padding: 16px 20px 40px;
}

.banner.error {
  color: var(--red);
  font-size: 13px;
}

.snapshot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
}

.chip {
  padding: 1px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--dim);
  font-size: 12px;
  white-space: nowrap;
}

.chip.ok {
  color: var(--green);
  border-color: rgba(108, 186, 143, 0.4);
}

.chip.warn {
  color: var(--amber);
  border-color: rgba(202, 164, 89, 0.4);
}

.chip.bad {
  color: var(--red);
  border-color: rgba(217, 123, 115, 0.45);
}

.polled {
  margin-left: auto;
  color: var(--faint);
  font-size: 12px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}

.filter {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
}

.filter.off {
  color: var(--faint);
  opacity: 0.55;
}

.filter .n {
  color: var(--faint);
  font-family: var(--mono);
}

.total {
  margin-left: auto;
  color: var(--faint);
  font-size: 12px;
}

.empty {
  color: var(--faint);
  font-size: 13px;
}

.feed {
  list-style: none;
  margin: 0;
  padding: 0;
}

.feed li {
  display: grid;
  grid-template-columns: 74px 84px 1fr;
  gap: 10px;
  align-items: baseline;
  padding: 5px 8px;
  border-bottom: 1px solid var(--border-soft);
  font-size: 13px;
}

.feed li:hover {
  background: var(--panel-3);
}

.when {
  color: var(--faint);
  font-family: var(--mono);
  font-size: 12px;
}

.rail {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--dim);
  text-align: right;
}

.rail.launched {
  color: var(--green);
}

.rail.decided {
  color: var(--purple);
}

.rail.recovery {
  color: var(--cyan);
}

.rail.diagnosis {
  color: var(--violet);
}

.rail.error,
.rail.reaped {
  color: var(--red);
}

.rail.skipped {
  color: var(--faint);
}

.body {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.item {
  color: var(--amber);
  font-family: var(--mono);
}

.stage {
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--blue);
  font-family: var(--mono);
  font-size: 11px;
}

.summary {
  color: var(--text);
}

.adw {
  color: var(--cyan);
  font-family: var(--mono);
  font-size: 12px;
}

.model {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--dim);
  font-family: var(--mono);
  font-size: 11px;
}

.model img {
  width: 12px;
  height: 12px;
  border-radius: 2px;
}

.repeats {
  color: var(--faint);
  font-family: var(--mono);
  font-size: 11px;
}

.detail {
  flex-basis: 100%;
  color: var(--faint);
  font-size: 12px;
}
</style>
