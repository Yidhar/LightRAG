import { lazy, Suspense } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ActivityIcon,
  FileStackIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UploadIcon,
  WorkflowIcon,
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

const DocumentManager = lazy(() => import('@/features/DocumentManager'))

function DocumentsSurfaceLoading() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full items-center justify-center bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_45%)] p-6">
      <div className="w-full max-w-2xl rounded-[28px] border border-border/70 bg-background/85 p-6 shadow-sm">
        <div className="mb-4 flex items-center gap-2">
          <span className="size-2 rounded-full bg-emerald-500 shadow-[0_0_0_6px_rgba(16,185,129,0.14)]" />
          <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
            {t('platformShell.documents.loadingLabel')}
          </p>
          <Badge variant="outline" className="ml-auto rounded-full px-3 py-1">
            {t('brand.name')}
          </Badge>
        </div>

        <h3 className="text-lg font-semibold text-foreground">{t('platformShell.documents.loadingTitle')}</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {t('platformShell.documents.loadingDescription')}
        </p>

        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <div className="rounded-[24px] border border-border/70 bg-muted/25 p-4">
            <div className="h-2.5 w-24 animate-pulse rounded-full bg-muted-foreground/20" />
            <div className="mt-3 h-28 animate-pulse rounded-2xl bg-muted-foreground/10" />
          </div>
          <div className="rounded-[24px] border border-border/70 bg-muted/20 p-4">
            <div className="h-2.5 w-24 animate-pulse rounded-full bg-muted-foreground/20" />
            <div className="mt-3 space-y-3">
              <div className="h-10 animate-pulse rounded-xl bg-muted-foreground/10" />
              <div className="h-10 animate-pulse rounded-xl bg-muted-foreground/10" />
              <div className="h-10 animate-pulse rounded-xl bg-muted-foreground/10" />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

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
  const canUploadDocuments = hasPermission(effectiveRole, 'kb:upload_document')
  const canDeleteDocuments = hasPermission(effectiveRole, 'kb:delete_document')
  const canManageKnowledgeBaseSettings = hasPermission(effectiveRole, 'kb:manage_settings')
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)
  const surfaceState =
    canUploadDocuments || canDeleteDocuments
      ? t('platformShell.documents.managedOperations')
      : t('platformShell.documents.readOnlyReview')

  const operationCards = [
    {
      label: t('platformShell.documents.operationMode'),
      value: canViewKnowledgeBase ? surfaceState : t('platformShell.documents.noDocumentAccess'),
    },
    {
      label: t('platformShell.documents.upload'),
      value: canUploadDocuments ? t('platformShell.common.enabled') : t('platformShell.common.locked'),
    },
    {
      label: t('platformShell.documents.deleteOrClear'),
      value: canDeleteDocuments ? t('platformShell.common.enabled') : t('platformShell.common.locked'),
    },
    {
      label: t('platformShell.documents.pipelineControls'),
      value: canManageKnowledgeBaseSettings
        ? t('platformShell.documents.extended')
        : t('platformShell.documents.standard'),
    },
  ]

  const workflowCards = [
    {
      title: t('platformShell.documents.badge'),
      description: t('platformShell.documents.description'),
      icon: UploadIcon,
    },
    {
      title: t('platformShell.documents.pipelineControls'),
      description: t('platformShell.documents.operatorNotesDescription'),
      icon: ActivityIcon,
    },
    {
      title: t('platformShell.retrieval.badge'),
      description: t('platformShell.retrieval.description'),
      icon: SparklesIcon,
    },
    {
      title: t('platformShell.graph.badge'),
      description: t('platformShell.graph.description'),
      icon: WorkflowIcon,
    },
  ]

  return (
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6 px-6 py-6">
        <section className="surface-panel overflow-hidden rounded-[32px] border border-border/70 bg-gradient-to-br from-emerald-500/12 via-card to-card">
          <div className="grid gap-8 px-6 py-7 lg:grid-cols-[minmax(0,1.35fr)_360px] lg:px-8">
            <div className="space-y-5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                  {t('platformShell.documents.badge')}
                </Badge>
                <AccessBadge role={effectiveRole} />
                <Badge variant="outline" className="rounded-full px-3 py-1">
                  {t('brand.name')}
                </Badge>
              </div>

              <div className="space-y-3">
                <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                  {t('platformShell.documents.title')}
                </h1>
                <p className="max-w-3xl text-sm leading-7 text-muted-foreground sm:text-[15px]">
                  {t('platformShell.documents.description')}
                </p>
              </div>

              <div className="flex flex-wrap gap-3">
                <Button variant="outline" asChild>
                  <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.common.openRetrieval')}
                  </Link>
                </Button>
                <Button variant="outline" asChild>
                  <Link to={appRoutes.kbGraph(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.common.openGraph')}
                  </Link>
                </Button>
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
                  {canViewKnowledgeBase ? surfaceState : t('platformShell.common.unavailable')}
                </p>
              </div>
            </div>
          </div>
        </section>

        <section className="grid gap-6 xl:grid-cols-[1.05fr_1fr]">
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.documents.accessAndOperations')}
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
                    {t('platformShell.documents.operationMode')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {canViewKnowledgeBase ? surfaceState : t('platformShell.documents.noDocumentAccess')}
                  </p>
                </div>
              </div>

              {!canViewKnowledgeBase ? (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.documents.unavailableTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.documents.unavailableDescription')}</AlertDescription>
                </Alert>
              ) : !canUploadDocuments && !canDeleteDocuments ? (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.documents.readOnlyTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.documents.readOnlyDescription')}</AlertDescription>
                </Alert>
              ) : (
                <div className="rounded-[24px] border border-border/70 bg-background/75 p-4">
                  <div className="mb-3 flex items-center gap-2">
                    <SparklesIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                    <p className="font-medium text-foreground">{t('platformShell.common.operatorNotes')}</p>
                  </div>
                  <p className="text-sm leading-6 text-muted-foreground">
                    {t('platformShell.documents.operatorNotesDescription')}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <WorkflowIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.documents.documentManager')}
              </CardTitle>
              <CardDescription>{t('platformShell.documents.documentManagerDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-2">
              {operationCards.map((item) => (
                <div key={item.label} className="rounded-[24px] border border-border/70 bg-background/80 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">{item.label}</p>
                  <p className="mt-2 text-sm font-medium text-foreground">{item.value}</p>
                </div>
              ))}
            </CardContent>
          </Card>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <FileStackIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.documents.documentManager')}
              </CardTitle>
              <CardDescription>{t('platformShell.documents.documentManagerDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_340px]">
                <div className="grid gap-4 sm:grid-cols-2">
                  {workflowCards.map((item) => {
                    const Icon = item.icon
                    return (
                      <div
                        key={item.title}
                        className="rounded-[28px] border border-border/70 bg-background/80 p-5 shadow-sm"
                      >
                        <div className="mb-4 flex size-11 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                          <Icon className="size-5" />
                        </div>
                        <p className="text-base font-semibold text-foreground">{item.title}</p>
                        <p className="mt-2 text-sm leading-6 text-muted-foreground">{item.description}</p>
                      </div>
                    )
                  })}
                </div>

                <div className="rounded-[28px] border border-border/70 bg-muted/20 p-5 shadow-sm">
                  <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.documents.accessAndOperations')}
                  </p>
                  <div className="mt-4 space-y-3">
                    {operationCards.map((item) => (
                      <div key={item.label} className="rounded-[22px] border border-border/70 bg-background/85 px-4 py-3">
                        <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">{item.label}</p>
                        <p className="mt-1 text-sm font-medium text-foreground">{item.value}</p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              {!canViewKnowledgeBase ? (
                <>
                  <Alert className="border-border/70 bg-muted/20">
                    <AlertTitle>{t('platformShell.documents.viewAccessRequired')}</AlertTitle>
                    <AlertDescription>{t('platformShell.documents.documentManagerHidden')}</AlertDescription>
                  </Alert>

                  <div className="rounded-[24px] border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                    {t('platformShell.documents.accessAvailableHint')}
                  </div>
                </>
              ) : (
                <div className="overflow-hidden rounded-[28px] border border-border/70 bg-background/70 shadow-sm">
                  <div className="h-[980px] min-h-[780px]">
                    <Suspense fallback={<DocumentsSurfaceLoading />}>
                      <DocumentManager />
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
