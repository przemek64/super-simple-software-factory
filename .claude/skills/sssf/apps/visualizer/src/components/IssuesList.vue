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
import type { LedgerIssue, ReviewCounts } from '../lib/api'
import { fetchIssues } from '../lib/api'
import { hrefFor } from '../lib/router'
import { fmtAgo, fmtDuration } from '../lib/format'

function reviewTitle(r: ReviewCounts): string {
  return (
    `${r.accepted} accepted, ${r.rejected} rejected — ` +
    `critical ${r.critical}, high ${r.high}, medium ${r.medium}, low ${r.low}`
  )
}

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

function notLaunchableReason(issue: LedgerIssue): string {
  if (issue.labels.includes('factory-hold')) return 'on hold — factory-hold label'
  if (!issue.labels.includes('approved-for-dev')) return 'not approved for dev — missing approved-for-dev label'
  return 'not launchable'
}
</script>

<template>
  <div class="issues">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="ordered.length" class="list-head">
      <span class="dim">{{ ordered.length }} issues</span>
    </div>

    <div v-if="ordered.length" class="rows">
      <div
        v-for="issue in ordered"
        :key="issue.number"
        class="row"
        :class="{ dead: !issue.launchable }"
        :title="issue.launchable ? undefined : notLaunchableReason(issue)"
      >
        <div class="row-meta">
          <span class="i-num">#{{ issue.number }}</span>
          <span class="i-title" :title="issue.title">{{ issue.title }}</span>
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
          <!-- picked = wall time since the factory first touched this issue;
               worked = sum of every run's own duration, i.e. time a workflow
               was actually spending on it. The two diverge whenever the item
               sat held, waiting on review, or on a human — which is most of
               the gap on anything but a fast merge. -->
          <span v-if="issue.pickedAt" class="i-time" :title="`picked ${issue.pickedAt}`">
            <span class="picked">{{ fmtAgo(issue.pickedAt) }}</span>
            <span v-if="issue.activeMs != null" class="worked">{{ fmtDuration(issue.activeMs) }} worked</span>
          </span>
        </div>
        <!-- Its own line, right under the status/PR/issue column — narrow and
             small on purpose: labels are secondary context, not the thing a
             scan of this list is looking for, and the row has no width left
             to spare next to the title. -->
        <div v-if="issue.labels.length" class="i-labels">
          <span
            v-for="l in issue.labels"
            :key="l"
            class="ref label"
            :class="{
              target: l.startsWith('target: '),
              approved: l === 'approved-for-dev',
              done: l === 'factory-done',
            }"
            >{{ l }}</span
          >
        </div>
        <!-- The stage trail: every run this issue went through, in order, a
             repeat labelled "p2/2", failed runs in red — so a stuck loop
             shows itself at a glance instead of hiding behind one status word. -->
        <div v-if="issue.stages.length" class="i-stages">
          <span v-for="run in issue.stages" :key="run.adw_id" class="stage-item">
            <a
              class="stage-chip"
              :class="run.status"
              :href="hrefFor(run.adw_id)"
              :title="`${run.label} — ${run.status ?? 'unknown'} — ${run.adw_id}`"
              >{{ run.label }}</a
            >
            <!-- p3 (CodeRabbit) only: what it found on this run, split by its
                 own fix/reject call and by severity. CodeRabbit's severity is
                 documented as unreliable (adw_modules/coderabbit.py) — this is
                 a rough signal for triage, not a verdict. -->
            <span v-if="run.review" class="review" :title="reviewTitle(run.review)">
              <span class="rv accept">✓{{ run.review.accepted }}</span>
              <span class="rv reject">✗{{ run.review.rejected }}</span>
              <span v-if="run.review.critical" class="sev critical">{{ run.review.critical }}</span>
              <span v-if="run.review.high" class="sev high">{{ run.review.high }}</span>
              <span v-if="run.review.medium" class="sev medium">{{ run.review.medium }}</span>
              <span v-if="run.review.low" class="sev low">{{ run.review.low }}</span>
            </span>
          </span>
        </div>
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
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  padding: 10px 18px;
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  background: var(--surface);
  font-size: 16px;
}

/* Not launchable right now — missing approved-for-dev, or held. De-emphasized
   the same way a superseded PR is: still readable, but visually the row you
   skip over while scanning for what a tick could actually pick up next. */
.row.dead {
  opacity: 0.5;
  filter: grayscale(0.6);
}

.row.dead:hover {
  opacity: 0.75;
}

.row-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
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

.i-stages {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding-left: 2px;
}

.stage-chip {
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  color: var(--dim);
  font-family: var(--mono);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}

.stage-chip:hover {
  border-color: var(--dim);
}

.stage-chip.success {
  color: var(--green);
  border-color: rgba(108, 186, 143, 0.35);
}

.stage-chip.running {
  color: var(--blue);
  border-color: rgba(127, 166, 212, 0.35);
}

.stage-chip.fail {
  color: var(--red);
  border-color: rgba(217, 123, 115, 0.5);
  background: rgba(217, 123, 115, 0.08);
}

.stage-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.review {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--mono);
  font-size: 12px;
}

.rv {
  white-space: nowrap;
}

.rv.accept {
  color: var(--green);
}

.rv.reject {
  color: var(--faint);
}

/* The four severity tiers, in the exact weight/color spread asked for: a
   critical finding should be unmissable next to a low one. */
.sev {
  padding: 0 4px;
  border-radius: 4px;
}

.sev.critical {
  color: #b0453d;
  font-weight: 700;
  background: rgba(176, 69, 61, 0.14);
}

.sev.high {
  color: var(--red);
}

.sev.medium {
  color: var(--amber);
}

.sev.low {
  color: var(--green);
}

.i-labels {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  flex-wrap: wrap;
}

.i-prs {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}

.i-time {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.3;
  white-space: nowrap;
}

.i-time .picked {
  color: var(--dim);
}

.i-time .worked {
  color: var(--faint);
}

.ref {
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  font-family: var(--mono);
  font-size: 13px;
  white-space: nowrap;
}

/* Narrower than the other chips on purpose — a label is secondary context
   sitting on its own line, and this row has the least width to spare. */
.ref.label {
  padding: 0 5px;
  color: var(--dim);
  font-size: 10px;
  font-stretch: condensed;
  letter-spacing: -0.01em;
}

.ref.label.target {
  color: var(--cyan);
  border-color: rgba(98, 170, 178, 0.35);
}

.ref.label.approved {
  color: #8fd6a8;
  border-color: rgba(143, 214, 168, 0.35);
}

.ref.label.done {
  color: #2f7d4f;
  border-color: rgba(47, 125, 79, 0.5);
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
