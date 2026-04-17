import type { Tab } from '@/stores/settings'
import { defaultKnowledgeBaseId, defaultWorkspaceId } from '@/app/routes'

export const resolveWorkspaceId = (workspaceId?: string) => workspaceId || defaultWorkspaceId

export const resolveKnowledgeBaseId = (kbId?: string) => kbId || defaultKnowledgeBaseId

/**
 * Map a pathname to the legacy ``Tab`` enum used by the settings store.
 * Updated in Phase A to recognise the flat ``/app/workspaces/:ws/<leaf>``
 * shape as well as the legacy ``/kb/:kb/<leaf>`` redirect targets.
 */
export const resolveCurrentTabFromPath = (pathname: string): Tab => {
  if (pathname.endsWith('/overview')) {
    return 'overview'
  }

  if (pathname.endsWith('/graph')) {
    return 'knowledge-graph'
  }

  if (pathname.endsWith('/retrieval')) {
    return 'retrieval'
  }

  if (pathname.endsWith('/api')) {
    return 'api'
  }

  if (pathname.endsWith('/knowledge-bases') || pathname.includes('/knowledge-bases/')) {
    return 'kb-settings'
  }

  if (pathname.endsWith('/members')) {
    return 'members'
  }

  if (pathname.endsWith('/settings')) {
    return 'workspace-settings'
  }

  return 'documents'
}
