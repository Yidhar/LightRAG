import { lazy, Suspense } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { BracesIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes, defaultWorkspaceId } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { backendBaseUrl } from '@/lib/constants'
import { useAuthStore } from '@/stores/state'
import { hasPermission, isSystemAdmin, resolveEffectiveRole } from '@/lib/permissions'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'

const ApiSite = lazy(() => import('@/features/ApiSite'))

function ApiSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center px-6 py-10">
      <div className="flex items-center gap-3 text-sm text-muted-foreground">
        <span className="size-2 animate-pulse rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
        {t('platformShell.api.loadingLabel')}
      </div>
    </div>
  )
}

export default function ApiPage() {
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
  const apiDocsUrl = backendBaseUrl ? `${backendBaseUrl}/docs` : '/docs'

  // API reference is platform-admin only. A regular user who types the
  // URL directly (or follows a stale bookmark) gets bounced back to the
  // workspace documents page rather than seeing a Swagger UI they can't
  // usefully operate.
  if (!isSystemAdmin(memberships, defaultWorkspaceId)) {
    return (
      <Navigate
        to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}
        replace
      />
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* Hero strip — badge + title + description + actions */}
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.api.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.api.title')}
          </h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openRetrieval')}
            </Link>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <a href={apiDocsUrl} target="_blank" rel="noreferrer">
              {t('platformShell.api.openDocsInNewTab')}
            </a>
          </Button>
        </div>
      </header>

      {canQueryKnowledgeBase ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense fallback={<ApiSurfaceLoading />}>
            <ApiSite />
          </Suspense>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 py-10">
          <div className="flex max-w-md flex-col items-center gap-4 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted/40">
              <BracesIcon
                className="size-5 text-muted-foreground"
                aria-hidden="true"
              />
            </div>
            <Alert className="border-border/70 bg-muted/20 text-left">
              <AlertTitle>{t('platformShell.api.unavailableTitle')}</AlertTitle>
              <AlertDescription>
                {t('platformShell.api.unavailableDescription')}
              </AlertDescription>
            </Alert>
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
                {t('platformShell.common.openRetrieval')}
              </Link>
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
