import { NavLink, useParams } from 'react-router-dom'
import {
  BookOpenIcon,
  BracesIcon,
  FileStackIcon,
  FolderKanbanIcon,
  LayoutDashboardIcon,
  NetworkIcon,
  Settings2Icon,
  UsersIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { cn } from '@/lib/utils'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleLabelKeys,
  type PermissionAction,
} from '@/lib/permissions'
import AccessBadge from '@/components/navigation/AccessBadge'
import Badge from '@/components/ui/Badge'

const navLinkClassName = ({ isActive }: { isActive: boolean }) =>
  cn(
    'motion-standard group relative flex flex-col gap-1.5 rounded-[22px] border px-3 py-2.5 text-left active:scale-[0.99]',
    isActive
      ? 'border-emerald-500/25 bg-emerald-500/[0.08] text-emerald-700 shadow-[inset_0_0_0_1px_rgba(16,185,129,0.12)] dark:text-emerald-300'
      : 'border-transparent bg-background/70 text-sidebar-foreground/85 hover:border-emerald-500/15 hover:bg-sidebar-accent/55 hover:text-sidebar-accent-foreground'
  )

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

  const knowledgeBaseItems = [
    {
      label: t('header.overview'),
      description: t('platformShell.kbOverview.fallbackDescription'),
      to: appRoutes.kbOverview(currentWorkspaceId, currentKnowledgeBaseId),
      icon: LayoutDashboardIcon,
      requiredPermission: null,
    },
    {
      label: t('header.documents'),
      description: t('platformShell.documents.description'),
      to: appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId),
      icon: FileStackIcon,
      requiredPermission: 'kb:view' as PermissionAction,
    },
    {
      label: t('header.retrieval'),
      description: t('platformShell.retrieval.description'),
      to: appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId),
      icon: BookOpenIcon,
      requiredPermission: 'kb:query' as PermissionAction,
    },
    {
      label: t('header.knowledgeGraph'),
      description: t('platformShell.graph.description'),
      to: appRoutes.kbGraph(currentWorkspaceId, currentKnowledgeBaseId),
      icon: NetworkIcon,
      requiredPermission: 'kb:view' as PermissionAction,
    },
    {
      label: t('header.api'),
      description: t('platformShell.api.description'),
      to: appRoutes.kbApi(currentWorkspaceId, currentKnowledgeBaseId),
      icon: BracesIcon,
      requiredPermission: 'kb:query' as PermissionAction,
    },
    {
      label: t('header.kbSettings'),
      description: t('platformShell.kbSettings.description'),
      to: appRoutes.kbSettings(currentWorkspaceId, currentKnowledgeBaseId),
      icon: Settings2Icon,
      requiredPermission: 'kb:manage_settings' as PermissionAction,
    },
  ].filter((item) => !item.requiredPermission || hasPermission(effectiveRole, item.requiredPermission))

  const workspaceItems = [
    {
      label: t('header.members'),
      description: t('platformShell.workspaceMembers.description'),
      to: appRoutes.workspaceMembers(currentWorkspaceId),
      icon: UsersIcon,
      requiredPermission: 'workspace:invite_member' as PermissionAction,
    },
    {
      label: t('header.workspaceSettings'),
      description: t('platformShell.workspaceSettings.description'),
      to: appRoutes.workspaceSettings(currentWorkspaceId),
      icon: FolderKanbanIcon,
      requiredPermission: 'workspace:update' as PermissionAction,
    },
  ].filter((item) => hasPermission(effectiveRole, item.requiredPermission))

  return (
    <aside className="hidden w-[272px] shrink-0 border-r border-border/60 bg-sidebar/88 lg:flex lg:flex-col">
      <div className="border-b border-border/60 px-4 py-4">
        <div className="surface-panel rounded-[28px] border border-border/70 bg-background/80 px-4 py-4 shadow-sm">
          <div className="space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="space-y-1">
                <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('navigation.sidebar.scopedWorkspaceShell')}
                </p>
                <p className="text-base font-semibold text-sidebar-foreground">{currentWorkspaceId}</p>
                <p className="text-sm text-muted-foreground">{currentKnowledgeBaseId}</p>
              </div>
              <AccessBadge role={effectiveRole} compact />
            </div>

            <div className="flex flex-wrap gap-2">
              <Badge variant="outline" className="rounded-full bg-muted/20 px-3 py-1 text-[11px]">
                {t('platformShell.common.workspace')}: {currentWorkspaceId}
              </Badge>
              <Badge variant="outline" className="rounded-full bg-muted/20 px-3 py-1 text-[11px]">
                {t('platformShell.common.knowledgeBase')}: {currentKnowledgeBaseId}
              </Badge>
            </div>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="space-y-6">
          <section className="space-y-3">
            <div className="flex items-center justify-between gap-2 px-1">
              <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                {t('header.knowledgeBase')}
              </div>
              <Badge variant="outline" className="rounded-full bg-background/70 px-3 py-1 text-[11px]">
                {knowledgeBaseItems.length}
              </Badge>
            </div>
            <nav className="space-y-2">
              {knowledgeBaseItems.map((item) => {
                const Icon = item.icon
                return (
                  <NavLink key={item.to} to={item.to} className={navLinkClassName}>
                    {({ isActive }) => (
                      <>
                        <div className="flex items-center gap-3">
                          <div
                            className={cn(
                              'flex size-10 shrink-0 items-center justify-center rounded-2xl border',
                              isActive
                                ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
                                : 'border-border/70 bg-muted/20 text-muted-foreground'
                            )}
                          >
                            <Icon className="size-4" aria-hidden="true" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-semibold">{item.label}</p>
                            <p className="line-clamp-1 text-xs leading-5 text-muted-foreground">
                              {item.description}
                            </p>
                          </div>
                        </div>
                      </>
                    )}
                  </NavLink>
                )
              })}
            </nav>
          </section>

          <section className="space-y-3">
            <div className="flex items-center justify-between gap-2 px-1">
              <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                {t('header.workspace')}
              </div>
              <Badge variant="outline" className="rounded-full bg-background/70 px-3 py-1 text-[11px]">
                {workspaceItems.length}
              </Badge>
            </div>
            <nav className="space-y-2">
              {workspaceItems.map((item) => {
                const Icon = item.icon
                return (
                  <NavLink key={item.to} to={item.to} className={navLinkClassName}>
                    {({ isActive }) => (
                      <div className="flex items-center gap-3">
                        <div
                          className={cn(
                            'flex size-10 shrink-0 items-center justify-center rounded-2xl border',
                            isActive
                              ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
                              : 'border-border/70 bg-muted/20 text-muted-foreground'
                          )}
                        >
                          <Icon className="size-4 shrink-0" aria-hidden="true" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-semibold">{item.label}</p>
                          <p className="line-clamp-1 text-xs leading-5 text-muted-foreground">
                            {item.description}
                          </p>
                        </div>
                      </div>
                    )}
                  </NavLink>
                )
              })}
            </nav>

            {workspaceItems.length === 0 && (
              <div className="rounded-[24px] border border-dashed border-border/70 px-4 py-4 text-xs leading-5 text-muted-foreground">
                {t('navigation.sidebar.workspaceAdminUnavailable', { role: roleLabel })}
              </div>
            )}
          </section>
        </div>
      </div>
    </aside>
  )
}
