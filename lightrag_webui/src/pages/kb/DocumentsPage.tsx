import { lazy, Suspense, useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { FileStackIcon, SparklesIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes, defaultKnowledgeBaseId } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { useKBStore } from '@/stores/kb'
import { hasPermission, resolveEffectiveRole } from '@/lib/permissions'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import KBTabs from '@/components/documents/KBTabs'

const DocumentManager = lazy(() => import('@/features/DocumentManager'))

function DocumentsSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center px-6 py-10">
      <div className="flex items-center gap-3 text-sm text-muted-foreground">
        <span className="size-2 animate-pulse rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
        {t('platformShell.documents.loadingLabel')}
      </div>
    </div>
  )
}

/**
 * Documents page.
 *
 * PR-UI-2 slice — flatten the previous five-layer card nest (hero with
 * a right-column triple metadata block, a split access/operations card,
 * a workflow grid duplicating sidebar nav, a per-operation status card,
 * and finally the DocumentManager itself) down to:
 *
 *   1. Hero strip — badge + title + description + actions.
 *   2. Access ribbon — role description + capability chips.
 *   3. DocumentManager — fills flex-1 so upload / list / pagination
 *      gets the full viewport height instead of a fixed 980px box.
 *
 * See docs/platform-v2/ui-audit.md §2 / §4.8.
 */
export default function DocumentsPage() {
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
  // Raw store value: null until KBTabs seeds the first linked KB. Keep it
  // separate from the defaulted id so we can tell "not resolved yet" from
  // "resolved to a concrete KB".
  const rawActiveKbId = useKBStore((s) => s.activeKbId)
  const activeKbId = rawActiveKbId ?? defaultKnowledgeBaseId
  // When true, the documents surface aggregates across every KB in the
  // current workspace — KBTabs paints "全部知识库" as the active option.
  const [allKbsMode, setAllKbsMode] = useState(false)
  // KB-list resolution state, driven by KBTabs.onResolved. Gates the
  // DocumentManager mount so it does NOT fetch against the transient
  // ``default`` sentinel and then remount+refetch when the real KB is
  // seeded — the double-fetch waterfall that made navigation slow.
  const [kbCount, setKbCount] = useState<number | null>(null)
  const handleKbsResolved = useCallback((count: number) => setKbCount(count), [])
  // Reset resolution state when the workspace changes so we show a loading
  // state (not the previous workspace's docs) until KBTabs re-resolves.
  // "Store previous prop in state" pattern (setState during render is the
  // documented reset-on-prop-change idiom; reading a ref during render is
  // not allowed under React 19's rules).
  const [lastWorkspace, setLastWorkspace] = useState(currentWorkspaceId)
  if (lastWorkspace !== currentWorkspaceId) {
    setLastWorkspace(currentWorkspaceId)
    if (kbCount !== null) setKbCount(null)
  }
  const kbResolved = kbCount !== null
  const showManager = allKbsMode || (kbResolved && rawActiveKbId != null)
  const showEmptyState = !allKbsMode && kbResolved && kbCount === 0

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.documents.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.documents.title')}
          </h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openRetrieval')}
            </Link>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbGraph(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openGraph')}
            </Link>
          </Button>
        </div>
      </header>

      {canViewKnowledgeBase && (
        <KBTabs
          workspaceId={currentWorkspaceId}
          allowAllOption
          allKbsSelected={allKbsMode}
          onPickAll={() => setAllKbsMode(true)}
          onChange={() => setAllKbsMode(false)}
          onResolved={handleKbsResolved}
        />
      )}

      {!canViewKnowledgeBase && (
        <Alert className="mx-6 mt-4 border-border/70 bg-muted/20">
          <AlertTitle>{t('platformShell.documents.unavailableTitle')}</AlertTitle>
          <AlertDescription>
            {t('platformShell.documents.unavailableDescription')}
          </AlertDescription>
        </Alert>
      )}

      {canViewKnowledgeBase ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense fallback={<DocumentsSurfaceLoading />}>
            {/* Only mount the manager once the KB is resolved (or in
                allKbsMode). Mounting under the transient ``default``
                sentinel fired a throwaway /documents/paginated and then
                remounted+refetched when KBTabs seeded the real KB — the
                double round-trip that made entering a KB take 10s+. Now
                it mounts once, keyed on the real KB id, so switching KB
                still forces a fresh single fetch with the new X-KB-Id. */}
            {showManager ? (
              <DocumentManager
                key={allKbsMode ? '__all_kbs__' : activeKbId}
                allKbsMode={allKbsMode}
              />
            ) : showEmptyState ? (
              <div className="flex h-full items-center justify-center px-6 py-10">
                <p className="max-w-md text-center text-sm text-muted-foreground">
                  {t('platformShell.documents.noKbHint', {
                    defaultValue: '当前工作区还没有知识库。请在上方"新建知识库"后再上传文档。',
                  })}
                </p>
              </div>
            ) : (
              <DocumentsSurfaceLoading />
            )}
          </Suspense>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 py-10">
          <div className="flex max-w-md flex-col items-center gap-4 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted/40">
              <FileStackIcon className="size-5 text-muted-foreground" aria-hidden="true" />
            </div>
            <p className="text-sm text-muted-foreground">
              {t('platformShell.documents.accessAvailableHint')}
            </p>
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
                <SparklesIcon className="size-4" aria-hidden="true" />
                {t('platformShell.common.openRetrieval')}
              </Link>
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
