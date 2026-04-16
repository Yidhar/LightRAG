export const defaultWorkspaceId = 'default'
export const defaultKnowledgeBaseId = 'default'

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
  kbRoot: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}`,
  kbOverview: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}/overview`,
  kbDocuments: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}/documents`,
  kbRetrieval: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}/retrieval`,
  kbGraph: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}/graph`,
  kbApi: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}/api`,
  kbSettings: (workspaceId: string = defaultWorkspaceId, kbId: string = defaultKnowledgeBaseId) =>
    `/app/workspaces/${workspaceId}/kb/${kbId}/settings`,
}

