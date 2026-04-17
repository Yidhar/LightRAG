import { NavLink, useLocation, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { appRoutes } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { hasPermission, resolveEffectiveRole, type PermissionAction } from '@/lib/permissions'

/**
 * Tablet primary nav. Phase A removed the KB URL tier, so the visible
 * items are either the workspace-admin pair (members + settings) when
 * the user is on one of those routes, or the workspace-scoped feature
 * group otherwise.
 */
export default function AppPrimaryNav() {
  const { t } = useTranslation()
  const location = useLocation()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const pathname = location.pathname
  // Workspace settings + members merged into the unified /workspaces
  // management page. The admin-route branch therefore only fires on
  // bare /members paths (kept for LDAP / multi-tenant deploys) — on
  // those routes we just show a single "工作区管理" shortcut.
  const isWorkspaceAdminRoute =
    pathname.endsWith('/members') || pathname.endsWith('/settings')

  const items = isWorkspaceAdminRoute
    ? [
      {
        label: t('header.workspaceManagement', { defaultValue: '工作区管理' }),
        to: appRoutes.workspaces,
        requiredPermission: null,
      },
    ]
    : [
      {
        label: t('header.overview'),
        to: appRoutes.kbOverview(currentWorkspaceId),
        requiredPermission: null,
      },
      {
        label: t('header.documents'),
        to: appRoutes.kbDocuments(currentWorkspaceId),
        requiredPermission: 'kb:view' as PermissionAction,
      },
      {
        label: t('header.retrieval'),
        to: appRoutes.kbRetrieval(currentWorkspaceId),
        requiredPermission: 'kb:query' as PermissionAction,
      },
      {
        label: t('header.knowledgeGraph'),
        to: appRoutes.kbGraph(currentWorkspaceId),
        requiredPermission: 'kb:view' as PermissionAction,
      },
      {
        label: t('header.api'),
        to: appRoutes.kbApi(currentWorkspaceId),
        requiredPermission: 'kb:query' as PermissionAction,
      },
      {
        label: t('header.kbSettings'),
        to: appRoutes.kbSettings(currentWorkspaceId),
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
          // Exact match only — without ``end`` the "工作区管理" link
          // (→ /workspaces) stays active on every /workspaces/<id>/*
          // page because the former is a prefix of the latter.
          end
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
