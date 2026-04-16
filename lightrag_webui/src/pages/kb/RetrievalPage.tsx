import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  BookOpenTextIcon,
  SearchCheckIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from 'lucide-react'
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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import AccessBadge from '@/components/navigation/AccessBadge'

const RetrievalTesting = lazy(() => import('@/features/RetrievalTesting'))

function RetrievalSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_45%)] p-6">
      <div className="w-full max-w-xl rounded-[28px] border border-border/70 bg-background/85 p-6 shadow-sm">
        <div className="mb-4 flex items-center gap-2">
          <span className="size-2 rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
          <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
            {t('platformShell.retrieval.loadingLabel')}
          </p>
          <Badge variant="outline" className="ml-auto rounded-full px-3 py-1">
            {t('brand.name')}
          </Badge>
        </div>

        <h3 className="text-lg font-semibold text-foreground">{t('platformShell.retrieval.loadingTitle')}</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {t('platformShell.retrieval.loadingDescription')}
        </p>

        <div className="mt-5 grid gap-3 sm:grid-cols-3">
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="rounded-2xl border border-border/70 bg-muted/30 p-4">
              <div className="h-2.5 w-20 animate-pulse rounded-full bg-muted-foreground/20" />
              <div className="mt-3 h-8 animate-pulse rounded-xl bg-muted-foreground/10" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

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
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6 px-4 py-6 sm:px-6">
        <section className="surface-panel overflow-hidden rounded-[32px] border border-border/70 bg-gradient-to-br from-emerald-500/12 via-card to-card">
          <div className="grid gap-8 px-6 py-7 lg:grid-cols-[minmax(0,1.35fr)_360px] lg:px-8">
            <div className="space-y-5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                  {t('platformShell.retrieval.badge')}
                </Badge>
                <AccessBadge role={effectiveRole} />
                <Badge variant="outline" className="rounded-full px-3 py-1">
                  {t('brand.name')}
                </Badge>
              </div>

              <div className="space-y-3">
                <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                  {t('platformShell.retrieval.title')}
                </h1>
                <p className="max-w-3xl text-sm leading-7 text-muted-foreground sm:text-[15px]">
                  {t('platformShell.retrieval.description')}
                </p>
              </div>

              <div className="flex flex-wrap gap-3">
                <Button variant="outline" asChild>
                  <Link to={appRoutes.kbApi(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.common.openApiDocs')}
                  </Link>
                </Button>
                {canManageKnowledgeBaseSettings && (
                  <Button variant="outline" asChild>
                    <Link to={appRoutes.kbSettings(currentWorkspaceId, currentKnowledgeBaseId)}>
                      {t('platformShell.common.kbSettings')}
                    </Link>
                  </Button>
                )}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
              <div className="rounded-[28px] border border-border/70 bg-background/80 p-5 shadow-sm">
                <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.common.workspace')}
                </p>
                <p className="mt-3 text-base font-semibold text-foreground">{currentWorkspaceId}</p>
              </div>
              <div className="rounded-[28px] border border-border/70 bg-background/80 p-5 shadow-sm">
                <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.common.knowledgeBase')}
                </p>
                <p className="mt-3 text-base font-semibold text-foreground">{currentKnowledgeBaseId}</p>
              </div>
              <div className="rounded-[28px] border border-border/70 bg-background/80 p-5 shadow-sm">
                <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.common.surfaceState')}
                </p>
                <p className="mt-3 text-base font-semibold text-foreground">
                  {canQueryKnowledgeBase
                    ? t('platformShell.retrieval.queryReadyPromptLab')
                    : t('platformShell.retrieval.awaitingQueryAccess')}
                </p>
              </div>
            </div>
          </div>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.retrieval.accessAndScope')}
              </CardTitle>
              <CardDescription>{t(roleDescriptionKeys[effectiveRole])}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="flex flex-wrap gap-2">
                {capabilitySummary.map((capabilityKey) => (
                  <Badge key={capabilityKey} variant="outline" className="rounded-full bg-muted/40 px-3 py-1">
                    {t(capabilityKey)}
                  </Badge>
                ))}
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-[24px] border border-border/70 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.common.currentPath')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {currentWorkspaceId} / {currentKnowledgeBaseId}
                  </p>
                </div>
                <div className="rounded-[24px] border border-border/70 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.retrieval.queryAccess')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {canQueryKnowledgeBase
                      ? t('platformShell.retrieval.interactive')
                      : t('platformShell.common.unavailable')}
                  </p>
                </div>
              </div>
              {canQueryKnowledgeBase && (
                <div className="rounded-[24px] border border-border/70 bg-background/75 p-4">
                  <div className="mb-3 flex items-center gap-2">
                    <SparklesIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                    <p className="font-medium text-foreground">{t('platformShell.common.operatorNotes')}</p>
                  </div>
                  <p className="text-sm leading-6 text-muted-foreground">
                    {t('platformShell.retrieval.operatorNotesDescription')}
                  </p>
                </div>
              )}

              {!canQueryKnowledgeBase ? (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.retrieval.unavailableTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.retrieval.unavailableDescription')}</AlertDescription>
                </Alert>
              ) : !canManageKnowledgeBaseSettings ? (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.retrieval.readOnlyAdminTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.retrieval.readOnlyAdminDescription')}</AlertDescription>
                </Alert>
              ) : null}
            </CardContent>
          </Card>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <SearchCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.retrieval.askKnowledgeBase')}
              </CardTitle>
              <CardDescription>{t('platformShell.retrieval.askKnowledgeBaseDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {!canQueryKnowledgeBase ? (
                <>
                  <Alert className="border-border/70 bg-muted/20">
                    <AlertTitle>{t('platformShell.retrieval.queryAccessRequired')}</AlertTitle>
                    <AlertDescription>{t('platformShell.retrieval.testingDisabled')}</AlertDescription>
                  </Alert>

                  <div className="rounded-[24px] border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                    {t('platformShell.retrieval.accessAvailableHint')}
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" asChild>
                      <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
                        {t('platformShell.common.openDocuments')}
                      </Link>
                    </Button>
                  </div>
                </>
              ) : (
                <div className="overflow-hidden rounded-[28px] border border-border/70 bg-background/70 shadow-sm">
                  <div className="h-[980px] min-h-[760px]">
                    <Suspense fallback={<RetrievalSurfaceLoading />}>
                      <RetrievalTesting />
                    </Suspense>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </section>
      </div>
    </div>
  )
}
