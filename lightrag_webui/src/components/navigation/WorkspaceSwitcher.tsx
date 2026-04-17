import { appRoutes, defaultWorkspaceId } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { cn } from '@/lib/utils'
import { BriefcaseBusinessIcon, ChevronRightIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useAuthStore } from '@/stores/state'

interface WorkspaceSwitcherProps {
  className?: string
}

/**
 * Compact workspace chip for the top bar.
 *
 * The previous card treatment (bg-white / dark:bg-slate-900 + drop shadow)
 * clashed with the surface-glass header. We now render a flat pill that
 * reuses the header's own border / background tokens so it reads as part
 * of the chrome rather than floating above it.
 */
export default function WorkspaceSwitcher({ className }: WorkspaceSwitcherProps) {
  const { t } = useTranslation()
  const { workspaceId } = useParams()
  const { memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const workspaceCount = useMemo(() => {
    return Array.from(
      new Set([defaultWorkspaceId, ...memberships.map((membership) => membership.workspace_id)])
    ).length
  }, [memberships])

  return (
    <Link
      to={appRoutes.workspaces}
      className={cn(
        'motion-standard group flex min-w-0 items-center gap-2 rounded-full border border-border/60 bg-background/40 px-3 py-1.5 text-sm transition-colors hover:border-emerald-500/40 hover:bg-emerald-500/[0.06]',
        className
      )}
    >
      <BriefcaseBusinessIcon
        className="size-4 shrink-0 text-emerald-600 dark:text-emerald-300"
        aria-hidden="true"
      />
      <span className="flex min-w-0 items-baseline gap-1.5">
        <span className="truncate text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
          {t('header.workspace')}
        </span>
        <span className="truncate font-medium text-foreground">{currentWorkspaceId}</span>
        {workspaceCount > 1 && (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            · {workspaceCount}
          </span>
        )}
      </span>
      <ChevronRightIcon
        className="size-3.5 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5"
        aria-hidden="true"
      />
    </Link>
  )
}
