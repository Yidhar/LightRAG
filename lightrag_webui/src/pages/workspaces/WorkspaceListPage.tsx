import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  BookOpenTextIcon,
  ChevronRightIcon,
  FolderKanbanIcon,
  LayoutGridIcon,
  PlusIcon,
  UsersIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { appRoutes, defaultKnowledgeBaseId, defaultWorkspaceId } from '@/app/routes'
import {
  createKnowledgeBase,
  createWorkspace,
  deleteKnowledgeBase,
  deleteWorkspace,
  listKnowledgeBases,
  listWorkspaces,
  type KnowledgeBaseCreateRequest,
  type KnowledgeBaseRecord,
  type WorkspaceRecord,
  updateKnowledgeBase,
} from '@/api/lightrag'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  summarizeRoleCapabilityKeys,
} from '@/lib/permissions'
import { errorMessage } from '@/lib/utils'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/AlertDialog'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import Input from '@/components/ui/Input'
import Textarea from '@/components/ui/Textarea'
import AccessBadge from '@/components/navigation/AccessBadge'

const defaultConfigOverrideText = '{\n  \n}'

interface KnowledgeBaseFormDialogProps {
  open: boolean
  mode: 'create' | 'edit'
  knowledgeBase: KnowledgeBaseRecord | null
  submitting: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (payload: KnowledgeBaseCreateRequest) => Promise<void>
}

interface KnowledgeBaseFormDialogContentProps {
  mode: 'create' | 'edit'
  knowledgeBase: KnowledgeBaseRecord | null
  submitting: boolean
  onSubmit: (payload: KnowledgeBaseCreateRequest) => Promise<void>
  onCancel: () => void
}

function KnowledgeBaseFormDialogContent({
  mode,
  knowledgeBase,
  submitting,
  onSubmit,
  onCancel,
}: KnowledgeBaseFormDialogContentProps) {
  const { t } = useTranslation()
  const [kbId, setKbId] = useState(mode === 'edit' ? knowledgeBase?.id || '' : '')
  const [name, setName] = useState(mode === 'edit' ? knowledgeBase?.name || '' : '')
  const [description, setDescription] = useState(mode === 'edit' ? knowledgeBase?.description || '' : '')
  const [status, setStatus] = useState(mode === 'edit' ? knowledgeBase?.status || 'active' : 'active')
  const [configOverrideText, setConfigOverrideText] = useState(
    mode === 'edit'
      ? JSON.stringify(knowledgeBase?.config_override || {}, null, 2) || defaultConfigOverrideText
      : defaultConfigOverrideText
  )

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    let configOverride: Record<string, any> = {}
    const trimmedConfig = configOverrideText.trim()

    if (trimmedConfig) {
      try {
        const parsed = JSON.parse(trimmedConfig)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          toast.error(t('platformShell.workspaceDirectory.configOverrideObject'))
          return
        }
        configOverride = parsed as Record<string, any>
      } catch (parseError) {
        toast.error(t('platformShell.workspaceDirectory.invalidJsonConfigOverride', { error: errorMessage(parseError) }))
        return
      }
    }

    await onSubmit({
      kb_id: kbId.trim() || undefined,
      name: name.trim() || undefined,
      description: description.trim(),
      status: status.trim() || 'active',
      config_override: configOverride,
    })
  }

  return (
    <DialogContent className="rounded-[1.5rem] border-border/70 sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>
          {mode === 'create'
            ? t('platformShell.workspaceDirectory.dialog.createTitle')
            : t('platformShell.workspaceDirectory.dialog.editTitle')}
        </DialogTitle>
        <DialogDescription>
          {mode === 'create'
            ? t('platformShell.workspaceDirectory.dialog.createDescription')
            : t('platformShell.workspaceDirectory.dialog.editDescription')}
        </DialogDescription>
      </DialogHeader>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <label htmlFor="kb-id" className="text-sm font-medium text-foreground">
              {t('platformShell.workspaceDirectory.kbId')}
            </label>
            <Input
              id="kb-id"
              value={kbId}
              onChange={(event) => setKbId(event.target.value)}
              placeholder={t('platformShell.workspaceDirectory.kbIdPlaceholder')}
              disabled={mode === 'edit' || submitting}
              className="h-11 rounded-xl border-border/70"
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="kb-name" className="text-sm font-medium text-foreground">
              {t('platformShell.workspaceDirectory.displayName')}
            </label>
            <Input
              id="kb-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t('platformShell.workspaceDirectory.displayNamePlaceholder')}
              disabled={submitting}
              className="h-11 rounded-xl border-border/70"
            />
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-[1fr_180px]">
          <div className="space-y-2">
            <label htmlFor="kb-description" className="text-sm font-medium text-foreground">
              {t('platformShell.workspaceDirectory.descriptionLabel')}
            </label>
            <Textarea
              id="kb-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={t('platformShell.workspaceDirectory.descriptionPlaceholder')}
              disabled={submitting}
              className="min-h-[88px] rounded-xl border-border/70"
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="kb-status" className="text-sm font-medium text-foreground">
              {t('platformShell.common.status')}
            </label>
            <Input
              id="kb-status"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              placeholder={t('platformShell.workspaceDirectory.statusPlaceholder')}
              disabled={submitting}
              className="h-11 rounded-xl border-border/70"
            />
            <p className="text-xs leading-5 text-muted-foreground">
              {t('platformShell.workspaceDirectory.statusHint')}
            </p>
          </div>
        </div>

        <div className="space-y-2">
          <label htmlFor="kb-config-override" className="text-sm font-medium text-foreground">
            {t('platformShell.workspaceDirectory.configOverride')}
          </label>
          <Textarea
            id="kb-config-override"
            value={configOverrideText}
            onChange={(event) => setConfigOverrideText(event.target.value)}
            spellCheck={false}
            disabled={submitting}
            className="min-h-[148px] rounded-xl border-border/70 font-mono text-xs leading-6"
          />
        </div>

        <DialogFooter className="gap-2 sm:gap-2">
          <Button type="button" variant="outline" onClick={onCancel} disabled={submitting}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={submitting}>
            {submitting
              ? t('common.saving')
              : mode === 'create'
                ? t('platformShell.workspaceDirectory.createKb')
                : t('platformShell.workspaceDirectory.saveChanges')}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  )
}

function KnowledgeBaseFormDialog({
  open,
  mode,
  knowledgeBase,
  submitting,
  onOpenChange,
  onSubmit,
}: KnowledgeBaseFormDialogProps) {
  const formKey = `${mode}-${knowledgeBase?.id ?? 'new'}`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <KnowledgeBaseFormDialogContent
          key={formKey}
          mode={mode}
          knowledgeBase={knowledgeBase}
          submitting={submitting}
          onSubmit={onSubmit}
          onCancel={() => onOpenChange(false)}
        />
      ) : null}
    </Dialog>
  )
}

export default function WorkspaceListPage() {
  const { t } = useTranslation()
  const { role, memberships } = useAuthStore()
  const workspaceIds = useMemo(() => {
    const discovered = Array.from(
      new Set([defaultWorkspaceId, ...memberships.map((membership) => membership.workspace_id)])
    )
    return discovered.sort()
  }, [memberships])
  const currentWorkspaceId = workspaceIds[0] || defaultWorkspaceId
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: defaultKnowledgeBaseId }
  )
  const canManageKnowledgeBases = hasPermission(effectiveRole, 'workspace:update')

  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kbDialogOpen, setKbDialogOpen] = useState(false)
  const [kbDialogMode, setKbDialogMode] = useState<'create' | 'edit'>('create')
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState<KnowledgeBaseRecord | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeBaseRecord | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Workspace-level state (Phase W1b wires this to /workspaces API).
  const [workspaces, setWorkspaces] = useState<WorkspaceRecord[]>([])
  const [workspacesLoading, setWorkspacesLoading] = useState(true)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false)
  const [workspaceForm, setWorkspaceForm] = useState<{ id: string; name: string; description: string }>(
    { id: '', name: '', description: '' }
  )
  const [workspaceDeleteTarget, setWorkspaceDeleteTarget] = useState<WorkspaceRecord | null>(null)
  const [workspaceSubmitting, setWorkspaceSubmitting] = useState(false)

  const loadKnowledgeBases = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await listKnowledgeBases(currentWorkspaceId)
      setKnowledgeBases(response.items)
    } catch (loadError) {
      setError(errorMessage(loadError))
      setKnowledgeBases([])
    } finally {
      setLoading(false)
    }
  }, [currentWorkspaceId])

  useEffect(() => {
    void loadKnowledgeBases()
  }, [loadKnowledgeBases])

  const loadWorkspaces = useCallback(async () => {
    try {
      setWorkspacesLoading(true)
      setWorkspaceError(null)
      const response = await listWorkspaces()
      setWorkspaces(response.items)
    } catch (loadError) {
      setWorkspaceError(errorMessage(loadError))
      setWorkspaces([])
    } finally {
      setWorkspacesLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadWorkspaces()
  }, [loadWorkspaces])

  const openWorkspaceCreateDialog = () => {
    setWorkspaceForm({ id: '', name: '', description: '' })
    setWorkspaceDialogOpen(true)
  }

  const handleWorkspaceSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmedName = workspaceForm.name.trim()
    if (!trimmedName) {
      toast.error(t('platformShell.workspaceDirectory.workspaceForm.nameRequired', { defaultValue: '请填写工作区名称' }))
      return
    }
    try {
      setWorkspaceSubmitting(true)
      const created = await createWorkspace({
        id: workspaceForm.id.trim() || undefined,
        name: trimmedName,
        description: workspaceForm.description.trim() || null,
      })
      toast.success(
        t('platformShell.workspaceDirectory.workspaceForm.createSuccess', {
          defaultValue: 'Workspace "{{name}}" created',
          name: created.name,
        })
      )
      setWorkspaceDialogOpen(false)
      await loadWorkspaces()
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setWorkspaceSubmitting(false)
    }
  }

  const handleDeleteWorkspace = async () => {
    if (!workspaceDeleteTarget) return
    try {
      setWorkspaceSubmitting(true)
      await deleteWorkspace(workspaceDeleteTarget.id)
      toast.success(
        t('platformShell.workspaceDirectory.workspaceForm.deleteSuccess', {
          defaultValue: 'Workspace "{{name}}" deleted',
          name: workspaceDeleteTarget.name,
        })
      )
      setWorkspaceDeleteTarget(null)
      await loadWorkspaces()
    } catch (deleteError) {
      toast.error(errorMessage(deleteError))
    } finally {
      setWorkspaceSubmitting(false)
    }
  }

  const workspaceLinks = [
    {
      label: t('platformShell.common.documents'),
      to: appRoutes.kbDocuments(currentWorkspaceId, defaultKnowledgeBaseId),
      description: t('platformShell.documents.description'),
      available: true,
      icon: BookOpenTextIcon,
    },
    {
      label: t('platformShell.common.members'),
      to: appRoutes.workspaceMembers(currentWorkspaceId),
      description: t('platformShell.workspaceMembers.description'),
      available: hasPermission(effectiveRole, 'workspace:invite_member'),
      icon: UsersIcon,
    },
    {
      label: t('platformShell.common.workspaceSettings'),
      to: appRoutes.workspaceSettings(currentWorkspaceId),
      description: t('platformShell.workspaceSettings.description'),
      available: hasPermission(effectiveRole, 'workspace:update'),
      icon: FolderKanbanIcon,
    },
  ].filter((item) => item.available)

  const surfacedKnowledgeBaseCount = loading
    ? t('platformShell.common.loadingCount')
    : Math.max(knowledgeBases.length, 1)

  const openCreateDialog = () => {
    setKbDialogMode('create')
    setSelectedKnowledgeBase(null)
    setKbDialogOpen(true)
  }

  const openEditDialog = (knowledgeBase: KnowledgeBaseRecord) => {
    setKbDialogMode('edit')
    setSelectedKnowledgeBase(knowledgeBase)
    setKbDialogOpen(true)
  }

  const handleKnowledgeBaseSubmit = async (payload: KnowledgeBaseCreateRequest) => {
    try {
      setSubmitting(true)
      if (kbDialogMode === 'create') {
        const response = await createKnowledgeBase(currentWorkspaceId, payload)
        toast.success(response.message)
      } else if (selectedKnowledgeBase) {
        const response = await updateKnowledgeBase(currentWorkspaceId, selectedKnowledgeBase.id, {
          name: payload.name,
          description: payload.description,
          status: payload.status,
          config_override: payload.config_override,
        })
        toast.success(response.message)
      }

      setKbDialogOpen(false)
      setSelectedKnowledgeBase(null)
      await loadKnowledgeBases()
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setSubmitting(false)
    }
  }

  const handleDeleteKnowledgeBase = async () => {
    if (!deleteTarget) {
      return
    }

    try {
      setSubmitting(true)
      const response = await deleteKnowledgeBase(currentWorkspaceId, deleteTarget.id)
      toast.success(response.message)
      setDeleteTarget(null)
      await loadKnowledgeBases()
    } catch (deleteError) {
      toast.error(errorMessage(deleteError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6 px-4 py-6 sm:px-6">
        <section className="surface-panel overflow-hidden rounded-[32px] border border-border/70 bg-gradient-to-br from-emerald-500/10 via-card to-card">
          <div className="px-6 py-7 lg:px-8">
            <div className="space-y-6">
              <div className="space-y-5 rounded-[28px] border border-border/70 bg-background/80 p-6 shadow-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                    {t('platformShell.workspaceDirectory.badge')}
                  </Badge>
                  <AccessBadge role={effectiveRole} />
                </div>

                <div className="space-y-2">
                  <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                    {t('platformShell.workspaceDirectory.title')}
                  </h1>
                  <p className="max-w-4xl text-sm leading-7 text-muted-foreground sm:text-[15px]">
                    {t('platformShell.workspaceDirectory.description')}
                  </p>
                </div>

                <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(280px,0.75fr)]">
                  <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                    <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      {t('platformShell.workspaceDirectory.activeWorkspace')}
                    </p>
                    <h2 className="mt-2 text-3xl font-semibold tracking-tight text-foreground">
                      {currentWorkspaceId}
                    </h2>
                    <div className="mt-5 flex flex-wrap gap-2">
                      {summarizeRoleCapabilityKeys(effectiveRole).map((capabilityKey) => (
                        <Badge key={capabilityKey} variant="outline" className="rounded-full bg-background/80 px-3 py-1">
                          {t(capabilityKey)}
                        </Badge>
                      ))}
                    </div>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                    <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                        {t('platformShell.workspaceDirectory.accessibleWorkspaces')}
                      </p>
                      <p className="mt-2 text-2xl font-semibold text-foreground">{workspaceIds.length}</p>
                    </div>
                    <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                        {t('platformShell.workspaceDirectory.knowledgeBasesSurfaced')}
                      </p>
                      <p className="mt-2 text-2xl font-semibold text-foreground">{surfacedKnowledgeBaseCount}</p>
                    </div>
                  </div>
                </div>

                {canManageKnowledgeBases && (
                  <div className="flex flex-wrap gap-3">
                    <Button onClick={openCreateDialog}>
                      <PlusIcon className="size-4" />
                      {t('platformShell.workspaceDirectory.createKb')}
                    </Button>
                  </div>
                )}
              </div>

              {workspaceLinks.length > 0 && (
                <div className="grid gap-4 lg:grid-cols-3">
                  {workspaceLinks.map((item) => {
                    const Icon = item.icon
                    return (
                      <Link
                        key={item.label}
                        to={item.to}
                        className="group motion-standard flex min-h-[176px] flex-col justify-between rounded-[28px] border border-border/70 bg-background/80 p-6 shadow-sm hover:-translate-y-0.5 hover:border-emerald-500/30 hover:bg-emerald-50/35 dark:hover:bg-emerald-500/5"
                      >
                        <div className="space-y-4">
                          <span className="inline-flex size-12 items-center justify-center rounded-2xl border border-border/70 bg-muted/30 text-emerald-600 dark:text-emerald-400">
                            <Icon className="size-5" />
                          </span>
                          <div className="space-y-2">
                            <p className="text-lg font-semibold text-foreground">{item.label}</p>
                            <p className="text-sm leading-6 text-muted-foreground">{item.description}</p>
                          </div>
                        </div>

                        <div className="mt-5 flex items-center gap-2 text-sm font-medium text-foreground">
                          <span>{t('platformShell.common.open')}</span>
                          <ChevronRightIcon className="size-4 transition-transform group-hover:translate-x-0.5" />
                        </div>
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader className="gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="space-y-2">
                <CardTitle className="flex items-center gap-2">
                  <FolderKanbanIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  {t('platformShell.workspaceDirectory.workspacesDirectory', {
                    defaultValue: 'Workspaces',
                  })}
                </CardTitle>
                <CardDescription>
                  {t('platformShell.workspaceDirectory.workspacesDirectoryDescription', {
                    defaultValue: 'Create, rename, or delete workspaces. Each workspace groups its own knowledge bases and members.',
                  })}
                </CardDescription>
              </div>
              <Button onClick={openWorkspaceCreateDialog}>
                <PlusIcon className="size-4" />
                {t('platformShell.workspaceDirectory.createWorkspace', { defaultValue: '新建工作区' })}
              </Button>
            </CardHeader>
            <CardContent className="space-y-3">
              {workspaceError && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>
                    {t('platformShell.workspaceDirectory.compatibilityMode')}
                  </AlertTitle>
                  <AlertDescription>{workspaceError}</AlertDescription>
                </Alert>
              )}

              {workspacesLoading ? (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.workspaceDirectory.loadingWorkspaces', { defaultValue: '加载工作区…' })}
                </div>
              ) : workspaces.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.workspaceDirectory.emptyWorkspaces', {
                    defaultValue: '还没有工作区 — 点击上方按钮创建第一个。',
                  })}
                </div>
              ) : (
                <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-3">
                  {workspaces.map((workspace) => (
                    <div
                      key={workspace.id}
                      className="flex flex-col gap-3 rounded-xl border border-border/70 bg-background/80 px-4 py-3"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-semibold text-foreground">
                            {workspace.name}
                          </p>
                          <p className="mt-0.5 text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
                            {workspace.id}
                          </p>
                        </div>
                        {workspace.id === defaultWorkspaceId && (
                          <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[10px]">
                            {t('platformShell.workspaceDirectory.defaultBadge')}
                          </Badge>
                        )}
                      </div>
                      {workspace.description && (
                        <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">
                          {workspace.description}
                        </p>
                      )}
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" variant="outline" asChild>
                          <Link to={appRoutes.kbOverview(workspace.id, defaultKnowledgeBaseId)}>
                            {t('platformShell.common.open', { defaultValue: '打开' })}
                          </Link>
                        </Button>
                        {workspace.id !== defaultWorkspaceId && (
                          <Button
                            variant="outline"
                            size="sm"
                            className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
                            onClick={() => setWorkspaceDeleteTarget(workspace)}
                          >
                            {t('platformShell.common.delete')}
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader className="gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="space-y-2">
                <CardTitle className="flex items-center gap-2">
                  <LayoutGridIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  {t('platformShell.workspaceDirectory.knowledgeBaseDirectory')}
                </CardTitle>
                <CardDescription>
                  {t('platformShell.workspaceDirectory.knowledgeBaseDirectoryDescription')}
                </CardDescription>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {error && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.workspaceDirectory.compatibilityMode')}</AlertTitle>
                  <AlertDescription>{t('platformShell.workspaceDirectory.compatibilityDescription')}</AlertDescription>
                </Alert>
              )}

              {!canManageKnowledgeBases && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.workspaceDirectory.readOnlyTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.workspaceDirectory.readOnlyDescription')}</AlertDescription>
                </Alert>
              )}

              {loading ? (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.workspaceDirectory.loadingKnowledgeBases')}
                </div>
              ) : knowledgeBases.length > 0 ? (
                <div className="grid gap-4 xl:grid-cols-2 2xl:grid-cols-3">
                  {knowledgeBases.map((kb) => (
                    <div
                      key={kb.id}
                      className="motion-standard flex min-h-[220px] flex-col rounded-[28px] border border-border/70 bg-background/80 p-6 hover:-translate-y-0.5 hover:border-emerald-500/30 hover:bg-emerald-50/35 dark:hover:bg-emerald-500/5"
                    >
                      <div className="flex items-start justify-between gap-4">
                        <div className="min-w-0">
                          <div className="flex items-center gap-3">
                            <span className="inline-flex size-12 shrink-0 items-center justify-center rounded-2xl border border-border/70 bg-muted/30 text-emerald-600 dark:text-emerald-400">
                              <BookOpenTextIcon className="size-5" />
                            </span>
                            <div className="min-w-0">
                              <p className="truncate text-lg font-semibold text-foreground">{kb.name || kb.id}</p>
                              <p className="mt-1 text-xs uppercase tracking-[0.12em] text-muted-foreground">{kb.id}</p>
                            </div>
                          </div>
                        </div>
                        <div className="flex flex-wrap items-center justify-end gap-2">
                          {kb.id === defaultKnowledgeBaseId && (
                            <Badge variant="outline" className="rounded-full px-2.5 py-0.5 text-[11px]">
                              {t('platformShell.workspaceDirectory.defaultBadge')}
                            </Badge>
                          )}
                          {kb.status && kb.status !== 'active' && (
                            <Badge variant="outline" className="rounded-full px-2.5 py-0.5 text-[11px]">
                              {kb.status}
                            </Badge>
                          )}
                        </div>
                      </div>

                      <p className="mt-4 flex-1 text-sm leading-6 text-muted-foreground">
                        {kb.description || t('platformShell.workspaceDirectory.noDescription')}
                      </p>

                      <div className="mt-6 flex flex-wrap gap-2">
                        <Button size="sm" asChild>
                          <Link to={appRoutes.kbDocuments(kb.workspace_id, kb.id)}>{t('platformShell.common.documents')}</Link>
                        </Button>
                        {canManageKnowledgeBases && (
                          <>
                            <Button variant="outline" size="sm" onClick={() => openEditDialog(kb)}>
                              {t('platformShell.common.edit')}
                            </Button>
                            {kb.id !== defaultKnowledgeBaseId && (
                              <Button
                                variant="outline"
                                size="sm"
                                className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
                                onClick={() => setDeleteTarget(kb)}
                              >
                                {t('platformShell.common.delete')}
                              </Button>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.workspaceDirectory.empty')}
                </div>
              )}
            </CardContent>
          </Card>
        </section>
      </div>

      <KnowledgeBaseFormDialog
        open={kbDialogOpen}
        mode={kbDialogMode}
        knowledgeBase={selectedKnowledgeBase}
        submitting={submitting}
        onOpenChange={setKbDialogOpen}
        onSubmit={handleKnowledgeBaseSubmit}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent className="rounded-[1.5rem] border-border/70">
          <AlertDialogHeader>
            <AlertDialogTitle>{t('platformShell.workspaceDirectory.deleteDialog.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTarget
                ? t('platformShell.workspaceDirectory.deleteDialog.descriptionWithName', {
                  knowledgeBaseName: deleteTarget.name || deleteTarget.id,
                  workspaceId: currentWorkspaceId,
                })
                : t('platformShell.workspaceDirectory.deleteDialog.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submitting}>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteKnowledgeBase}
              disabled={submitting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {submitting ? t('platformShell.common.deleting') : t('platformShell.workspaceDirectory.deleteKb')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Workspace create dialog (Phase W1b) */}
      <Dialog open={workspaceDialogOpen} onOpenChange={setWorkspaceDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {t('platformShell.workspaceDirectory.workspaceForm.title', { defaultValue: '新建工作区' })}
            </DialogTitle>
            <DialogDescription>
              {t('platformShell.workspaceDirectory.workspaceForm.description', {
                defaultValue: '工作区分隔知识库与成员。创建后你会自动成为该工作区的 Owner。',
              })}
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={handleWorkspaceSubmit}>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('platformShell.workspaceDirectory.workspaceForm.nameLabel', { defaultValue: '名称' })}
              </label>
              <Input
                required
                value={workspaceForm.name}
                onChange={(event) =>
                  setWorkspaceForm((prev) => ({ ...prev, name: event.target.value }))
                }
                placeholder={t('platformShell.workspaceDirectory.workspaceForm.namePlaceholder', {
                  defaultValue: '例如：Marketing Research',
                })}
                disabled={workspaceSubmitting}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('platformShell.workspaceDirectory.workspaceForm.idLabel', { defaultValue: 'ID（可选）' })}
              </label>
              <Input
                value={workspaceForm.id}
                onChange={(event) =>
                  setWorkspaceForm((prev) => ({ ...prev, id: event.target.value }))
                }
                placeholder={t('platformShell.workspaceDirectory.workspaceForm.idPlaceholder', {
                  defaultValue: '留空则从名称生成',
                })}
                disabled={workspaceSubmitting}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('platformShell.workspaceDirectory.workspaceForm.descriptionLabel', { defaultValue: '描述' })}
              </label>
              <Textarea
                value={workspaceForm.description}
                onChange={(event) =>
                  setWorkspaceForm((prev) => ({ ...prev, description: event.target.value }))
                }
                rows={3}
                disabled={workspaceSubmitting}
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setWorkspaceDialogOpen(false)}
                disabled={workspaceSubmitting}
              >
                {t('common.cancel')}
              </Button>
              <Button type="submit" disabled={workspaceSubmitting}>
                {workspaceSubmitting
                  ? t('platformShell.common.saving', { defaultValue: '保存中…' })
                  : t('platformShell.workspaceDirectory.workspaceForm.submit', { defaultValue: '创建' })}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Workspace delete confirm (Phase W1b) */}
      <AlertDialog
        open={Boolean(workspaceDeleteTarget)}
        onOpenChange={(open) => !open && setWorkspaceDeleteTarget(null)}
      >
        <AlertDialogContent className="rounded-[1.5rem] border-border/70">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('platformShell.workspaceDirectory.workspaceForm.deleteTitle', {
                defaultValue: '删除工作区',
              })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {workspaceDeleteTarget
                ? t('platformShell.workspaceDirectory.workspaceForm.deleteConfirm', {
                  defaultValue:
                    '删除工作区 "{{name}}" 将一并移除其成员和知识库授权。此操作不可撤销。',
                  name: workspaceDeleteTarget.name,
                })
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={workspaceSubmitting}>
              {t('common.cancel')}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteWorkspace}
              disabled={workspaceSubmitting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {workspaceSubmitting
                ? t('platformShell.common.deleting')
                : t('platformShell.common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
