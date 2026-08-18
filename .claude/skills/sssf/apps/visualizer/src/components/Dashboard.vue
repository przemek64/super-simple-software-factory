<script setup lang="ts">
/**
 * The dashboard: what can be started, and how. What is HAPPENING belongs to the
 * sessions list and the session view — the only run state shown here is a
 * per-launcher active count, plus launches that never made it into the db at
 * all, which no other screen can report because they have no session row.
 */
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { Launcher, LaunchStatus } from '../lib/types'
import { fetchLaunchers, fetchLaunches } from '../lib/api'
import LauncherRow from './LauncherRow.vue'

const launchers = shallowRef<Launcher[]>([])
const launches = shallowRef<LaunchStatus[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    const [ls, runs] = await Promise.all([fetchLaunchers(), fetchLaunches()])
    launchers.value = ls
    launches.value = runs
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
  // Slower than the trace's 500ms: nothing here changes while you look at it.
  timer = setInterval(() => void tick(), 2000)
})

onUnmounted(() => clearInterval(timer))

/** Launches that died before writing a single row — otherwise an invisible click. */
const stillborn = computed(() => launches.value.filter((l) => l.state === 'failed_to_start'))
</script>

<template>
  <div class="dashboard">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="stillborn.length" class="stillborn">
      <p class="head">{{ stillborn.length }} launch(es) never started</p>
      <details v-for="l in stillborn" :key="l.adw_id">
        <summary>{{ l.label }} · {{ l.adw_id }} · {{ l.started_at }}</summary>
        <code class="cmd">{{ l.command }}</code>
        <pre v-if="l.log_tail">{{ l.log_tail }}</pre>
        <p v-else class="faint">Nothing was written to {{ l.log }}</p>
      </details>
    </div>

    <div v-if="launchers.length" class="rows">
      <LauncherRow
        v-for="l in launchers"
        :key="l.id"
        :launcher="l"
        :launches="launches"
        @launched="tick"
      />
    </div>

    <p v-else-if="loaded" class="empty faint">
      No launchers. Declare them in
      <code>adws/adw_sssf_config/launchers.yaml</code> in the repo this factory reads.
    </p>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.empty {
  color: var(--faint);
  font-size: 13px;
}

.empty code,
.cmd {
  font-family: var(--mono);
}

.stillborn {
  margin-bottom: 16px;
  padding: 12px 16px;
  border: 1px solid var(--red);
  border-radius: 10px;
  background: rgba(217, 123, 115, 0.07);
  font-size: 12px;
}

.stillborn .head {
  margin: 0 0 8px;
  color: var(--red);
  font-weight: 700;
}

.stillborn summary {
  cursor: pointer;
  color: var(--dim);
}

.stillborn pre {
  margin: 8px 0 0;
  padding: 8px;
  overflow-x: auto;
  background: var(--panel-3);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--dim);
}

.faint {
  color: var(--faint);
}
</style>
