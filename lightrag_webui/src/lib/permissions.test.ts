import { describe, expect, it } from 'bun:test'

import {
  hasPermission,
  normalizeMembershipClaims,
  resolveEffectiveRole,
} from './permissions'

describe('permissions helpers', () => {
  it('normalizes only valid membership claims', () => {
    expect(
      normalizeMembershipClaims([
        { workspace_id: 'default', kb_id: 'alpha', role: 'editor' },
        { workspace_id: 'default', role: 'admin' },
        { workspace_id: '', role: 'viewer' },
        null,
      ])
    ).toEqual([
      { workspace_id: 'default', kb_id: 'alpha', role: 'editor' },
      { workspace_id: 'default', kb_id: null, role: 'admin' },
    ])
  })

  it('prefers KB-scoped membership over workspace-scoped membership', () => {
    const role = resolveEffectiveRole(
      {
        role: 'user',
        memberships: [
          { workspace_id: 'default', role: 'admin', kb_id: null },
          { workspace_id: 'default', kb_id: 'alpha', role: 'viewer' },
        ],
      },
      { workspaceId: 'default', kbId: 'alpha' }
    )

    expect(role).toBe('viewer')
  })

  it('falls back to legacy token role when no memberships exist', () => {
    expect(resolveEffectiveRole({ role: 'user' }, { workspaceId: 'default', kbId: 'default' })).toBe(
      'owner'
    )
    expect(resolveEffectiveRole({ role: 'unknown' }, { workspaceId: 'default', kbId: 'default' })).toBe(
      'viewer'
    )
  })

  it('matches backend-aligned permissions for admin and viewer', () => {
    expect(hasPermission('admin', 'workspace:invite_member')).toBe(true)
    expect(hasPermission('admin', 'kb:manage_permissions')).toBe(false)
    expect(hasPermission('viewer', 'kb:query')).toBe(true)
    expect(hasPermission('viewer', 'kb:upload_document')).toBe(false)
  })

  it('returns no_access when memberships exist but none match the workspace', () => {
    // Isolation guarantee: a user whose only membership is in workspace_A
    // must not be able to read or query workspace_B just by flipping the
    // X-Workspace-Id header. Falling back to "viewer" here (which still
    // has kb:view + kb:query) would break that.
    const role = resolveEffectiveRole(
      {
        role: 'user',
        memberships: [{ workspace_id: 'workspace_A', kb_id: null, role: 'owner' }],
      },
      { workspaceId: 'workspace_B', kbId: 'default' }
    )

    expect(role).toBe('no_access')
    expect(hasPermission(role, 'kb:view')).toBe(false)
    expect(hasPermission(role, 'kb:query')).toBe(false)
    expect(hasPermission(role, 'workspace:view')).toBe(false)
  })
})
