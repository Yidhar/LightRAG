import { NavLink, useParams } from 'react-router-dom'
import {
  BookOpenIcon,
  BracesIcon,
  FileStackIcon,
  LayoutDashboardIcon,
  NetworkIcon,
  Settings2Icon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { cn } from '@/lib/utils'
import { appRoutes, defaultKnowledgeBaseId } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleLabelKeys,
  type PermissionAction,
} from '@/lib/permissions'
import AccessBadge from '@/components/navigation/AccessBadge'

/**
 * Workspace-scoped sidebar navigation.
 *
 * IA pivot (2026-04-17): the sidebar no longer surfaces a per-knowledge-base
 * section. Every primary item is scoped to the current workspace and, for
 * surfaces that still technically take a kb id, silently passes
 * ``defaultKnowledgeBaseId`` — the backend treats that as "federate across
 * all knowledge bases in the workspace" (lands with the backend federation
 * PR). Managing individual knowledge bases will move to a dedicated page
 * under the workspace once the route migration PR lands.
 */
const navLinkClassName = ({ isActive }: { isActive: boolean }) =>
  cn(
    'motion-standard group flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium active:scale-[0.99]',
    isActive
      ? 'bg-emerald-500/[0.10] text-emerald-700 shadow-[inset_0_0_0_1px_rgba(16,185,129,0.18)] dark:text-emerald-300'
      : 'text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground'
  )

type NavItem = {
  key: string
  label: string
  to: string
  icon: typeof LayoutDashboardIcon
  requiredPermission: PermissionAction | null
}

export default function AppSidebarNav() {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships } = useAuthStore()

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )
  const roleLabel = t(roleLabelKeys[effectiveRole])

  // Workspace-scoped primary items. Pages that still require a kb id in their
  // URL receive ``defaultKnowledgeBaseId`` here; backend federation makes that
  // equivalent to "all knowledge bases in the current workspace".
  const workspaceItems: NavItem[] = [
    {
      key: 'overview',
      label: t('header.overview'),
      to: appRoutes.kbOverview(currentWorkspaceId, defaultKnowledgeBaseId),
      icon: LayoutDashboardIcon,
      requiredPermission: null,
    },
    {
      key: 'documents',
      label: t('header.documents'),
      to: appRoutes.kbDocuments(currentWorkspaceId, defaultKnowledgeBaseId),
      icon: FileStackIcon,
      requiredPermission: 'kb:view',
    },
    {
      key: 'retrieval',
      label: t('header.retrieval'),
      to: appRoutes.kbRetrieval(currentWorkspaceId, defaultKnowledgeBaseId),
      icon: BookOpenIcon,
      requiredPermission: 'kb:query',
    },
    {
      key: 'graph',
      label: t('header.knowledgeGraph'),
      to: appRoutes.kbGraph(currentWorkspaceId, defaultKnowledgeBaseId),
      icon: NetworkIcon,
      requiredPermission: 'kb:view',
    },
    {
      key: 'api',
      label: t('header.api'),
      to: appRoutes.kbApi(currentWorkspaceId, defaultKnowledgeBaseId),
      icon: BracesIcon,
      requiredPermission: 'kb:query',
    },
  ]

  // Admin nav intentionally kept narrow. Member management and audit
  // log are still route-reachable for power users / multi-tenant deploys
  // (LDAP, shared workspaces, etc.) — they just don't take up sidebar
  // real estate when the product surface is "personal workspace + KBs".
  const adminItems: NavItem[] = [
    {
      key: 'settings',
      label: t('header.workspaceSettings'),
      to: appRoutes.workspaceSettings(currentWorkspaceId),
      icon: Settings2Icon,
      requiredPermission: 'workspace:update',
    },
  ]

  const visibleWorkspaceItems = workspaceItems.filter(
    (item) => !item.requiredPermission || hasPermission(effectiveRole, item.requiredPermission)
  )
  const visibleAdminItems = adminItems.filter(
    (item) => !item.requiredPermission || hasPermission(effectiveRole, item.requiredPermission)
  )

  return (
    <aside className="hidden w-[240px] shrink-0 flex-col border-r border-border/60 bg-sidebar/95 lg:flex">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-3">
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            {t('platformShell.common.workspace')}
          </p>
          <p className="truncate text-sm font-semibold text-sidebar-foreground">
            {currentWorkspaceId}
          </p>
        </div>
        <AccessBadge role={effectiveRole} compact />
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-2 py-3">
        <SidebarSection label={t('platformShell.common.workspace')}>
          {visibleWorkspaceItems.map((item) => (
            <SidebarItem key={item.key} item={item} />
          ))}
        </SidebarSection>

        {visibleAdminItems.length > 0 && (
          <SidebarSection label={t('navigation.sidebar.adminHeading', { defaultValue: '管理' })}>
            {visibleAdminItems.map((item) => (
              <SidebarItem key={item.key} item={item} />
            ))}
          </SidebarSection>
        )}

        {visibleAdminItems.length === 0 && (
          <p className="px-3 text-[11px] leading-5 text-muted-foreground">
            {t('navigation.sidebar.workspaceAdminUnavailable', { role: roleLabel })}
          </p>
        )}
      </nav>
    </aside>
  )
}

function SidebarSection({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <section className="space-y-1.5">
      <div className="px-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </div>
      <div className="space-y-0.5">{children}</div>
    </section>
  )
}

function SidebarItem({ item }: { item: NavItem }) {
  const Icon = item.icon
  return (
    <NavLink to={item.to} className={navLinkClassName}>
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span className="truncate">{item.label}</span>
    </NavLink>
  )
}
