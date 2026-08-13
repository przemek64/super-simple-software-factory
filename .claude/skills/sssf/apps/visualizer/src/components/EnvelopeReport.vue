<script setup lang="ts">
/**
 * An envelope in plain English.
 *
 * Every agent's answer is already structured — a reviewer says which
 * requirements were met and what blocks approval, an axis says what it found and
 * how badly — but until now the only way to read that was the raw JSON. This
 * renders the fields an output type is known to carry, and falls back to the
 * envelope's common ones for a type it does not know. The JSON stays underneath,
 * collapsed, because it is the record and this is only a reading of it.
 *
 * Nothing here is derived from the db: the shapes are the pydantic models in
 * adws/adw_modules/data_types.py. A field a model does not send is simply absent,
 * so an older run renders as much as it has.
 */
import { computed } from 'vue'
import { Check, X, TriangleAlert } from 'lucide-vue-next'
import type { Envelope } from '../lib/types'

const props = defineProps<{ envelope: Envelope }>()

interface Finding {
  requirement?: string
  met?: boolean
  evidence?: string
}

interface AxisFinding {
  kind?: string
  severity?: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
  title?: string
  location?: string
  evidence?: string
}

interface ScoutFinding {
  file?: string
  note?: string
}

interface Payload {
  status?: string
  summary?: string
  notes_for_next_agent?: string
  artifacts?: string[]
  // ReviewOutput
  approved?: boolean
  findings?: (Finding & AxisFinding & ScoutFinding)[]
  blocking?: string[]
  // AxisOutput
  axis?: string
  report_path?: string
  // BuildOutput / DocumentOutput / ChangesOutput
  changed_files?: string[]
  documented_files?: string[]
  document_path?: string
  commit_message?: string
  stat?: string
  // VerifyOutput
  passed?: boolean
  failures?: string[]
}

const payload = computed<Payload>(() => {
  try {
    return JSON.parse(props.envelope.payload_json ?? '{}') as Payload
  } catch {
    return {}
  }
})

const type = computed(() => props.envelope.output_type ?? '')

/** The one-line verdict, when the type has one. Null means "no verdict to give". */
const verdict = computed<{ ok: boolean; text: string } | null>(() => {
  const p = payload.value
  if (p.approved !== undefined) {
    return {
      ok: p.approved,
      text: p.approved ? 'Approved' : 'Not approved',
    }
  }
  if (p.passed !== undefined) {
    return { ok: p.passed, text: p.passed ? 'Passed' : 'Failed' }
  }
  return null
})

/** Requirement-style findings (ReviewOutput): each one met or not. */
const requirements = computed(() =>
  (payload.value.findings ?? []).filter((f) => f.requirement !== undefined),
)

/** Severity-style findings (AxisOutput), worst first. */
const RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 } as const

const axisFindings = computed(() =>
  (payload.value.findings ?? [])
    .filter((f) => f.severity !== undefined || f.title !== undefined)
    .toSorted((a, b) => (RANK[a.severity ?? 'LOW'] ?? 3) - (RANK[b.severity ?? 'LOW'] ?? 3)),
)

/** Where-things-live findings (ScoutOutput). */
const places = computed(() => (payload.value.findings ?? []).filter((f) => f.file !== undefined))

const files = computed(() => payload.value.changed_files ?? payload.value.documented_files ?? [])
</script>

<template>
  <div class="report">
    <p v-if="verdict" class="verdict" :class="verdict.ok ? 'ok' : 'bad'">
      <component :is="verdict.ok ? Check : X" :size="14" :stroke-width="2.5" />
      {{ verdict.text }}
      <span v-if="payload.axis" class="axis">· {{ payload.axis }} axis</span>
    </p>

    <p v-if="payload.summary" class="summary">{{ payload.summary }}</p>

    <!-- What must change before approval. Listed first: it is the actionable half. -->
    <div v-if="payload.blocking?.length" class="block blocking">
      <h5><TriangleAlert :size="12" :stroke-width="2.5" /> must change before approval</h5>
      <ul>
        <li v-for="(b, i) in payload.blocking" :key="i">{{ b }}</li>
      </ul>
    </div>

    <div v-if="payload.failures?.length" class="block blocking">
      <h5><TriangleAlert :size="12" :stroke-width="2.5" /> failures</h5>
      <ul>
        <li v-for="(f, i) in payload.failures" :key="i" class="mono">{{ f }}</li>
      </ul>
    </div>

    <!-- Reviewer: the ask, in the requester's words, and whether it is there. -->
    <div v-if="requirements.length" class="block">
      <h5>requirements ({{ requirements.filter((f) => f.met).length }}/{{ requirements.length }} met)</h5>
      <ul class="reqs">
        <li v-for="(f, i) in requirements" :key="i" :class="f.met ? 'met' : 'unmet'">
          <component :is="f.met ? Check : X" :size="13" :stroke-width="2.5" />
          <span>
            <span class="req">{{ f.requirement }}</span>
            <span v-if="f.evidence" class="evidence">{{ f.evidence }}</span>
          </span>
        </li>
      </ul>
    </div>

    <!-- Two-axis review: severity-ranked findings. -->
    <div v-if="axisFindings.length" class="block">
      <h5>findings ({{ axisFindings.length }})</h5>
      <ul class="axis-list">
        <li v-for="(f, i) in axisFindings" :key="i">
          <span class="sev" :class="(f.severity ?? 'LOW').toLowerCase()">{{ f.severity ?? 'LOW' }}</span>
          <span>
            <span class="req">{{ f.title }}</span>
            <span v-if="f.location" class="loc mono">{{ f.location }}</span>
            <span v-if="f.kind" class="kind">{{ f.kind }}</span>
            <span v-if="f.evidence" class="evidence">{{ f.evidence }}</span>
          </span>
        </li>
      </ul>
    </div>

    <!-- Scout: where things live. -->
    <div v-if="places.length" class="block">
      <h5>where things are ({{ places.length }})</h5>
      <ul>
        <li v-for="(f, i) in places" :key="i">
          <span class="mono">{{ f.file }}</span>
          <span v-if="f.note" class="evidence">{{ f.note }}</span>
        </li>
      </ul>
    </div>

    <div v-if="files.length" class="block">
      <h5>{{ type === 'DocumentOutput' ? 'documented' : 'changed' }} files ({{ files.length }})</h5>
      <ul class="mono">
        <li v-for="(f, i) in files" :key="i">{{ f }}</li>
      </ul>
    </div>

    <p v-if="payload.stat" class="stat mono">{{ payload.stat }}</p>

    <div v-if="payload.commit_message" class="block">
      <h5>commit message</h5>
      <pre class="quote">{{ payload.commit_message }}</pre>
    </div>

    <p v-if="payload.document_path || payload.report_path" class="path mono">
      {{ payload.document_path || payload.report_path }}
    </p>

    <div v-if="payload.artifacts?.length" class="block">
      <h5>artifacts</h5>
      <ul class="mono">
        <li v-for="(a, i) in payload.artifacts" :key="i">{{ a }}</li>
      </ul>
    </div>

    <div v-if="payload.notes_for_next_agent" class="block">
      <h5>notes for the next agent</h5>
      <p class="summary">{{ payload.notes_for_next_agent }}</p>
    </div>

    <p v-if="!payload.summary && !verdict && !requirements.length && !axisFindings.length" class="faint">
      This envelope carries no prose — the JSON below is all of it.
    </p>
  </div>
</template>

<style scoped>
.report {
  font-size: 12.5px;
  line-height: 1.5;
}

.verdict {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 8px;
  padding: 3px 10px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 12px;
}

.verdict.ok {
  color: var(--green);
  background: rgba(74, 222, 128, 0.1);
}

.verdict.bad {
  color: var(--red);
  background: rgba(255, 111, 103, 0.1);
}

.axis {
  color: var(--faint);
  font-weight: 400;
}

.summary {
  margin: 0 0 10px;
  color: var(--text);
  white-space: pre-wrap;
}

.block {
  margin-top: 12px;
}

.block h5 {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 0 0 5px;
  color: var(--faint);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.blocking h5 {
  color: var(--red);
}

ul {
  margin: 0;
  padding-left: 16px;
  color: var(--dim);
}

li {
  margin-bottom: 3px;
}

.reqs,
.axis-list {
  list-style: none;
  padding-left: 0;
}

.reqs li,
.axis-list li {
  display: flex;
  align-items: baseline;
  gap: 7px;
  margin-bottom: 6px;
}

.reqs li.met > svg {
  color: var(--green);
}

.reqs li.unmet > svg {
  color: var(--red);
}

.req {
  display: block;
  color: var(--text);
}

.evidence,
.loc,
.kind {
  display: block;
  color: var(--faint);
  font-size: 11.5px;
}

.kind {
  display: inline-block;
  margin-right: 6px;
}

.sev {
  flex: none;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.sev.critical,
.sev.high {
  color: var(--red);
  background: rgba(255, 111, 103, 0.14);
}

.sev.medium {
  color: var(--amber);
  background: rgba(232, 182, 74, 0.14);
}

.sev.low {
  color: var(--faint);
  background: rgba(139, 156, 182, 0.12);
}

.mono,
.quote {
  font-family: var(--mono);
  font-size: 11.5px;
}

.quote {
  margin: 0;
  padding: 7px 9px;
  background: var(--panel-3);
  border-left: 2px solid var(--border);
  border-radius: 4px;
  color: var(--dim);
  white-space: pre-wrap;
}

.stat,
.path {
  margin: 8px 0 0;
  color: var(--faint);
}

.faint {
  margin: 0;
  color: var(--faint);
}
</style>
