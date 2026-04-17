export type AccessRole = 'owner' | 'admin' | 'editor' | 'viewer' | 'no_access'

export type PermissionAction =
  | 'workspace:view'
  | 'workspace:update'
  | 'workspace:invite_member'
  | 'workspace:delete'
  | 'kb:view'
  | 'kb:query'
  | 'kb:upload_document'
  | 'kb:delete_document'
  | 'kb:edit_graph'
  | 'kb:manage_settings'
  | 'kb:manage_permissions'
  | 'audit:view_workspace'
  | 'audit:delete_workspace'

export type MembershipClaim = {
  workspace_id: string
  kb_id?: string | null
  role: string
}

export interface AccessContextLike {
  role?: string | null
  memberships?: MembershipClaim[]
}

const ROLE_PERMISSIONS: Record<AccessRole, Set<PermissionAction>> = {
  // Sentinel role used when an authenticated user's memberships do not
  // include the requested workspace/KB. Intentionally empty so every
  // `hasPermission` check fails — mirrors the backend NO_ACCESS_ROLE and
  // keeps one user's workspace invisible to another user's token.
  no_access: new Set<PermissionAction>(),
  owner: new Set<PermissionAction>([
    'workspace:view',
    'workspace:update',
    'workspace:invite_member',
    'workspace:delete',
    'kb:view',
    'kb:query',
    'kb:upload_document',
    'kb:delete_document',
    'kb:edit_graph',
    'kb:manage_settings',
    'kb:manage_permissions',
    'audit:view_workspace',
    'audit:delete_workspace',
  ]),
  admin: new Set<PermissionAction>([
    'workspace:view',
    'workspace:update',
    'workspace:invite_member',
    'kb:view',
    'kb:query',
    'kb:upload_document',
    'kb:delete_document',
    'kb:edit_graph',
    'kb:manage_settings',
    'audit:view_workspace',
  ]),
  editor: new Set<PermissionAction>([
    'workspace:view',
    'kb:view',
    'kb:query',
    'kb:upload_document',
    'kb:delete_document',
    'kb:edit_graph',
  ]),
  viewer: new Set<PermissionAction>([
    'workspace:view',
    'kb:view',
    'kb:query',
  ]),
}

const LEGACY_ROLE_FALLBACK: Record<string, AccessRole> = {
  user: 'owner',
  owner: 'owner',
  admin: 'admin',
  editor: 'editor',
  viewer: 'viewer',
}

export const roleLabelKeys: Record<AccessRole, string> = {
  owner: 'permissions.roles.owner',
  admin: 'permissions.roles.admin',
  editor: 'permissions.roles.editor',
  viewer: 'permissions.roles.viewer',
  no_access: 'permissions.roles.noAccess',
}

export const roleDescriptionKeys: Record<AccessRole, string> = {
  owner: 'permissions.roleDescriptions.owner',
  admin: 'permissions.roleDescriptions.admin',
  editor: 'permissions.roleDescriptions.editor',
  viewer: 'permissions.roleDescriptions.viewer',
  no_access: 'permissions.roleDescriptions.noAccess',
}

export const normalizeMembershipClaims = (memberships: unknown): MembershipClaim[] => {
  if (!Array.isArray(memberships)) {
    return []
  }

  return memberships
    .map((claim) => {
      if (!claim || typeof claim !== 'object') {
        return null
      }

      const candidate = claim as Record<string, unknown>
      const workspaceId = candidate.workspace_id
      const kbId = candidate.kb_id
      const role = candidate.role

      if (typeof workspaceId !== 'string' || !workspaceId.trim()) {
        return null
      }

      if (typeof role !== 'string' || !role.trim()) {
        return null
      }

      return {
        workspace_id: workspaceId,
        kb_id: typeof kbId === 'string' && kbId.trim() ? kbId : null,
        role,
      }
    })
    .filter((claim): claim is MembershipClaim => claim !== null)
}

export const mapLegacyRole = (role?: string | null): AccessRole =>
  LEGACY_ROLE_FALLBACK[(role || '').trim().toLowerCase()] || 'viewer'

export const resolveEffectiveRole = (
  tokenInfo: AccessContextLike | null | undefined,
  {
    workspaceId,
    kbId,
  }: {
    workspaceId?: string | null
    kbId?: string | null
  }
): AccessRole => {
  if (!tokenInfo) {
    return 'viewer'
  }

  const memberships = tokenInfo.memberships || []

  if (memberships.length > 0) {
    const kbScopedMatch = memberships.find(
      (claim) => claim.workspace_id === workspaceId && (claim.kb_id || null) === (kbId || null)
    )
    if (kbScopedMatch) {
      return mapLegacyRole(kbScopedMatch.role)
    }

    const workspaceScopedMatch = memberships.find(
      (claim) => claim.workspace_id === workspaceId && (claim.kb_id || null) === null
    )
    if (workspaceScopedMatch) {
      return mapLegacyRole(workspaceScopedMatch.role)
    }

    // Memberships exist but none target this workspace/KB — return the
    // no_access sentinel so every hasPermission check fails. Falling
    // back to 'viewer' would still grant kb:view + kb:query and break
    // per-user isolation.
    return 'no_access'
  }

  return mapLegacyRole(tokenInfo.role)
}

export const hasPermission = (role: AccessRole, action: PermissionAction): boolean =>
  ROLE_PERMISSIONS[role].has(action)

export const summarizeRoleCapabilityKeys = (role: AccessRole): string[] => {
  switch (role) {
    case 'owner':
      return [
        'permissions.capabilities.manageMembers',
        'permissions.capabilities.adjustWorkspaceSettings',
        'permissions.capabilities.editKbSettings',
        'permissions.capabilities.curateContent',
      ]
    case 'admin':
      return [
        'permissions.capabilities.manageMembers',
        'permissions.capabilities.adjustWorkspaceSettings',
        'permissions.capabilities.editKbSettings',
        'permissions.capabilities.curateContent',
      ]
    case 'editor':
      return [
        'permissions.capabilities.uploadDocuments',
        'permissions.capabilities.runRetrieval',
        'permissions.capabilities.editGraphContent',
      ]
    case 'viewer':
      return [
        'permissions.capabilities.browseDocuments',
        'permissions.capabilities.runRetrieval',
        'permissions.capabilities.viewGraphAndApiDocs',
      ]
    case 'no_access':
    default:
      return ['permissions.capabilities.noAccess']
  }
}
