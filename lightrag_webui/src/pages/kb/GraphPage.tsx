import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import { NetworkIcon, ShieldCheckIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleDescriptionKeys,
  summarizeRoleCapabilityKeys,
} from '@/lib/permissions'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'

const GraphViewer = lazy(() => import('@/features/GraphViewer'))

function GraphSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center px-6 py-10">
      <div className="flex items-center gap-3 text-sm text-muted-foreground">
        <span className="size-2 animate-pulse rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
        {t('platformShell.graph.loadingLabel')}
      </div>
    </div>
  )
}

/**
 * Knowledge graph page.
 *
 * PR-UI-2 slice — drop the hero / metadata / access-panel / viewer-card
 * stack (ui-audit §4.10) and let GraphViewer own the viewport. The
 * access ribbon keeps the capability summary visible; the canvas fills
 * flex-1 instead of a fixed 780px box.
 */
export default function GraphPage() {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )

  const canViewKnowledgeBase = hasPermission(effectiveRole, 'kb:view')
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.graph.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.graph.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.graph.description')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openDocuments')}
            </Link>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openRetrieval')}
            </Link>
          </Button>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.graph.accessAndRole')}
          </span>
          <span className="text-muted-foreground">·</span>
          <span className="truncate text-muted-foreground">
            {t(roleDescriptionKeys[effectiveRole])}
          </span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {capabilitySummary.map((capabilityKey) => (
            <Badge
              key={capabilityKey}
              variant="outline"
              className="rounded-full bg-background/70 px-2.5 py-0.5 text-[11px]"
            >
              {t(capabilityKey)}
            </Badge>
          ))}
        </div>
      </div>

      {!canViewKnowledgeBase && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.graph.unavailableTitle')}</AlertTitle>
          <AlertDescription>{t('platformShell.graph.unavailableDescription')}</AlertDescription>
        </Alert>
      )}

      {canViewKnowledgeBase ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense fallback={<GraphSurfaceLoading />}>
            <GraphViewer />
          </Suspense>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 py-10">
          <div className="flex max-w-md flex-col items-center gap-4 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted/40">
              <NetworkIcon className="size-5 text-muted-foreground" aria-hidden="true" />
            </div>
            <p className="text-sm text-muted-foreground">
              {t('platformShell.graph.accessAvailableHint')}
            </p>
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
                {t('platformShell.common.openDocuments')}
              </Link>
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
