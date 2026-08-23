import { ref } from 'vue'

// Hash routes, by section:
//   #/                              → sessions list
//   #/runs/<adw_id>                 → waterfall
//   #/runs/<adw_id>/<phase_id>      → phase panel open
//   #/dashboard                     → launchers
//   #/issues                       → the factory ledger (issue → PR → status)
//
// The run id used to be the first segment, which left no room for a second
// screen without reserving a word inside the id namespace — a reserved word in
// an id namespace is the kind of thing that breaks silently later, so runs got
// a section of their own instead.
export type Section = 'sessions' | 'runs' | 'dashboard' | 'issues'

export interface Route {
  section: Section
  adwId: string | null
  phaseId: string | null
}

function parse(): Route {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map(decodeURIComponent)

  if (parts[0] === 'dashboard') return { section: 'dashboard', adwId: null, phaseId: null }
  if (parts[0] === 'issues') return { section: 'issues', adwId: null, phaseId: null }
  if (parts[0] === 'runs' && parts[1]) {
    return { section: 'runs', adwId: parts[1], phaseId: parts[2] ?? null }
  }
  // Links minted before the sections existed put the run id first. They still
  // work, and they land on the same run.
  if (parts[0]) return { section: 'runs', adwId: parts[0], phaseId: parts[1] ?? null }
  return { section: 'sessions', adwId: null, phaseId: null }
}

const route = ref<Route>(parse())

window.addEventListener('hashchange', () => {
  route.value = parse()
})

export function useRoute() {
  return route
}

// Display name for the phase crumb — set by the trace view once phases load,
// since the phase_id in the URL is not the display name.
export const phaseCrumb = ref<string | null>(null)

export function hrefFor(adwId?: string | null, phaseId?: string | null): string {
  if (!adwId) return '#/'
  let h = `#/runs/${encodeURIComponent(adwId)}`
  if (phaseId) h += `/${encodeURIComponent(phaseId)}`
  return h
}

export const dashboardHref = '#/dashboard'
export const issuesHref = '#/issues'

export function navigate(adwId?: string | null, phaseId?: string | null): void {
  window.location.hash = hrefFor(adwId, phaseId)
}
