<script setup lang="ts">
/**
 * The run's phases as one tight strip of colored bars — the row's answer to
 * PhaseDots, which the cards keep.
 *
 * Bars rather than glyphs because a row has no room for a status chip: the
 * strip has to carry both "how far did it get" and "did it end well", so it
 * reads as progress at a glance and the frame carries the verdict.
 */
import { computed } from 'vue'
import type { Phase } from '../lib/types'

const props = defineProps<{ phases: Phase[] }>()

const ordered = computed(() => props.phases.toSorted((a, b) => (a.seq ?? 0) - (b.seq ?? 0)))
</script>

<template>
  <span class="bars">
    <span
      v-for="p in ordered"
      :key="p.phase_id"
      class="b"
      :class="p.status"
      :title="`${p.seq ?? '?'} ${p.name} — ${p.status}`"
    />
    <span v-if="!ordered.length" class="faint">—</span>
  </span>
</template>

<style scoped>
.bars {
  display: inline-flex;
  /* Tight, not touching: 2px is enough to count the phases without the strip
     reading as a gapped row of chips. */
  gap: 2px;
  align-items: center;
  flex: none;
}

.b {
  width: 13px;
  height: 7px;
  border-radius: 2px;
  background: var(--faint);
}

.b.success {
  background: var(--green);
}

.b.fail {
  background: var(--red);
}

.b.running {
  background: var(--blue);
  animation: pulse 1.2s ease-in-out infinite;
}

/* Queued phases are the ones not reached yet — outlined, so the strip shows
   the shape of the whole workflow instead of stopping where the run did. */
.b.queued {
  background: transparent;
  box-shadow: inset 0 0 0 1px var(--border);
}
</style>
