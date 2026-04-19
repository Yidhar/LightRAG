import { lazy, Suspense, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { SparklesIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes, defaultWorkspaceId } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { useKBStore } from '@/stores/kb'
import { hasPermission, isSystemAdmin, resolveEffectiveRole } from '@/lib/permissions'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import KBTabs from '@/components/documents/KBTabs'

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
  // API reference is platform-admin only — hide its entry point from
  // regular users so the surface stops advertising a page they can't
  // usefully operate.
  const platformAdmin = isSystemAdmin(memberships, defaultWorkspaceId)

  // Retrieval defaults to federated mode — every query spans every KB in
  // the current workspace until the operator narrows scope via the
  // dropdown. Start with the sentinel selected and ``activeKbId`` null so
  // the axios interceptor does not inject ``X-KB-Id`` on the first query.
  const setActiveKb = useKBStore((s) => s.setActiveKb)
  const activeKbId = useKBStore((s) => s.activeKbId)
  const [allKbsMode, setAllKbsMode] = useState(true)
  // Reset-on-prop-change via the "store previous prop in state"
  // pattern recommended by the React docs. An effect here would trip
  // the ``set-state-in-effect`` rule; deriving from a render-phase
  // comparison keeps the reset synchronous with the route change and
  // avoids a wasted render where the stale per-KB scope would leak
  // into the first query against the new workspace.
  const [lastWorkspaceId, setLastWorkspaceId] = useState(currentWorkspaceId)
  if (lastWorkspaceId !== currentWorkspaceId) {
    setLastWorkspaceId(currentWorkspaceId)
    setAllKbsMode(true)
    setActiveKb(null)
  }

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
          {platformAdmin && (
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.kbApi(currentWorkspaceId, currentKnowledgeBaseId)}>
                {t('platformShell.common.openApiDocs')}
              </Link>
            </Button>
          )}
          {canManageKnowledgeBaseSettings && (
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.kbSettings(currentWorkspaceId, currentKnowledgeBaseId)}>
                {t('platformShell.common.kbSettings')}
              </Link>
            </Button>
          )}
        </div>
      </header>

      {canQueryKnowledgeBase && (
        <KBTabs
          workspaceId={currentWorkspaceId}
          allowAllOption
          allKbsSelected={allKbsMode}
          onPickAll={() => setAllKbsMode(true)}
          onChange={() => setAllKbsMode(false)}
        />
      )}

      {!canQueryKnowledgeBase && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.retrieval.unavailableTitle')}</AlertTitle>
          <AlertDescription>{t('platformShell.retrieval.unavailableDescription')}</AlertDescription>
        </Alert>
      )}

      {canQueryKnowledgeBase ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense fallback={<RetrievalSurfaceLoading />}>
            {/* Key on the picked scope so switching KB remounts and
                re-fetches the per-KB history. In allKbsMode we key on
                the sentinel so remount fires when flipping in/out of
                the federated view. */}
            <RetrievalTesting
              key={allKbsMode ? '__all_kbs__' : activeKbId ?? '__default__'}
            />
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
