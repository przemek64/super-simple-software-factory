<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Columns3, LayoutDashboard, ListTree, Rows3 } from 'lucide-vue-next'
import { fetchHealth } from './lib/api'
import { useRoute, hrefFor, dashboardHref, phaseCrumb } from './lib/router'
import { layoutMode } from './lib/view'
import SessionsList from './components/SessionsList.vue'
import SessionTrace from './components/SessionTrace.vue'
import Dashboard from './components/Dashboard.vue'

const route = useRoute()

// Which repo this UI is pointed at. Two factories are usually open at once and
// the pages are otherwise identical, so the folder name is what tells them
// apart. Health carries it; it never changes, so this is fetched once.
const repo = ref('')
onMounted(async () => {
  try {
    repo.value = (await fetchHealth()).repo
    // The browser tab is the other place two open factories look identical.
    if (repo.value) document.title = `${repo.value} — sssf`
  } catch {
    repo.value = '' // the banner already reports an unreachable api
  }
})

function toggleLayout() {
  layoutMode.value = layoutMode.value === 'vertical' ? 'horizontal' : 'vertical'
}
</script>

<template>
  <div class="app">
    <header class="topbar">
      <nav class="crumbs">
        <!-- Inline copy of public/logo.svg (the favicon) so the mark renders
             crisply with no fetch; keep the two in sync. -->
        <svg class="logo" viewBox="0 0 32 32" aria-hidden="true">
          <rect x="4" y="6" width="17" height="5" rx="2.5" fill="#caa459" />
          <rect x="8" y="13.5" width="20" height="5" rx="2.5" fill="#ab8fce" />
          <rect x="4" y="21" width="13" height="5" rx="2.5" fill="#62aab2" />
        </svg>
        <span class="brand">Super Simple Software Factory</span>
        <template v-if="repo">
          <span class="sep">›</span>
          <span class="repo" :title="`this factory reads ${repo}`">{{ repo }}</span>
        </template>
        <span class="sep">›</span>
        <a :href="hrefFor()" :class="{ current: route.section === 'sessions' }">sessions</a>
        <template v-if="route.section === 'runs' && route.adwId">
          <span class="sep">›</span>
          <a :href="hrefFor(route.adwId)" :class="{ current: !route.phaseId }">{{
            route.adwId
          }}</a>
        </template>
        <template v-if="route.section === 'dashboard'">
          <span class="sep">›</span>
          <span class="current">dashboard</span>
        </template>
        <template v-if="route.adwId && route.phaseId">
          <span class="sep">›</span>
          <span class="current">{{ phaseCrumb ?? route.phaseId }}</span>
        </template>
      </nav>
      <span class="topbar-right">
        <!-- The two screens. Sessions stays the front page: this tool is opened
             to watch a run far more often than to start one, and a launch button
             you walk past every time is a launch button eventually pressed by
             accident. -->
        <a
          class="screen-switch"
          :href="route.section === 'dashboard' ? hrefFor() : dashboardHref"
        >
          <component
            :is="route.section === 'dashboard' ? ListTree : LayoutDashboard"
            :size="16"
            :stroke-width="2"
          />
          {{ route.section === 'dashboard' ? 'sessions' : 'dashboard' }}
        </a>
        <button
          v-if="route.section === 'runs' && route.adwId"
          class="layout-toggle"
          :title="`Switch to ${layoutMode === 'vertical' ? 'horizontal' : 'vertical'} layout`"
          @click="toggleLayout"
        >
          <component :is="layoutMode === 'vertical' ? Rows3 : Columns3" :size="16" :stroke-width="2" />
          {{ layoutMode === 'vertical' ? 'horizontal' : 'vertical' }}
        </button>
        <span class="live-hint"><span class="live-dot" /> live</span>
      </span>
    </header>
    <main>
      <Dashboard v-if="route.section === 'dashboard'" />
      <SessionsList v-else-if="!route.adwId" />
      <SessionTrace v-else :key="route.adwId" :adw-id="route.adwId" :phase-id="route.phaseId" />
    </main>
  </div>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--topbar-h);
  padding: 0 20px;
  background: rgba(11, 15, 24, 0.72);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  position: sticky;
  top: 0;
  z-index: 10;
}

/* Gradient hairline instead of a hard border — the brand colors, whispered. */
.topbar::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 1px;
  background: linear-gradient(
    90deg,
    rgba(171, 143, 206, 0.45),
    rgba(98, 170, 178, 0.35) 40%,
    rgba(98, 170, 178, 0.06)
  );
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  min-width: 0;
}

.logo {
  width: 20px;
  height: 20px;
  flex: none;
  filter: drop-shadow(0 0 8px rgba(171, 143, 206, 0.35));
}

.brand {
  background: linear-gradient(90deg, var(--purple), var(--cyan));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  font-weight: 700;
  letter-spacing: 0.05em;
  white-space: nowrap;
}

.repo {
  /* Deliberately louder than the brand beside it: this is the one word that
     differs between two otherwise identical windows. */
  color: var(--amber);
  font-weight: 700;
  font-size: 1.35em;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.sep {
  color: var(--faint);
}

.crumbs a {
  color: var(--dim);
}

.crumbs a:hover {
  color: var(--text);
}

.crumbs .current {
  color: var(--text);
}

.topbar-right {
  display: inline-flex;
  align-items: center;
  gap: 16px;
}

.layout-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--dim);
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
}

.screen-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--dim);
  font-size: 12px;
}

.screen-switch:hover {
  color: var(--text);
  border-color: var(--cyan);
}

.layout-toggle:hover {
  color: var(--text);
  border-color: var(--dim);
}

.live-hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--dim);
  font-size: 12px;
  white-space: nowrap;
}

.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 10px rgba(108, 186, 143, 0.7);
  animation: pulse 1.6s ease-in-out infinite;
}
</style>
