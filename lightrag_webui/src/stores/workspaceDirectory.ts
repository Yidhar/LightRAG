import { create } from 'zustand'

import { listWorkspaces, type WorkspaceRecord } from '@/api/lightrag'

/**
 * Workspace id → record lookup shared by the sidebar chip, top-bar
 * switcher, and anywhere else the UI wants to render a human-readable
 * workspace name instead of the raw uuid.
 *
 * The backend stores workspaces by opaque id (``7b692d…``) but users
 * think in friendly names. Fetching once on app boot and caching in a
 * Zustand slice gives every component a sync lookup without re-hitting
 * the API on every route change.
 *
 * A single inflight promise deduplicates concurrent callers — the
 * sidebar, top bar, and retrieval page all mount at once on first
 * render but only one GET /workspaces goes out.
 */

export type WorkspaceDirectoryState = {
  workspaces: WorkspaceRecord[]
  byId: Record<string, WorkspaceRecord>
  loading: boolean
  loaded: boolean
  error: string | null
  /** Kick off a fetch if one isn't already in flight / already loaded. */
  ensureLoaded: () => Promise<void>
  /** Force a refresh — used after create / rename / delete mutations. */
  refresh: () => Promise<void>
  /** Lookup helper that degrades gracefully to the id when unknown. */
  nameFor: (workspaceId: string | null | undefined) => string
}

let inflight: Promise<void> | null = null

async function runFetch(
  set: (partial: Partial<WorkspaceDirectoryState>) => void
): Promise<void> {
  set({ loading: true, error: null })
  try {
    const response = await listWorkspaces()
    const byId: Record<string, WorkspaceRecord> = {}
    for (const item of response.items) {
      byId[item.id] = item
    }
    set({
      workspaces: response.items,
      byId,
      loading: false,
      loaded: true,
      error: null,
    })
  } catch (err) {
    set({
      loading: false,
      loaded: true,
      error: err instanceof Error ? err.message : String(err),
    })
  } finally {
    inflight = null
  }
}

export const useWorkspaceDirectoryStore = create<WorkspaceDirectoryState>(
  (set, get) => ({
    workspaces: [],
    byId: {},
    loading: false,
    loaded: false,
    error: null,

    ensureLoaded: async () => {
      if (get().loaded || inflight) {
        await inflight
        return
      }
      inflight = runFetch(set)
      await inflight
    },

    refresh: async () => {
      inflight = runFetch(set)
      await inflight
    },

    nameFor: (workspaceId) => {
      if (!workspaceId) return ''
      const record = get().byId[workspaceId]
      return record?.name || workspaceId
    },
  })
)

// Cross-component notifier: after any workspace CRUD, emit this event
// so the directory refetches. Reuses the ``lightrag:kb-updated`` pattern
// already established in KBTabs for consistency.
if (typeof window !== 'undefined') {
  window.addEventListener('lightrag:workspaces-updated', () => {
    void useWorkspaceDirectoryStore.getState().refresh()
  })
}
