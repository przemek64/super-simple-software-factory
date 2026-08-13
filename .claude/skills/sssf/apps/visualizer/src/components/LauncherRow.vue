<script setup lang="ts">
/**
 * One launcher: its inputs and Run button on the left, its workflow diagram on
 * the right.
 *
 * The diagram is a static picture of the workflow and never shows run state —
 * where a run has got to is the session view's job. The only run state here is
 * the active count, which exists so a launch button is not pressed twice for
 * want of feedback.
 */
import { computed, ref } from 'vue'
import { Play, Loader } from 'lucide-vue-next'
import type { Launcher, LaunchStatus } from '../lib/types'
import { diagramUrl, launchRun } from '../lib/api'
import { navigate } from '../lib/router'

const props = defineProps<{ launcher: Launcher; launches: LaunchStatus[] }>()
const emit = defineEmits<{ launched: [] }>()

const values = ref<Record<string, string>>({})
const busy = ref(false)
const error = ref<string | null>(null)
const noDiagram = ref(false)

const active = computed(
  () =>
    props.launches.filter(
      (l) => l.launcher_id === props.launcher.id && (l.state === 'running' || l.state === 'starting'),
    ).length,
)

async function run() {
  if (busy.value) return
  busy.value = true
  error.value = null
  try {
    const record = await launchRun(props.launcher.id, values.value)
    emit('launched')
    // Straight to the trace: the run is the thing you wanted, not this form.
    navigate(record.adw_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="row">
    <div class="form">
      <header>
        <h2>{{ launcher.label }}</h2>
        <a v-if="active" class="active" href="#/" :title="`${active} active`"
          >{{ active }} active</a
        >
      </header>
      <p v-if="launcher.description" class="dim desc">{{ launcher.description }}</p>

      <div class="fields">
        <label v-for="p in launcher.params" :key="p.name">
          <span class="label">{{ p.label }}</span>
          <input
            v-model="values[p.name]"
            :type="p.type === 'int' ? 'number' : 'text'"
            :placeholder="p.hint ?? p.label"
            :aria-label="p.label"
            @keyup.enter="run"
          />
        </label>

        <button class="run" :disabled="busy" @click="run">
          <component :is="busy ? Loader : Play" :size="15" :stroke-width="2" :class="{ spin: busy }" />
          {{ busy ? 'starting' : 'Run' }}
        </button>
      </div>

      <p v-if="error" class="error">{{ error }}</p>
      <code class="workflow dim">{{ launcher.workflow }}</code>
    </div>

    <div class="diagram">
      <img
        v-if="!noDiagram"
        :src="diagramUrl(launcher.id)"
        :alt="`${launcher.label} workflow`"
        @error="noDiagram = true"
      />
      <p v-else class="faint">
        No diagram yet — drop an SVG at
        <code>{{ launcher.diagram ?? 'adws/adw_sssf_config/diagrams/…' }}</code>
      </p>
    </div>
  </section>
</template>

<style scoped>
.row {
  display: grid;
  grid-template-columns: minmax(260px, 340px) 1fr;
  gap: 24px;
  align-items: start;
  padding: 18px 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
}

header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

h2 {
  margin: 0;
  font-size: 15px;
  color: var(--text);
  font-weight: 700;
}

.active {
  color: var(--amber);
  font-size: 12px;
  white-space: nowrap;
}

.desc {
  margin: 6px 0 0;
  font-size: 12px;
}

.fields {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 10px;
  margin-top: 14px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 130px;
}

.label {
  font-size: 11px;
  color: var(--faint);
  letter-spacing: 0.04em;
}

input {
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--panel-3);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  width: 100%;
}

input:focus {
  outline: none;
  border-color: var(--cyan);
}

.run {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border: 1px solid var(--cyan);
  border-radius: 7px;
  background: rgba(90, 210, 221, 0.12);
  color: var(--text);
  font-family: var(--sans);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}

.run:hover:not(:disabled) {
  background: rgba(90, 210, 221, 0.22);
}

.run:disabled {
  opacity: 0.6;
  cursor: default;
}

.spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.error {
  margin: 10px 0 0;
  color: var(--red);
  font-size: 12px;
}

.workflow {
  display: block;
  margin-top: 12px;
  font-family: var(--mono);
  font-size: 11px;
}

.diagram {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 120px;
  padding: 10px;
  background: var(--panel-3);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
}

.diagram img {
  max-width: 100%;
  height: auto;
}

.diagram .faint {
  color: var(--faint);
  font-size: 12px;
  text-align: center;
}

.diagram code {
  font-family: var(--mono);
}
</style>
