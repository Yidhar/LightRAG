import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import { NetworkIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { useKBStore } from '@/stores/kb'
import { hasPermission, resolveEffectiveRole } from '@/lib/permissions'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import KBTabs from '@/components/documents/KBTabs'

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
  // Graph reads can't federate across KBs — each KB has its own graph
  // storage and merging them server-side would be expensive and largely
  // meaningless for visualization. So the picker here is single-select
  // (no "全部" option); KBTabs auto-seeds the first linked KB.
  const activeKbId = useKBStore((s) => s.activeKbId)

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

      {canViewKnowledgeBase && <KBTabs workspaceId={currentWorkspaceId} />}

      {!canViewKnowledgeBase && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.graph.unavailableTitle')}</AlertTitle>
          <AlertDescription>{t('platformShell.graph.unavailableDescription')}</AlertDescription>
        </Alert>
      )}

      {canViewKnowledgeBase ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense fallback={<GraphSurfaceLoading />}>
            {/* Key on ``activeKbId`` so picking a different KB forces a
                fresh mount — sigma reinitializes, label dropdowns
                reload, and the cached viewport is thrown away. Without
                the remount the previous KB's graph data lingers because
                internal refs hold onto nodes/edges from the old query. */}
            <GraphViewer key={activeKbId ?? '__unscoped__'} />
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
