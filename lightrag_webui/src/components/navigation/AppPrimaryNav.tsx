import { NavLink, useLocation, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { hasPermission, resolveEffectiveRole, type PermissionAction } from '@/lib/permissions'

export default function AppPrimaryNav() {
  const { t } = useTranslation()
  const location = useLocation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships } = useAuthStore()

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )
  const isWorkspaceScopedRoute =
    location.pathname.endsWith('/members') ||
    (!location.pathname.includes('/kb/') && location.pathname.endsWith('/settings'))

  const items = isWorkspaceScopedRoute
    ? [
      {
        label: t('header.members'),
        to: appRoutes.workspaceMembers(currentWorkspaceId),
        requiredPermission: 'workspace:invite_member' as PermissionAction,
      },
      {
        label: t('header.workspaceSettings'),
        to: appRoutes.workspaceSettings(currentWorkspaceId),
        requiredPermission: 'workspace:update' as PermissionAction,
      },
    ]
    : [
      {
        label: t('header.overview'),
        to: appRoutes.kbOverview(currentWorkspaceId, currentKnowledgeBaseId),
        requiredPermission: null,
      },
      {
        label: t('header.documents'),
        to: appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId),
        requiredPermission: 'kb:view' as PermissionAction,
      },
      {
        label: t('header.retrieval'),
        to: appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId),
        requiredPermission: 'kb:query' as PermissionAction,
      },
      {
        label: t('header.knowledgeGraph'),
        to: appRoutes.kbGraph(currentWorkspaceId, currentKnowledgeBaseId),
        requiredPermission: 'kb:view' as PermissionAction,
      },
      {
        label: t('header.api'),
        to: appRoutes.kbApi(currentWorkspaceId, currentKnowledgeBaseId),
        requiredPermission: 'kb:query' as PermissionAction,
      },
      {
        label: t('header.kbSettings'),
        to: appRoutes.kbSettings(currentWorkspaceId, currentKnowledgeBaseId),
        requiredPermission: 'kb:manage_settings' as PermissionAction,
      },
    ]
  const visibleItems = items.filter(
    (item) => !item.requiredPermission || hasPermission(effectiveRole, item.requiredPermission)
  )

  return (
    <nav className="flex flex-wrap items-center justify-center gap-2">
      {visibleItems.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          className={({ isActive }) =>
            cn(
              'motion-standard inline-flex h-9 items-center rounded-full px-3 text-sm font-medium active:scale-[0.98]',
              isActive
                ? 'bg-emerald-500/14 text-emerald-700 shadow-[inset_0_0_0_1px_rgba(16,185,129,0.16)] dark:text-emerald-300'
                : 'text-muted-foreground hover:bg-muted/80 hover:text-foreground'
            )
          }
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  )
}
