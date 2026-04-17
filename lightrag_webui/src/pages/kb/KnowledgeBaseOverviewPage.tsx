import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
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
          const message =
            loadError instanceof Error
              ? loadError.message
              : t('platformShell.kbOverview.loadFailed')
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
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)

  const configOverrideCount = Object.keys(knowledgeBase?.config_override || {}).length
  const createdAt = knowledgeBase?.created_at
    ? new Date(knowledgeBase.created_at).toLocaleString()
    : t('platformShell.kbSettings.notExposedYet')
  const statusLabel = loading
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

  // Small inline stats strip — replaces the old two-column grid of
  // "stat cards" which duplicated every value that's already in the
  // header (name, workspace, KB id, role).
  const stats: { label: string; value: string }[] = [
    { label: t('platformShell.common.status'), value: statusLabel },
    { label: t('platformShell.kbSettings.createdAt'), value: createdAt },
    {
      label: t('platformShell.kbOverview.configOverrides'),
      value: loading ? t('platformShell.common.loadingCount') : String(configOverrideCount),
    },
  ]

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* Hero strip */}
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.kbOverview.badge')}
            </Badge>
            <span className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
              {currentWorkspaceId} / {currentKnowledgeBaseId}
            </span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {knowledgeBase?.name || currentKnowledgeBaseId}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {knowledgeBase?.description ||
              t('platformShell.kbOverview.fallbackDescription')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" asChild>
            <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openDocuments')}
            </Link>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbRetrieval(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.kbOverview.runRetrieval')}
            </Link>
          </Button>
        </div>
      </header>

      {/* Access ribbon */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.kbOverview.scopeAndAccess')}
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

      {/* Inline stats row — status / created / overrides */}
      <div className="grid grid-cols-3 gap-px border-b border-border/60 bg-border/60 text-sm">
        {stats.map((item) => (
          <div key={item.label} className="bg-background px-6 py-3">
            <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
              {item.label}
            </p>
            <p className="mt-1 truncate font-medium text-foreground">{item.value}</p>
          </div>
        ))}
      </div>

      {error && (
        <div className="px-6 pt-4">
          <Alert className="border-border/70 bg-muted/20">
            <AlertTitle>{t('platformShell.kbOverview.compatibilityMode')}</AlertTitle>
            <AlertDescription>
              {t('platformShell.kbOverview.compatibilityDescription')}
            </AlertDescription>
          </Alert>
        </div>
      )}

      {/* Quick actions — flatter, 3-col grid */}
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 px-6 py-6">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
              {t('platformShell.kbOverview.nextActions')}
            </h2>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {quickActions.map((item) => {
              const Icon = item.icon
              return (
                <div
                  key={item.key}
                  className="flex flex-col gap-3 rounded-xl border border-border/60 bg-background/70 px-4 py-3 transition-colors hover:border-emerald-500/30"
                >
                  <div className="flex items-start gap-2.5">
                    <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                      <Icon className="size-4" aria-hidden="true" />
                    </span>
                    <div className="min-w-0 space-y-0.5">
                      <p className="truncate text-sm font-semibold text-foreground">
                        {item.label}
                      </p>
                      <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">
                        {item.description}
                      </p>
                    </div>
                  </div>
                  <div className="mt-auto">
                    {item.available ? (
                      <Button size="sm" variant="outline" className="w-full" asChild>
                        <Link to={item.to}>{t('platformShell.common.open')}</Link>
                      </Button>
                    ) : (
                      <Button size="sm" variant="outline" className="w-full" disabled>
                        {t('platformShell.common.locked')}
                      </Button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
