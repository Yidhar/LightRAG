import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ShieldCheckIcon, UsersIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { appRoutes } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleDescriptionKeys,
  summarizeRoleCapabilityKeys,
} from '@/lib/permissions'
import {
  getWorkspace,
  updateWorkspace,
  type WorkspaceRecord,
} from '@/api/lightrag'
import { errorMessage } from '@/lib/utils'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import Textarea from '@/components/ui/Textarea'

/**
 * Workspace settings — Phase W1c.
 *
 * Previous version was placeholder cards ("What lands here next"). This
 * rewrite wires the page to the real PATCH ``/workspaces/{id}`` endpoint
 * so owners/admins can rename and re-describe a workspace. Members who
 * lack the ``workspace:update`` permission see a read-only summary.
 */
export default function WorkspaceSettingsPage() {
  const { t } = useTranslation()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const canUpdate = hasPermission(effectiveRole, 'workspace:update')
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)

  const [workspace, setWorkspace] = useState<WorkspaceRecord | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const loadWorkspace = useCallback(async () => {
    try {
      setLoading(true)
      setLoadError(null)
      const record = await getWorkspace(currentWorkspaceId)
      setWorkspace(record)
      setName(record.name)
      setDescription(record.description ?? '')
    } catch (err) {
      setLoadError(errorMessage(err))
      setWorkspace(null)
    } finally {
      setLoading(false)
    }
  }, [currentWorkspaceId])

  useEffect(() => {
    void loadWorkspace()
  }, [loadWorkspace])

  const nameChanged = workspace ? name.trim() !== workspace.name : false
  const descriptionChanged = workspace
    ? (description.trim() || null) !== (workspace.description ?? null)
    : false
  const dirty = nameChanged || descriptionChanged
  const canSave = canUpdate && dirty && !submitting && name.trim().length > 0

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSave) return
    try {
      setSubmitting(true)
      const payload: { name?: string; description?: string | null } = {}
      if (nameChanged) payload.name = name.trim()
      if (descriptionChanged) payload.description = description.trim() || null
      const updated = await updateWorkspace(currentWorkspaceId, payload)
      setWorkspace(updated)
      setName(updated.name)
      setDescription(updated.description ?? '')
      toast.success(
        t('platformShell.workspaceSettings.saveSuccess', {
          defaultValue: '工作区已更新',
        })
      )
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  const handleReset = () => {
    if (!workspace) return
    setName(workspace.name)
    setDescription(workspace.description ?? '')
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
              {t('platformShell.workspaceSettings.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.workspaceSettings.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.workspaceSettings.description')}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to={appRoutes.workspaceMembers(currentWorkspaceId)}>
            <UsersIcon className="size-4" />
            {t('platformShell.workspaceSettings.openMembers')}
          </Link>
        </Button>
      </header>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.workspaceSettings.workspaceScope')}
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
        <div className="mx-auto flex w-full max-w-2xl flex-col gap-5 px-6 py-6">
          {loadError && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.workspaceSettings.loadError', {
                  defaultValue: '无法读取工作区信息',
                })}
              </AlertTitle>
              <AlertDescription className="text-xs">
                {t('platformShell.workspaceSettings.loadErrorHint', {
                  defaultValue:
                    '后端尚未就绪，或未运行 migrate_workspaces_create_table 迁移。',
                })}
              </AlertDescription>
            </Alert>
          )}

          {!canUpdate && workspace && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.workspaceSettings.readOnlyTitle', {
                  defaultValue: '只读视图',
                })}
              </AlertTitle>
              <AlertDescription className="text-xs">
                {t('platformShell.workspaceSettings.readOnlyDescription', {
                  defaultValue: '当前角色没有修改工作区设置的权限。',
                })}
              </AlertDescription>
            </Alert>
          )}

          {loading ? (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
              {t('platformShell.common.loading', { defaultValue: '加载中…' })}
            </div>
          ) : workspace ? (
            <form
              onSubmit={handleSubmit}
              className="flex flex-col gap-4 rounded-xl border border-border/60 bg-background/70 p-5"
            >
              <div className="space-y-2">
                <label htmlFor="workspace-name" className="text-sm font-medium text-foreground">
                  {t('platformShell.workspaceSettings.form.nameLabel', { defaultValue: '名称' })}
                </label>
                <Input
                  id="workspace-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  disabled={!canUpdate || submitting}
                  required
                  maxLength={255}
                  placeholder={t('platformShell.workspaceSettings.form.namePlaceholder', {
                    defaultValue: '例如：Marketing Research',
                  })}
                />
              </div>

              <div className="space-y-2">
                <label
                  htmlFor="workspace-description"
                  className="text-sm font-medium text-foreground"
                >
                  {t('platformShell.workspaceSettings.form.descriptionLabel', {
                    defaultValue: '描述',
                  })}
                </label>
                <Textarea
                  id="workspace-description"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  disabled={!canUpdate || submitting}
                  rows={4}
                  maxLength={1024}
                  placeholder={t('platformShell.workspaceSettings.form.descriptionPlaceholder', {
                    defaultValue: '简要说明这个工作区的用途、成员范围或团队分工。',
                  })}
                />
                <p className="text-[11px] text-muted-foreground">
                  {t('platformShell.workspaceSettings.form.descriptionHint', {
                    defaultValue: '会显示在工作区目录和切换器中。最长 1024 字符。',
                  })}
                </p>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-4">
                <p className="text-[11px] text-muted-foreground">
                  {workspace.updated_at && (
                    <>
                      {t('platformShell.workspaceSettings.form.lastUpdated', {
                        defaultValue: '上次更新',
                      })}
                      {': '}
                      <span className="font-mono">{workspace.updated_at}</span>
                    </>
                  )}
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={handleReset}
                    disabled={!dirty || submitting}
                  >
                    {t('common.reset', { defaultValue: '还原' })}
                  </Button>
                  <Button type="submit" size="sm" disabled={!canSave}>
                    {submitting
                      ? t('platformShell.common.saving', { defaultValue: '保存中…' })
                      : t('platformShell.workspaceSettings.form.save', { defaultValue: '保存' })}
                  </Button>
                </div>
              </div>
            </form>
          ) : null}
        </div>
      </div>
    </div>
  )
}
