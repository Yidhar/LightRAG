import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import { SparklesIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { hasPermission, resolveEffectiveRole } from '@/lib/permissions'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'

const RetrievalTesting = lazy(() => import('@/features/RetrievalTesting'))

function RetrievalSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center px-6 py-10">
      <div className="flex items-center gap-3 text-sm text-muted-foreground">
        <span className="size-2 animate-pulse rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
        {t('platformShell.retrieval.loadingLabel')}
      </div>
    </div>
  )
}

/**
 * Retrieval page.
 *
 *   1. Hero strip — badge + title + description + inline workspace
 *      switcher + primary actions. Workspace switch happens in-place via
 *      ``navigate()`` so the conversation state persists per (user,
 *      workspace, KB) through the server-side history sync.
 *   2. Main surface — RetrievalTesting fills flex-1.
 *
 * Note: the old "Access & scope" ribbon was removed — role/capability
 * badges were noise on this page and the workspace switcher took that
 * real estate.
 */
export default function RetrievalPage() {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )

  const canQueryKnowledgeBase = hasPermission(effectiveRole, 'kb:query')
  const canManageKnowledgeBaseSettings = hasPermission(effectiveRole, 'kb:manage_settings')

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.retrieval.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.retrieval.title')}
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbApi(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openApiDocs')}
            </Link>
          </Button>
          {canManageKnowledgeBaseSettings && (
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.kbSettings(currentWorkspaceId, currentKnowledgeBaseId)}>
                {t('platformShell.common.kbSettings')}
              </Link>
            </Button>
          )}
        </div>
      </header>

      {!canQueryKnowledgeBase && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.retrieval.unavailableTitle')}</AlertTitle>
          <AlertDescription>{t('platformShell.retrieval.unavailableDescription')}</AlertDescription>
        </Alert>
      )}

      {canQueryKnowledgeBase ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense fallback={<RetrievalSurfaceLoading />}>
            <RetrievalTesting />
          </Suspense>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 py-10">
          <div className="flex max-w-md flex-col items-center gap-4 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted/40">
              <SparklesIcon className="size-5 text-muted-foreground" aria-hidden="true" />
            </div>
            <p className="text-sm text-muted-foreground">
              {t('platformShell.retrieval.accessAvailableHint')}
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
