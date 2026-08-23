<script setup lang="ts">
/**
 * The factory ledger: one row per issue, with its current stage, factory
 * status, and linked PR(s) — live vs superseded. Successor to the old Archon
 * ledger (07 - Operations/ledger/ in the vault), which had to reconstruct
 * issue↔PR links by regex over branch names and prompt text because its runs
 * never logged the link directly. SSSF logs pr/issue on the run, and
 * adws_factory writes Fixes/Closes #N on the PR body itself, so this reads
 * straight from GitHub + state.json — no heuristics, always current, no
 * regenerate step.
 */
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { LedgerIssue } from '../lib/api'
import { fetchIssues } from '../lib/api'

const issues = shallowRef<LedgerIssue[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    issues.value = await fetchIssues()
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
  // Slower than the session poll — this reflects gh + state.json, which
  // change on the order of minutes, not the 500ms an agent event does.
  timer = setInterval(() => void tick(), 10_000)
})

onUnmounted(() => clearInterval(timer))

const ordered = computed(() => issues.value.toSorted((a, b) => b.number - a.number))
</script>

<template>
  <div class="issues">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="ordered.length" class="list-head">
      <span class="dim">{{ ordered.length }} issues</span>
    </div>

    <div v-if="ordered.length" class="rows">
      <div v-for="issue in ordered" :key="issue.number" class="row">
        <span class="i-num">#{{ issue.number }}</span>
        <span class="i-title" :title="issue.title">{{ issue.title }}</span>
        <span v-if="issue.stage" class="i-stage">{{ issue.stage }}</span>
        <span
          v-if="issue.status"
          class="ref factory-status"
          :class="issue.status"
          >{{ issue.status }}</span
        >
        <span class="i-prs">
          <span
            v-for="pr in issue.prs"
            :key="pr.number"
            class="ref pr"
            :class="{ superseded: !pr.live, draft: pr.isDraft }"
            :title="`${pr.title} — ${pr.state}${pr.isDraft ? ' (draft)' : ''}${pr.live ? '' : ' — superseded'}`"
            >PR #{{ pr.number }}</span
          >
          <span v-if="!issue.prs.length" class="faint">no PR yet</span>
        </span>
      </div>
    </div>
    <div v-else-if="loaded" class="empty-state">no open or in-flight issues</div>
    <div v-else-if="!apiError" class="empty-state">loading issues…</div>
  </div>
</template>

<style scoped>
.issues {
  display: flex;
  flex-direction: column;
}

.list-head {
  padding: 16px 24px 0;
  font-size: 16px;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 24px 28px;
}

.row {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  padding: 10px 18px;
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  background: var(--surface);
  font-size: 16px;
}

.i-num {
  flex: none;
  font-family: var(--mono);
  font-weight: 700;
  color: var(--purple);
}

.i-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.i-stage {
  flex: none;
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  color: var(--cyan);
  font-family: var(--mono);
  font-size: 13px;
}

.i-prs {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}

.ref {
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  font-family: var(--mono);
  font-size: 13px;
  white-space: nowrap;
}

.ref.pr {
  color: var(--green);
  border-color: rgba(108, 186, 143, 0.35);
}

/* A superseded PR is history, not something to act on — de-emphasized rather
   than hidden, since it is still useful context for "what happened here". */
.ref.pr.superseded {
  color: var(--faint);
  border-color: var(--border-soft);
}

.ref.pr.draft {
  font-style: italic;
}

.factory-status {
  text-transform: capitalize;
}

.factory-status.held {
  color: var(--amber);
  border-color: rgba(202, 164, 89, 0.35);
}

.factory-status.escalated {
  color: var(--red);
  border-color: rgba(217, 123, 115, 0.35);
}

.factory-status.merged {
  color: var(--green);
  border-color: rgba(108, 186, 143, 0.35);
}

.factory-status.parked {
  color: var(--dim);
  border-color: var(--border-soft);
}

.factory-status.running {
  color: var(--blue);
  border-color: rgba(127, 166, 212, 0.35);
}
</style>
