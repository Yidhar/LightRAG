import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import { BracesIcon, ShieldCheckIcon, SparklesIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { backendBaseUrl } from '@/lib/constants'
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

const ApiSite = lazy(() => import('@/features/ApiSite'))

function ApiSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_45%)] p-6">
      <div className="w-full max-w-xl rounded-[28px] border border-border/70 bg-background/85 p-6 shadow-sm">
        <div className="mb-4 flex items-center gap-2">
          <span className="size-2 rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
          <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
            {t('platformShell.api.loadingLabel')}
          </p>
          <Badge variant="outline" className="ml-auto rounded-full px-3 py-1">
            {t('brand.name')}
          </Badge>
        </div>

        <h3 className="text-lg font-semibold text-foreground">{t('platformShell.api.loadingTitle')}</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {t('platformShell.api.loadingDescription')}
        </p>

        <div className="mt-5 space-y-3">
          <div className="h-10 animate-pulse rounded-2xl border border-border/70 bg-muted/25" />
          <div className="h-48 animate-pulse rounded-[24px] border border-border/70 bg-muted/15" />
        </div>
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
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)
  const apiDocsUrl = backendBaseUrl ? `${backendBaseUrl}/docs` : '/docs'

  return (
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-6">
        <section className="surface-panel overflow-hidden rounded-[28px] border border-border/70 bg-gradient-to-br from-emerald-500/10 via-card to-card">
          <div className="grid gap-6 px-6 py-7 lg:grid-cols-[1.55fr_1fr] lg:px-8">
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                  {t('platformShell.api.badge')}
                </Badge>
                <AccessBadge role={effectiveRole} />
                <Badge variant="outline" className="rounded-full px-3 py-1">
                  {t('brand.name')}
                </Badge>
              </div>

              <div className="space-y-2">
                <h1 className="text-3xl font-semibold tracking-tight text-foreground">
                  {t('platformShell.api.title')}
                </h1>
                <p className="max-w-2xl text-sm leading-7 text-muted-foreground">
                  {t('platformShell.api.description')}
                </p>
              </div>

              <div className="flex flex-wrap gap-3">
                <Button variant="outline" asChild>
                  <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.common.openRetrieval')}
                  </Link>
                </Button>
                <Button variant="outline" asChild>
                  <a href={apiDocsUrl} target="_blank" rel="noreferrer">
                    {t('platformShell.api.openDocsInNewTab')}
                  </a>
                </Button>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-1">
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.common.workspace')}</CardDescription>
                  <CardTitle className="text-xl">{currentWorkspaceId}</CardTitle>
                </CardHeader>
              </Card>
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.common.knowledgeBase')}</CardDescription>
                  <CardTitle className="text-xl">{currentKnowledgeBaseId}</CardTitle>
                </CardHeader>
              </Card>
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.api.docsSource')}</CardDescription>
                  <CardTitle className="text-xl">/docs</CardTitle>
                </CardHeader>
              </Card>
            </div>
          </div>
        </section>

        <section className="grid gap-6 xl:grid-cols-[1fr_1.55fr]">
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.api.developerAccess')}
              </CardTitle>
              <CardDescription>{t(roleDescriptionKeys[effectiveRole])}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {capabilitySummary.map((capabilityKey) => (
                  <Badge key={capabilityKey} variant="outline" className="rounded-full bg-muted/40 px-3 py-1">
                    {t(capabilityKey)}
                  </Badge>
                ))}
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-border/70 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.common.currentPath')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {currentWorkspaceId} / {currentKnowledgeBaseId}
                  </p>
                </div>
                <div className="rounded-2xl border border-border/70 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.common.surfaceState')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {canQueryKnowledgeBase
                      ? t('platformShell.api.referenceReady')
                      : t('platformShell.api.awaitingQueryAccess')}
                  </p>
                </div>
              </div>

              {!canQueryKnowledgeBase ? (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.api.unavailableTitle')}</AlertTitle>
                  <AlertDescription>
                    {t('platformShell.api.unavailableDescription')}
                  </AlertDescription>
                </Alert>
              ) : (
                <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                  <div className="mb-3 flex items-center gap-2">
                    <SparklesIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                    <p className="font-medium text-foreground">{t('platformShell.api.referenceNotes')}</p>
                  </div>
                  <p className="text-sm leading-6 text-muted-foreground">
                    {t('platformShell.api.referenceNotesDescription')}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BracesIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.api.liveDocs')}
              </CardTitle>
              <CardDescription>
                {t('platformShell.api.liveDocsDescription')}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {!canQueryKnowledgeBase ? (
                <>
                  <Alert className="border-border/70 bg-muted/20">
                    <AlertTitle>{t('platformShell.api.queryAccessRequired')}</AlertTitle>
                    <AlertDescription>
                      {t('platformShell.api.iframeHidden')}
                    </AlertDescription>
                  </Alert>

                  <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                    {t('platformShell.api.accessAvailableHint')}
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" asChild>
                      <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
                        {t('platformShell.common.openRetrieval')}
                      </Link>
                    </Button>
                  </div>
                </>
              ) : (
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border/70 bg-background/75 px-4 py-3">
                    <div className="space-y-1">
                      <p className="text-sm font-medium text-foreground">
                        {t('platformShell.api.interactiveBackendReference')}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {t('platformShell.api.embeddedFrom', {
                          apiDocsUrl,
                        })}
                      </p>
                    </div>
                    <Badge variant="outline" className="rounded-full px-3 py-1">
                      {t('platformShell.api.liveDocsBadge')}
                    </Badge>
                  </div>

                  <div className="overflow-hidden rounded-[24px] border border-border/70 bg-background/70">
                    <div className="h-[760px] min-h-[640px]">
                      <Suspense fallback={<ApiSurfaceLoading />}>
                        <ApiSite />
                      </Suspense>
                    </div>
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
