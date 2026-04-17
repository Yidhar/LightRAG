import { create } from 'zustand'

/**
 * Active workspace / KB scope shared across the app.
 *
 * The dependency resolver on the backend (``_read_header``) accepts an
 * ``X-Workspace-Id`` / ``X-KB-Id`` pair for any request path that does
 * not already carry the ids. The axios request interceptor in
 * ``api/lightrag.ts`` reads this store and injects those headers, so
 * pages that only hit root-level endpoints (``/documents/...``,
 * ``/query``) still carry the correct scope.
 *
 * Right now the scope is set from two places:
 *   - ``useSyncActiveScope`` in AppShell mirrors the current URL
 *     ``/:workspaceId`` into ``activeWorkspaceId`` on every route change.
 *   - The KBTabs component on the Documents page writes ``activeKbId``
 *     when the operator picks a specific KB.
 */
interface KBState {
  activeWorkspaceId: string | null
  activeKbId: string | null
  setActiveWorkspace: (workspaceId: string | null) => void
  setActiveKb: (kbId: string | null) => void
  reset: () => void
}

export const useKBStore = create<KBState>((set) => ({
  activeWorkspaceId: null,
  activeKbId: null,
  setActiveWorkspace: (activeWorkspaceId) => set({ activeWorkspaceId }),
  setActiveKb: (activeKbId) => set({ activeKbId }),
  reset: () => set({ activeWorkspaceId: null, activeKbId: null }),
}))
