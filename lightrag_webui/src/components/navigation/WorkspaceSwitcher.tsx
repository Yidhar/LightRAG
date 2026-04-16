import { appRoutes, defaultWorkspaceId } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { cn } from '@/lib/utils'
import { BriefcaseBusinessIcon, ChevronRightIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useAuthStore } from '@/stores/state'
import { resolveEffectiveRole, roleLabelKeys } from '@/lib/permissions'

interface WorkspaceSwitcherProps {
  className?: string
}

export default function WorkspaceSwitcher({ className }: WorkspaceSwitcherProps) {
  const { t } = useTranslation()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const workspaceCount = useMemo(() => {
    return Array.from(
      new Set([defaultWorkspaceId, ...memberships.map((membership) => membership.workspace_id)])
    ).length
  }, [memberships])
  const roleLabel = t(roleLabelKeys[effectiveRole])

  return (
    <Link
      to={appRoutes.workspaces}
      className={cn(
        'motion-standard inline-flex min-w-[12rem] items-center gap-3 rounded-2xl border border-slate-200 bg-white px-3.5 py-2 text-sm shadow-[0_6px_20px_rgba(15,23,42,0.06)] active:scale-95 hover:border-emerald-500/30 hover:bg-emerald-50/70 dark:border-slate-800 dark:bg-slate-900 dark:hover:bg-slate-800',
        className
      )}
    >
      <div className="flex size-9 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-300">
        <BriefcaseBusinessIcon className="size-4" aria-hidden="true" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
          {t('header.workspace')}
        </div>
        <div className="truncate font-medium text-foreground">{currentWorkspaceId}</div>
        <div className="truncate text-[11px] text-muted-foreground">
          {t('navigation.workspaceSwitcher.summary', {
            count: workspaceCount,
            context: workspaceCount === 1 ? 'single' : 'plural',
            role: roleLabel,
          })}
        </div>
      </div>
      <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
    </Link>
  )
}
