export const defaultWorkspaceId = 'default'
export const defaultKnowledgeBaseId = 'default'

/**
 * Route builders. Phase A collapses the former
 * ``/app/workspaces/:wsId/kb/:kbId/*`` tier down to
 * ``/app/workspaces/:wsId/*`` because the KB is no longer a first-class
 * URL-visible concept (see docs/PlatformV2ExecutionPlan.md).
 *
 * Each KB-scoped helper still accepts an optional second positional
 * argument so existing call sites that hand in ``defaultKnowledgeBaseId``
 * keep compiling; the value is ignored. This keeps the patch reviewable
 * and lets the next commit mechanically drop the stale argument at leisure.
 */
export const appRoutes = {
  login: '/login',
  home: '/',
  app: '/app',
  workspaces: '/app/workspaces',
  workspace: (workspaceId: string = defaultWorkspaceId) => `/app/workspaces/${workspaceId}`,
  workspaceMembers: (workspaceId: string = defaultWorkspaceId) =>
    `/app/workspaces/${workspaceId}/members`,
  workspaceSettings: (workspaceId: string = defaultWorkspaceId) =>
    `/app/workspaces/${workspaceId}/settings`,
  kbRoot: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}`,
  kbOverview: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}/overview`,
  kbDocuments: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}/documents`,
  kbRetrieval: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}/retrieval`,
  kbGraph: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}/graph`,
  kbApi: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}/api`,
  kbSettings: (workspaceId: string = defaultWorkspaceId, _legacyKbId?: string) =>
    `/app/workspaces/${workspaceId}/knowledge-bases`,
}
