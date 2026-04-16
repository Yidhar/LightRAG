import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ShieldCheckIcon, SparklesIcon } from 'lucide-react'
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
 * PR-UI-2 flattens the previous 3-layer card nesting down to one compact
 * page chrome + a full-viewport retrieval surface:
 *
 *   1. Hero strip — badge + title + description + primary actions. No
 *      right-column metadata cards; workspace/KB identity lives in the
 *      top bar switchers.
 *   2. Access ribbon — single-row summary of role + capabilities,
 *      replacing the former "Access & scope" Card with 4 inner panels.
 *   3. Main surface — RetrievalTesting fills flex-1 (no fixed 980px
 *      height that left a black band when the chat was empty).
 *
 * See docs/platform-v2/ui-audit.md §2 / §3 / §4.9 for the driving
 * findings. Duplicate "ask this KB" headings inside RetrievalTesting
 * itself are tackled by PR-UI-3.
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
              {t('platformShell.retrieval.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.retrieval.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.retrieval.description')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
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

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.retrieval.accessAndScope')}
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

      {!canQueryKnowledgeBase && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.retrieval.unavailableTitle')}</AlertTitle>
          <AlertDescription>{t('platformShell.retrieval.unavailableDescription')}</AlertDescription>
        </Alert>
      )}
      {canQueryKnowledgeBase && !canManageKnowledgeBaseSettings && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.retrieval.readOnlyAdminTitle')}</AlertTitle>
          <AlertDescription>
            {t('platformShell.retrieval.readOnlyAdminDescription')}
          </AlertDescription>
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
