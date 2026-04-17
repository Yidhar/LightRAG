import { lazy, Suspense, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { FileStackIcon, ShieldCheckIcon, SparklesIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes, defaultKnowledgeBaseId } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { useKBStore } from '@/stores/kb'
import {
  hasPermission,
  resolveEffectiveRole,
  roleDescriptionKeys,
  summarizeRoleCapabilityKeys,
} from '@/lib/permissions'
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
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)
  const activeKbId = useKBStore((s) => s.activeKbId) ?? defaultKnowledgeBaseId
  // When true, the documents surface aggregates across every KB in the
  // current workspace — KBTabs paints "全部知识库" as the active option.
  const [allKbsMode, setAllKbsMode] = useState(false)

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
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.documents.description')}
          </p>
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

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.documents.accessAndOperations')}
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

      {canViewKnowledgeBase && (
        <KBTabs
          workspaceId={currentWorkspaceId}
          allowAllOption
          allKbsSelected={allKbsMode}
          onPickAll={() => setAllKbsMode(true)}
          onChange={() => setAllKbsMode(false)}
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
            {/* Key the manager on activeKbId so switching tabs forces a
                fresh mount (and a fresh fetch with the new X-KB-Id
                header injected by the axios interceptor). In allKbsMode
                we key on the sentinel so the remount resets state when
                flipping into / out of the aggregated view. */}
            <DocumentManager
              key={allKbsMode ? '__all_kbs__' : activeKbId}
              allKbsMode={allKbsMode}
            />
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
