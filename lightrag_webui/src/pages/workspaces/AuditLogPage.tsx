import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { RotateCcwIcon, ShieldCheckIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { resolveWorkspaceId } from '@/app/routeHelpers'
import {
  listAuditEvents,
  type AuditEventResponse,
  type AuditListFilters,
  type AuditOutcome,
} from '@/api/lightrag'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleDescriptionKeys,
  summarizeRoleCapabilityKeys,
} from '@/lib/permissions'
import { errorMessage } from '@/lib/utils'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'

const OUTCOMES: AuditOutcome[] = ['success', 'denied', 'error']

const outcomeBadgeClass = (outcome: AuditOutcome): string => {
  if (outcome === 'denied') {
    return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  }
  if (outcome === 'error') {
    return 'border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-300'
  }
  return 'border-emerald-500/30 bg-emerald-500/[0.08] text-emerald-700 dark:text-emerald-300'
}

/**
 * Audit log page (PR-AUDIT-3).
 *
 * Simple MVP: filter bar (actor / action / outcome / time range) + a
 * compact table of events. Row click expands an inline detail panel;
 * no external drawer component needed. Export / retention delete
 * actions land in follow-ups.
 */
export default function AuditLogPage() {
  const { t } = useTranslation()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const canView = hasPermission(effectiveRole, 'audit:view_workspace')
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)

  const [events, setEvents] = useState<AuditEventResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  // Filter form (staged) vs active filters (committed with Apply).
  const [draftFilters, setDraftFilters] = useState({
    actor_user_id: '',
    action: '',
    outcome: '' as '' | AuditOutcome,
    since: '',
    until: '',
  })
  const [activeFilters, setActiveFilters] = useState<AuditListFilters>({ limit: 100 })

  const load = useCallback(async () => {
    if (!canView) return
    try {
      setLoading(true)
      setLoadError(null)
      const response = await listAuditEvents(currentWorkspaceId, activeFilters)
      setEvents(response.events)
    } catch (err) {
      setLoadError(errorMessage(err))
      setEvents([])
    } finally {
      setLoading(false)
    }
  }, [canView, currentWorkspaceId, activeFilters])

  useEffect(() => {
    void load()
  }, [load])

  const onApply = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const next: AuditListFilters = { limit: 100 }
    if (draftFilters.actor_user_id.trim()) next.actor_user_id = draftFilters.actor_user_id.trim()
    if (draftFilters.action.trim()) next.action = draftFilters.action.trim()
    if (draftFilters.outcome) next.outcome = draftFilters.outcome
    if (draftFilters.since.trim()) next.since = draftFilters.since.trim()
    if (draftFilters.until.trim()) next.until = draftFilters.until.trim()
    setActiveFilters(next)
  }

  const onReset = () => {
    setDraftFilters({
      actor_user_id: '',
      action: '',
      outcome: '',
      since: '',
      until: '',
    })
    setActiveFilters({ limit: 100 })
  }

  const onRefresh = async () => {
    try {
      await load()
      toast.success(
        t('platformShell.audit.refreshed', { defaultValue: '审计日志已刷新' })
      )
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const visibleEvents = useMemo(() => events, [events])

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.audit.badge', { defaultValue: 'AUDIT' })}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.audit.title', { defaultValue: '审计日志' })}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.audit.description', {
              defaultValue:
                '查看本工作区发生的创建、修改、删除与权限拒绝事件。支持按执行者、动作、结果和时间范围过滤。',
            })}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
          <RotateCcwIcon className={loading ? 'size-4 animate-spin' : 'size-4'} />
          {t('common.refresh', { defaultValue: '刷新' })}
        </Button>
      </header>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.audit.scope', { defaultValue: '工作区审计' })}
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

      <div className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 px-6 py-6">
          {!canView && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.audit.forbiddenTitle', {
                  defaultValue: '无权查看审计日志',
                })}
              </AlertTitle>
              <AlertDescription className="text-xs">
                {t('platformShell.audit.forbiddenHint', {
                  defaultValue:
                    '需要 admin 或 owner 角色才能访问此工作区的审计日志。',
                })}
              </AlertDescription>
            </Alert>
          )}

          {canView && (
            <>
              <form
                onSubmit={onApply}
                className="flex flex-wrap items-end gap-3 rounded-xl border border-border/60 bg-background/70 px-4 py-3"
              >
                <FilterInput
                  label={t('platformShell.audit.filter.actor', { defaultValue: '执行者 user id' })}
                  value={draftFilters.actor_user_id}
                  onChange={(v) => setDraftFilters((p) => ({ ...p, actor_user_id: v }))}
                />
                <FilterInput
                  label={t('platformShell.audit.filter.action', { defaultValue: '动作' })}
                  value={draftFilters.action}
                  onChange={(v) => setDraftFilters((p) => ({ ...p, action: v }))}
                  placeholder="workspace:update"
                />
                <div className="flex min-w-[140px] flex-col gap-1">
                  <label className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.audit.filter.outcome', { defaultValue: '结果' })}
                  </label>
                  <select
                    value={draftFilters.outcome}
                    onChange={(e) =>
                      setDraftFilters((p) => ({
                        ...p,
                        outcome: (e.target.value || '') as '' | AuditOutcome,
                      }))
                    }
                    className="h-9 rounded-md border border-border/70 bg-background px-2 text-sm"
                  >
                    <option value="">{t('platformShell.audit.filter.any', { defaultValue: '全部' })}</option>
                    {OUTCOMES.map((o) => (
                      <option key={o} value={o}>{o}</option>
                    ))}
                  </select>
                </div>
                <FilterInput
                  label={t('platformShell.audit.filter.since', { defaultValue: '起始 (ISO)' })}
                  value={draftFilters.since}
                  onChange={(v) => setDraftFilters((p) => ({ ...p, since: v }))}
                  placeholder="2026-04-01T00:00:00Z"
                />
                <FilterInput
                  label={t('platformShell.audit.filter.until', { defaultValue: '结束 (ISO)' })}
                  value={draftFilters.until}
                  onChange={(v) => setDraftFilters((p) => ({ ...p, until: v }))}
                  placeholder="2026-04-17T23:59:59Z"
                />
                <div className="flex items-center gap-2">
                  <Button type="submit" size="sm">
                    {t('common.apply', { defaultValue: '应用' })}
                  </Button>
                  <Button type="button" size="sm" variant="ghost" onClick={onReset}>
                    {t('common.reset', { defaultValue: '重置' })}
                  </Button>
                </div>
              </form>

              {loadError && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>
                    {t('platformShell.audit.loadError', {
                      defaultValue: '审计日志加载失败',
                    })}
                  </AlertTitle>
                  <AlertDescription className="text-xs">
                    {t('platformShell.audit.loadErrorHint', {
                      defaultValue: '后端尚未启用审计表或暂时不可达，请稍后重试。',
                    })}
                  </AlertDescription>
                </Alert>
              )}

              {loading ? (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                  {t('platformShell.common.loading', { defaultValue: '加载中…' })}
                </div>
              ) : visibleEvents.length === 0 ? (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                  {t('platformShell.audit.empty', {
                    defaultValue: '没有匹配的审计事件。',
                  })}
                </div>
              ) : (
                <div className="overflow-hidden rounded-xl border border-border/60 bg-background/70">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/60 bg-muted/30 text-left text-[11px] uppercase tracking-[0.1em] text-muted-foreground">
                        <th className="px-3 py-2 font-medium">
                          {t('platformShell.audit.col.time', { defaultValue: '时间' })}
                        </th>
                        <th className="px-3 py-2 font-medium">
                          {t('platformShell.audit.col.actor', { defaultValue: '执行者' })}
                        </th>
                        <th className="px-3 py-2 font-medium">
                          {t('platformShell.audit.col.action', { defaultValue: '动作' })}
                        </th>
                        <th className="px-3 py-2 font-medium">
                          {t('platformShell.audit.col.resource', { defaultValue: '资源' })}
                        </th>
                        <th className="px-3 py-2 font-medium">
                          {t('platformShell.audit.col.outcome', { defaultValue: '结果' })}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleEvents.map((event) => (
                        <AuditRow
                          key={event.id}
                          event={event}
                          expanded={expandedId === event.id}
                          onToggle={() =>
                            setExpandedId((cur) => (cur === event.id ? null : event.id))
                          }
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function FilterInput({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  return (
    <div className="flex min-w-[160px] flex-col gap-1">
      <label className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </label>
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="h-9"
      />
    </div>
  )
}

function AuditRow({
  event,
  expanded,
  onToggle,
}: {
  event: AuditEventResponse
  expanded: boolean
  onToggle: () => void
}) {
  const actorLabel =
    event.actor.username || event.actor.user_id || 'unknown'
  const timestamp = event.occurred_at

  return (
    <>
      <tr
        onClick={onToggle}
        className="cursor-pointer border-b border-border/40 text-foreground hover:bg-muted/40"
      >
        <td className="px-3 py-2 font-mono text-[11px] text-muted-foreground">
          {timestamp}
        </td>
        <td className="px-3 py-2">
          <div className="flex flex-col leading-tight">
            <span className="truncate text-[13px] font-medium">{actorLabel}</span>
            {event.actor.role && (
              <span className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                {event.actor.role}
              </span>
            )}
          </div>
        </td>
        <td className="px-3 py-2 font-mono text-[12px]">{event.action}</td>
        <td className="px-3 py-2">
          <div className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-[12px]">{event.resource.type}</span>
            {event.resource.id && (
              <span className="truncate text-[11px] text-muted-foreground">
                {event.resource.id}
              </span>
            )}
          </div>
        </td>
        <td className="px-3 py-2">
          <Badge
            variant="outline"
            className={`rounded-full px-2 py-0.5 text-[10px] uppercase ${outcomeBadgeClass(event.outcome)}`}
          >
            {event.outcome}
          </Badge>
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-border/40 bg-muted/20">
          <td colSpan={5} className="px-3 py-3">
            <div className="grid gap-3 text-[12px] leading-5 md:grid-cols-2">
              <DetailRow label="HTTP" value={`${event.http.method ?? '—'} ${event.http.path ?? ''} ${event.http.status ?? ''}`} />
              <DetailRow label="Client" value={event.client.ip ? `${event.client.ip} · ${event.client.user_agent ?? ''}` : '—'} />
              <DetailRow label="Workspace" value={event.workspace_id ?? '—'} />
              <DetailRow label="Knowledge base" value={event.kb_id ?? '—'} />
              <div className="md:col-span-2">
                <p className="mb-1 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  Metadata
                </p>
                <pre className="max-h-48 overflow-auto rounded-md border border-border/60 bg-background/80 p-2 text-[11px] leading-5">
                  {event.metadata
                    ? JSON.stringify(event.metadata, null, 2)
                    : '(none)'}
                </pre>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="mb-0.5 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </p>
      <p className="font-mono text-[12px] text-foreground">{value}</p>
    </div>
  )
}
