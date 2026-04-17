import { useEffect } from 'react'
import { useParams } from 'react-router-dom'

import { useKBStore } from '@/stores/kb'

/**
 * Keep the KB store's ``activeWorkspaceId`` in sync with the current
 * ``:workspaceId`` URL parameter. Mounting this hook in AppShell
 * guarantees that every root-level API call carries the correct
 * ``X-Workspace-Id`` header via the axios interceptor.
 *
 * ``activeKbId`` is intentionally NOT managed here — pages that need a
 * specific KB (DocumentsPage, KB-level admin) set that themselves via
 * their own tabs / pickers.
 */
export function useSyncKBScope() {
  const { workspaceId } = useParams()
  const setActiveWorkspace = useKBStore((s) => s.setActiveWorkspace)

  useEffect(() => {
    setActiveWorkspace(workspaceId ?? null)
  }, [workspaceId, setActiveWorkspace])
}
