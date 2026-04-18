import { useEffect, useRef } from 'react'
import { useParams } from 'react-router-dom'

import { useKBStore } from '@/stores/kb'

/**
 * Keep the KB store's ``activeWorkspaceId`` in sync with the current
 * ``:workspaceId`` URL parameter. Mounting this hook in AppShell
 * guarantees that every root-level API call carries the correct
 * ``X-Workspace-Id`` header via the axios interceptor.
 *
 * Also resets ``activeKbId`` whenever the workspace actually *changes*.
 * Previously we left it alone so pages that set it themselves (KBTabs
 * on DocumentsPage) didn't get stomped — but that meant a KB id like
 * ``"default"`` from the old workspace bled into the new one, and the
 * post-v2 backend rejects that with 404 (KB X not linked to workspace Y)
 * when the new workspace has no such KB linked. On a real switch we
 * drop the kb scope; the page-level picker re-seeds it from the new
 * workspace's first KB.
 */
export function useSyncKBScope() {
  const { workspaceId } = useParams()
  const setActiveWorkspace = useKBStore((s) => s.setActiveWorkspace)
  const setActiveKb = useKBStore((s) => s.setActiveKb)
  const previousWorkspaceRef = useRef<string | null | undefined>(undefined)

  useEffect(() => {
    const next = workspaceId ?? null
    const previous = previousWorkspaceRef.current
    setActiveWorkspace(next)
    if (previous !== undefined && previous !== next) {
      setActiveKb(null)
    }
    previousWorkspaceRef.current = next
  }, [workspaceId, setActiveWorkspace, setActiveKb])
}
