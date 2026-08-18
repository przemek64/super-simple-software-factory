/**
 * The lane model, shared by both waterfall layouts.
 *
 * A lane is one actor on the run — the engineer, the code workspace, or one
 * agent — plus the phases it owns. The horizontal layout draws a lane as a row
 * and the vertical layout draws it as a column, but the lane itself is the same
 * thing either way, so it lives here rather than in either component.
 */
import { Bot, SquareTerminal, UserRound } from 'lucide-vue-next'
import type { AgentSession, Phase, PhaseKind } from './types'

export const ENGINEER_COLOR = '#caa459'
export const CODE_COLOR = '#62aab2'

export const KIND_ICONS = { engineer: UserRound, code: SquareTerminal, agent: Bot }

export interface LaneContext {
  used: number
  window: number
  /** 0–100, uncapped by the floor applied to the bar's width. */
  pct: number
}

export interface Lane {
  id: string
  label: string
  /** Model driving this lane's agent — rendered with its provider icon. */
  model: string | null
  /** Context-window occupancy, or null while unknown (running / old db). */
  context: LaneContext | null
  metaLines: string[]
  color: string
  kind: PhaseKind
  phases: Phase[]
}

/** Occupancy for an agent lane. Null unless BOTH numbers are real — a bar
 *  against an unknown ceiling would be decoration, not data. */
export function laneContext(info: AgentSession | undefined): LaneContext | null {
  const used = info?.context_tokens ?? 0
  const window = info?.context_window ?? 0
  if (!used || !window) return null
  return { used, window, pct: Math.min(100, (used / window) * 100) }
}

/** Sub-1% occupancy is common and real; round it away and the bar reads empty. */
export function contextLabel(ctx: LaneContext): string {
  return ctx.pct < 1 ? `${ctx.pct.toFixed(1)}%` : `${Math.round(ctx.pct)}%`
}

/** Keep a non-zero fill visible — the exact numbers ride in the label and title. */
export function contextFill(ctx: LaneContext): string {
  return `${Math.max(ctx.pct, 2)}%`
}
