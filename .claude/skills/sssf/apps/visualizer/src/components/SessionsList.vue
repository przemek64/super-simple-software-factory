<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { SessionSummary } from '../lib/types'
import { fetchSessions } from '../lib/api'
import { ts } from '../lib/format'
import SessionCard from './SessionCard.vue'
import SessionRow from './SessionRow.vue'

// Two shapes for the same list: tiles for browsing, rows for scanning many runs.
// Remembered per browser — the choice is a working preference, not a route, so
// it should survive a reload without turning into a shareable url.
const VIEW_KEY = 'sssf.sessions.view'
type ViewMode = 'rows' | 'cards'
const view = ref<ViewMode>(localStorage.getItem(VIEW_KEY) === 'cards' ? 'cards' : 'rows')

function setView(mode: ViewMode) {
  view.value = mode
  localStorage.setItem(VIEW_KEY, mode)
}

const sessions = shallowRef<SessionSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    sessions.value = await fetchSessions()
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

onUnmounted(() => clearInterval(timer))

/** Optimistic removal; an empty id means the write failed, so re-sync instead. */
function onArchived(adwId: string) {
  if (!adwId) {
    void tick()
    return
  }
  sessions.value = sessions.value.filter((s) => s.adw_id !== adwId)
}

const ordered = computed(() =>
  sessions.value.toSorted((a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0)),
)
</script>

<template>
  <div class="sessions">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="ordered.length" class="list-head">
      <span class="dim">{{ ordered.length }} runs</span>
      <span class="view-switch">
        <button
          type="button"
          :class="{ on: view === 'rows' }"
          title="Compact rows — every run on two lines"
          @click="setView('rows')"
        >
          rows
        </button>
        <button
          type="button"
          :class="{ on: view === 'cards' }"
          title="Tiles — one card per run"
          @click="setView('cards')"
        >
          tiles
        </button>
      </span>
    </div>

    <div v-if="ordered.length && view === 'rows'" class="rows">
      <SessionRow
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>

    <div v-else-if="ordered.length" class="cards">
      <SessionCard
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>
    <div v-else-if="loaded" class="empty-state">no sessions yet — run an ADW to see it here</div>
    <div v-else-if="!apiError" class="empty-state">loading sessions…</div>
  </div>
</template>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 24px 0;
  font-size: 16px;
}

.view-switch {
  display: inline-flex;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  overflow: hidden;
}

.view-switch button {
  padding: 3px 14px;
  border: 0;
  background: transparent;
  color: var(--dim);
  font-family: inherit;
  font-size: 15px;
  cursor: pointer;
}

.view-switch button.on {
  background: rgba(135, 144, 194, 0.16);
  color: var(--text);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 24px 28px;
}

.cards {
  /* Uniform grid: every card the same width and (fixed in SessionCard) height,
     independent of content. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 18px;
  padding: 16px 24px 28px;
}





</style>
