<script setup lang="ts">
/**
 * Why a run failed, on the card and at the top of its detail view.
 *
 * Two lines at most, because it sits inside a fixed-height card: the headline,
 * and the hint when the class is one the server knows the repair for. The hint
 * is the reason this component exists — "guard: launchers.yaml modified" tells
 * you what happened, "never edit the checkout while a run is live" tells you
 * whether to relaunch. `detail` is deliberately not rendered here; it is the
 * raw error and belongs in the phase view, which already shows it.
 *
 * `compact` is the card; without it the bar is the full-width banner used at
 * L2, where there is room for the hint to wrap rather than truncate.
 */
import { computed } from 'vue'
import type { FailureKind, FailureReason } from '../lib/types'

const props = defineProps<{ failure: FailureReason; compact?: boolean }>()

/** Short label for the class — the card has no room to spell it out. */
const LABELS: Record<FailureKind, string> = {
  guard_readonly: 'guard',
  guard_allowlist: 'guard',
  guard_canonical: 'guard',
  snapshot_budget: 'budget',
  stale_worktree: 'worktree',
  precommit_hook: 'hook',
  test_gate: 'tests',
  timeout: 'timeout',
  hard_kill: 'killed',
  gate: 'gate',
  abandoned: 'abandoned',
  error: 'error',
  unknown: 'unknown',
}

const label = computed(() => LABELS[props.failure.kind] ?? 'error')

/**
 * "plan 2/6" — where in the run it died. Omitted when it died outside a phase.
 *
 * The "/N" appears only when N is genuinely ahead of the failed phase. A run
 * writes a phase row when it ENTERS the phase, so on a run that died the count
 * is usually just "how far it got" — rendering that as "2/2" would read as
 * "finished the last phase" when it means the opposite.
 */
const where = computed(() => {
  const f = props.failure
  if (!f.phase) return null
  const known = f.phase_seq !== null && f.phase_count > f.phase_seq
  return known ? `${f.phase} ${f.phase_seq}/${f.phase_count}` : f.phase
})

// The headline already names the class for the recognised kinds ("guard: …"),
// so repeating the chip text in the line would stutter. Strip that prefix.
const headline = computed(() => props.failure.headline.replace(/^guard:\s*/, ''))
</script>

<template>
  <div class="fail-bar" :class="{ compact }" :title="failure.detail ?? failure.headline">
    <div class="fail-head">
      <span class="fail-tag">{{ label }}</span>
      <span v-if="where" class="fail-where">{{ where }}</span>
      <span class="fail-text">{{ headline }}</span>
    </div>
    <div v-if="failure.hint" class="fail-hint">{{ failure.hint }}</div>
  </div>
</template>

<style scoped>
.fail-bar {
  display: flex;
  flex-direction: column;
  /* Centered because the card gives the bar a fixed height whether it has a
     hint line or not — a lone headline should sit in the middle, not on top. */
  justify-content: center;
  gap: 3px;
  padding: 8px 10px;
  border: 1px solid rgba(217, 123, 115, 0.4);
  border-left-width: 3px;
  border-radius: 8px;
  background: rgba(217, 123, 115, 0.08);
  font-size: 15px;
  line-height: 1.35;
}

.fail-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.fail-tag {
  flex: none;
  padding: 1px 6px;
  border-radius: 5px;
  background: rgba(217, 123, 115, 0.22);
  color: #ff8f88;
  font-family: var(--mono);
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.fail-where {
  flex: none;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--dim);
}

.fail-text {
  min-width: 0;
  color: #ffb4ae;
}

.fail-hint {
  color: var(--dim);
  font-size: 14px;
}

/* On a card the region is a fixed two lines, so both rows clamp to one line
   each — a wrapping hint would push the timeline out of the card. */
.fail-bar.compact .fail-text,
.fail-bar.compact .fail-hint {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
