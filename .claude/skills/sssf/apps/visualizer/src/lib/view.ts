/**
 * Which waterfall layout to draw. Module-level so the topbar toggle and the
 * trace agree without prop-drilling, and remembered across reloads — a run you
 * are watching live reloads often.
 */
import { ref, watch } from 'vue'

export type LayoutMode = 'horizontal' | 'vertical'

const KEY = 'sssf.layout'

/** Vertical is the default: it is the only layout where six lanes and their
 *  descriptions fit one screen without zooming out. */
function initial(): LayoutMode {
  try {
    return localStorage.getItem(KEY) === 'horizontal' ? 'horizontal' : 'vertical'
  } catch {
    return 'vertical' // private-mode / blocked storage: not worth failing over
  }
}

export const layoutMode = ref<LayoutMode>(initial())

watch(layoutMode, (mode) => {
  try {
    localStorage.setItem(KEY, mode)
  } catch {
    /* ignore */
  }
})
