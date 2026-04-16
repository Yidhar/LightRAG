import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  ActivityIcon,
  BookOpenTextIcon,
  BracesIcon,
  FileStackIcon,
  NetworkIcon,
  Settings2Icon,
  ShieldCheckIcon,
  SparklesIcon,
} from 'lucide-react'

import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { getKnowledgeBase, type KnowledgeBaseRecord } from '@/api/lightrag'
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

export default function KnowledgeBaseOverviewPage() {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )

  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBaseRecord | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    const loadKnowledgeBase = async () => {
      try {
        setLoading(true)
        setError(null)
        const response = await getKnowledgeBase(currentWorkspaceId, currentKnowledgeBaseId)
        if (!cancelled) {
          setKnowledgeBase(response)
        }
      } catch (loadError) {
        if (!cancelled) {
          const message = loadError instanceof Error ? loadError.message : t('platformShell.kbOverview.loadFailed')
          setError(message)
          setKnowledgeBase(null)
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    loadKnowledgeBase()

    return () => {
      cancelled = true
    }
  }, [currentKnowledgeBaseId, currentWorkspaceId, t])

  const canOpenDocuments = hasPermission(effectiveRole, 'kb:view')
  const canOpenRetrieval = hasPermission(effectiveRole, 'kb:query')
  const canOpenGraph = hasPermission(effectiveRole, 'kb:view')
  const canOpenApi = hasPermission(effectiveRole, 'kb:query')
  const canOpenSettings = hasPermission(effectiveRole, 'kb:manage_settings')

  const configOverrideCount = Object.keys(knowledgeBase?.config_override || {}).length
  const createdAt = knowledgeBase?.created_at
    ? new Date(knowledgeBase.created_at).toLocaleString()
    : t('platformShell.kbSettings.notExposedYet')
  const currentStatusLabel = loading
    ? t('platformShell.kbOverview.loadingMetadata')
    : knowledgeBase?.status || t('platformShell.kbOverview.compatibilityMode')

  const quickActions = useMemo(
    () => [
      {
        key: 'documents',
        label: t('platformShell.kbOverview.quickActions.documents.label'),
        description: t('platformShell.kbOverview.quickActions.documents.description'),
        to: appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId),
        icon: FileStackIcon,
        available: canOpenDocuments,
      },
      {
        key: 'retrieval',
        label: t('platformShell.kbOverview.quickActions.retrieval.label'),
        description: t('platformShell.kbOverview.quickActions.retrieval.description'),
        to: appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId),
        icon: SparklesIcon,
        available: canOpenRetrieval,
      },
      {
        key: 'graph',
        label: t('platformShell.kbOverview.quickActions.graph.label'),
        description: t('platformShell.kbOverview.quickActions.graph.description'),
        to: appRoutes.kbGraph(currentWorkspaceId, currentKnowledgeBaseId),
        icon: NetworkIcon,
        available: canOpenGraph,
      },
      {
        key: 'api',
        label: t('platformShell.kbOverview.quickActions.api.label'),
        description: t('platformShell.kbOverview.quickActions.api.description'),
        to: appRoutes.kbApi(currentWorkspaceId, currentKnowledgeBaseId),
        icon: BracesIcon,
        available: canOpenApi,
      },
      {
        key: 'settings',
        label: t('platformShell.kbOverview.quickActions.settings.label'),
        description: t('platformShell.kbOverview.quickActions.settings.description'),
        to: appRoutes.kbSettings(currentWorkspaceId, currentKnowledgeBaseId),
        icon: Settings2Icon,
        available: canOpenSettings,
      },
    ],
    [
      canOpenApi,
      canOpenDocuments,
      canOpenGraph,
      canOpenRetrieval,
      canOpenSettings,
      currentKnowledgeBaseId,
      currentWorkspaceId,
      t,
    ]
  )

  const overviewStats = [
    {
      label: t('platformShell.common.workspace'),
      value: currentWorkspaceId,
    },
    {
      label: t('platformShell.common.knowledgeBase'),
      value: currentKnowledgeBaseId,
    },
    {
      label: t('platformShell.common.status'),
      value: currentStatusLabel,
    },
    {
      label: t('platformShell.kbSettings.createdAt'),
      value: createdAt,
    },
    {
      label: t('platformShell.kbOverview.configOverrides'),
      value: loading ? t('platformShell.common.loadingCount') : String(configOverrideCount),
    },
    {
      label: t('platformShell.common.surfaceState'),
      value: canOpenRetrieval ? t('platformShell.common.enabled') : t('platformShell.common.locked'),
    },
  ]

  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)

  return (
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6 px-6 py-6">
        <section className="surface-panel overflow-hidden rounded-[32px] border border-border/70 bg-gradient-to-br from-emerald-500/12 via-card to-card">
          <div className="grid gap-8 px-6 py-7 lg:grid-cols-[minmax(0,1.35fr)_360px] lg:px-8">
            <div className="space-y-5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                  {t('platformShell.kbOverview.badge')}
                </Badge>
                <AccessBadge role={effectiveRole} />
                <Badge variant="outline" className="rounded-full px-3 py-1">
                  {t('brand.name')}
                </Badge>
              </div>

              <div className="space-y-3">
                <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                  {knowledgeBase?.name || currentKnowledgeBaseId}
                </h1>
                <p className="max-w-3xl text-sm leading-7 text-muted-foreground sm:text-[15px]">
                  {knowledgeBase?.description || t('platformShell.kbOverview.fallbackDescription')}
                </p>
              </div>

              <div className="flex flex-wrap gap-3">
                <Button asChild>
                  <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.common.openDocuments')}
                  </Link>
                </Button>
                <Button variant="outline" asChild>
                  <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.kbOverview.runRetrieval')}
                  </Link>
                </Button>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {overviewStats.slice(0, 3).map((item) => (
                  <div
                    key={item.label}
                    className="rounded-[24px] border border-border/70 bg-background/75 px-4 py-4 shadow-sm"
                  >
                    <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      {item.label}
                    </p>
                    <p className="mt-2 text-sm font-medium text-foreground">{item.value}</p>
                  </div>
                ))}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
              {overviewStats.slice(3).map((item) => (
                <div
                  key={item.label}
                  className="rounded-[28px] border border-border/70 bg-background/80 p-5 shadow-sm"
                >
                  <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    {item.label}
                  </p>
                  <p className="mt-3 text-base font-semibold text-foreground">{item.value}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="grid gap-6 xl:grid-cols-[1.05fr_1fr]">
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbOverview.scopeAndAccess')}
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
                    {t('platformShell.common.surfaceState')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {canOpenRetrieval ? t('platformShell.retrieval.queryReadyPromptLab') : t('platformShell.common.locked')}
                  </p>
                </div>
              </div>

              {error && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.kbOverview.compatibilityMode')}</AlertTitle>
                  <AlertDescription>{t('platformShell.kbOverview.compatibilityDescription')}</AlertDescription>
                </Alert>
              )}
            </CardContent>
          </Card>

          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ActivityIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbSettings.metadataAndOverridesTitle')}
              </CardTitle>
              <CardDescription>{t('platformShell.kbSettings.metadataAndOverridesDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-[24px] border border-border/70 bg-background/80 p-4">
                <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.common.status')}
                </p>
                <p className="mt-2 text-sm font-medium text-foreground">{currentStatusLabel}</p>
              </div>
              <div className="rounded-[24px] border border-border/70 bg-background/80 p-4">
                <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.kbOverview.configOverrides')}
                </p>
                <p className="mt-2 text-sm font-medium text-foreground">{configOverrideCount}</p>
              </div>
              <div className="rounded-[24px] border border-border/70 bg-background/80 p-4 sm:col-span-2">
                <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.common.knowledgeBase')}
                </p>
                <p className="mt-2 text-sm font-medium text-foreground">
                  {knowledgeBase?.name || currentKnowledgeBaseId}
                </p>
              </div>
            </CardContent>
          </Card>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BookOpenTextIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbOverview.nextActions')}
              </CardTitle>
              <CardDescription>{t('platformShell.kbOverview.nextActionsDescription')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {quickActions.map((item) => {
                  const Icon = item.icon
                  return (
                    <div
                      key={item.key}
                      className="flex h-full flex-col rounded-[28px] border border-border/70 bg-background/85 p-6 shadow-sm"
                    >
                      <div className="mb-4">
                        <div className="flex size-12 items-center justify-center rounded-[20px] bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                          <Icon className="size-5" />
                        </div>
                      </div>

                      <div className="space-y-2">
                        <p className="text-base font-semibold text-foreground">{item.label}</p>
                        <p className="text-sm leading-6 text-muted-foreground">{item.description}</p>
                      </div>

                      <div className="mt-auto pt-5">
                        {item.available ? (
                          <Button variant="outline" asChild className="w-full justify-center">
                            <Link to={item.to}>{t('platformShell.common.open')}</Link>
                          </Button>
                        ) : (
                          <Button variant="outline" disabled className="w-full justify-center">
                            {t('platformShell.common.locked')}
                          </Button>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </CardContent>
          </Card>
        </section>
      </div>
    </div>
  )
}
