import type { Tab } from '@/stores/settings'
import { defaultKnowledgeBaseId, defaultWorkspaceId } from '@/app/routes'

export const resolveWorkspaceId = (workspaceId?: string) => workspaceId || defaultWorkspaceId

export const resolveKnowledgeBaseId = (kbId?: string) => kbId || defaultKnowledgeBaseId

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

  if (pathname.includes('/kb/') && pathname.endsWith('/settings')) {
    return 'kb-settings'
  }

  if (pathname.endsWith('/members')) {
    return 'members'
  }

  if (!pathname.includes('/kb/') && pathname.endsWith('/settings')) {
    return 'workspace-settings'
  }

  return 'documents'
}
