import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  BookOpenTextIcon,
  FolderKanbanIcon,
  LayoutGridIcon,
  PlusIcon,
  ShieldCheckIcon,
  Trash2Icon,
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
  roleDescriptionKeys,
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

const defaultConfigOverrideText = '{\n  \n}'

// ---------------------------------------------------------------------------
// Knowledge-base form dialog (unchanged behavior, kept as-is)
// ---------------------------------------------------------------------------

interface KnowledgeBaseFormDialogProps {
  open: boolean
  mode: 'create' | 'edit'
  knowledgeBase: KnowledgeBaseRecord | null
  submitting: boolean
  categorySuggestions: string[]
  onOpenChange: (open: boolean) => void
  onSubmit: (payload: KnowledgeBaseCreateRequest) => Promise<void>
}

function KnowledgeBaseFormDialogContent({
  mode,
  knowledgeBase,
  submitting,
  categorySuggestions,
  onSubmit,
  onCancel,
}: {
  mode: 'create' | 'edit'
  knowledgeBase: KnowledgeBaseRecord | null
  submitting: boolean
  categorySuggestions: string[]
  onSubmit: (payload: KnowledgeBaseCreateRequest) => Promise<void>
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [kbId, setKbId] = useState(mode === 'edit' ? knowledgeBase?.id || '' : '')
  const [name, setName] = useState(mode === 'edit' ? knowledgeBase?.name || '' : '')
  const [description, setDescription] = useState(
    mode === 'edit' ? knowledgeBase?.description || '' : ''
  )
  const [status, setStatus] = useState(
    mode === 'edit' ? knowledgeBase?.status || 'active' : 'active'
  )
  const [category, setCategory] = useState(
    mode === 'edit' ? knowledgeBase?.category || '' : ''
  )
  const [configOverrideText, setConfigOverrideText] = useState(
    mode === 'edit'
      ? JSON.stringify(knowledgeBase?.config_override || {}, null, 2) ||
          defaultConfigOverrideText
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
        toast.error(
          t('platformShell.workspaceDirectory.invalidJsonConfigOverride', {
            error: errorMessage(parseError),
          })
        )
        return
      }
    }
    await onSubmit({
      kb_id: kbId.trim() || undefined,
      name: name.trim() || undefined,
      description: description.trim(),
      status: status.trim() || 'active',
      config_override: configOverride,
      // Send trimmed category; backend treats "" as "uncategorised" which
      // is exactly what we want when the user leaves the field blank.
      category: category.trim(),
    })
  }

  return (
    <DialogContent className="rounded-2xl border-border/70 sm:max-w-xl">
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
              className="h-10 rounded-xl border-border/70"
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
              className="h-10 rounded-xl border-border/70"
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
              className="min-h-[80px] rounded-xl border-border/70"
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
              className="h-10 rounded-xl border-border/70"
            />
          </div>
        </div>

        <div className="space-y-2">
          <label htmlFor="kb-category" className="text-sm font-medium text-foreground">
            {t('platformShell.workspaceDirectory.categoryLabel', {
              defaultValue: '分类',
            })}
          </label>
          <Input
            id="kb-category"
            list="kb-category-suggestions"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            placeholder={t('platformShell.workspaceDirectory.categoryPlaceholder', {
              defaultValue: '例如：研究、行业、客户… 留空表示未分类',
            })}
            disabled={submitting}
            className="h-10 rounded-xl border-border/70"
            autoComplete="off"
          />
          {categorySuggestions.length > 0 && (
            <datalist id="kb-category-suggestions">
              {categorySuggestions.map((tag) => (
                <option key={tag} value={tag} />
              ))}
            </datalist>
          )}
          <p className="text-xs text-muted-foreground">
            {t('platformShell.workspaceDirectory.categoryHint', {
              defaultValue:
                '用于在工作区知识库列表按组展示；输入已有分类可复用，新分类会自动加入候选。',
            })}
          </p>
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
            className="min-h-[120px] rounded-xl border-border/70 font-mono text-xs leading-6"
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
  categorySuggestions,
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
          categorySuggestions={categorySuggestions}
          onSubmit={onSubmit}
          onCancel={() => onOpenChange(false)}
        />
      ) : null}
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function WorkspaceListPage() {
  const { t } = useTranslation()
  const { role, memberships } = useAuthStore()

  // Workspaces (API-backed) + derived current workspace for the KB panel.
  const [workspaces, setWorkspaces] = useState<WorkspaceRecord[]>([])
  const [workspacesLoading, setWorkspacesLoading] = useState(true)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false)
  const [workspaceForm, setWorkspaceForm] = useState<{ name: string; description: string }>(
    { name: '', description: '' }
  )
  const [workspaceDeleteTarget, setWorkspaceDeleteTarget] = useState<WorkspaceRecord | null>(null)
  const [workspaceSubmitting, setWorkspaceSubmitting] = useState(false)

  const fallbackWorkspaceIds = useMemo(
    () =>
      Array.from(
        new Set([defaultWorkspaceId, ...memberships.map((membership) => membership.workspace_id)])
      ).sort(),
    [memberships]
  )

  const currentWorkspaceId =
    workspaces[0]?.id || fallbackWorkspaceIds[0] || defaultWorkspaceId

  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: defaultKnowledgeBaseId }
  )
  const capabilitySummary = summarizeRoleCapabilityKeys(effectiveRole)
  const canManageKnowledgeBases = hasPermission(effectiveRole, 'workspace:update')

  // Knowledge bases for the current workspace.
  const UNCATEGORIZED_KEY = '__uncategorized__'
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRecord[]>([])
  const [kbLoading, setKbLoading] = useState(true)
  const [kbError, setKbError] = useState<string | null>(null)
  const [kbDialogOpen, setKbDialogOpen] = useState(false)
  const [kbDialogMode, setKbDialogMode] = useState<'create' | 'edit'>('create')
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState<KnowledgeBaseRecord | null>(null)
  const [kbDeleteTarget, setKbDeleteTarget] = useState<KnowledgeBaseRecord | null>(null)
  const [kbSubmitting, setKbSubmitting] = useState(false)

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

  const loadKnowledgeBases = useCallback(async () => {
    try {
      setKbLoading(true)
      setKbError(null)
      const response = await listKnowledgeBases(currentWorkspaceId)
      setKnowledgeBases(response.items)
    } catch (loadError) {
      setKbError(errorMessage(loadError))
      setKnowledgeBases([])
    } finally {
      setKbLoading(false)
    }
  }, [currentWorkspaceId])

  useEffect(() => {
    void loadWorkspaces()
  }, [loadWorkspaces])

  useEffect(() => {
    void loadKnowledgeBases()
  }, [loadKnowledgeBases])

  // Derived category buckets. We group in-memory off whatever the registry
  // returned rather than hitting /categories separately — that endpoint is
  // still useful elsewhere but here we already have every KB loaded.
  const categorySuggestions = useMemo(() => {
    const seen = new Set<string>()
    for (const kb of knowledgeBases) {
      const tag = (kb.category || '').trim()
      if (tag) seen.add(tag)
    }
    return Array.from(seen).sort((a, b) =>
      a.localeCompare(b, undefined, { sensitivity: 'base' })
    )
  }, [knowledgeBases])

  const knowledgeBaseGroups = useMemo(() => {
    const buckets = new Map<string, KnowledgeBaseRecord[]>()
    for (const kb of knowledgeBases) {
      const tag = (kb.category || '').trim()
      const key = tag || UNCATEGORIZED_KEY
      const existing = buckets.get(key)
      if (existing) {
        existing.push(kb)
      } else {
        buckets.set(key, [kb])
      }
    }

    // Sort named categories case-insensitively, push the "uncategorized"
    // bucket to the end so uncategorised KBs never obscure the grouping.
    const named = Array.from(buckets.entries())
      .filter(([key]) => key !== UNCATEGORIZED_KEY)
      .sort(([a], [b]) => a.localeCompare(b, undefined, { sensitivity: 'base' }))
    const uncategorized = buckets.get(UNCATEGORIZED_KEY)
    if (uncategorized && uncategorized.length > 0) {
      named.push([UNCATEGORIZED_KEY, uncategorized])
    }
    return named
  }, [knowledgeBases])

  // ---------- workspace handlers ----------

  const openWorkspaceCreateDialog = () => {
    setWorkspaceForm({ name: '', description: '' })
    setWorkspaceDialogOpen(true)
  }

  const handleWorkspaceSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmedName = workspaceForm.name.trim()
    if (!trimmedName) {
      toast.error(
        t('platformShell.workspaceDirectory.workspaceForm.nameRequired', {
          defaultValue: '请填写工作区名称',
        })
      )
      return
    }
    try {
      setWorkspaceSubmitting(true)
      // Backend auto-assigns an opaque uuid4 hex id when omitted.
      const created = await createWorkspace({
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

  const handleWorkspaceDelete = async () => {
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

  // ---------- KB handlers ----------

  const openKbCreateDialog = () => {
    setKbDialogMode('create')
    setSelectedKnowledgeBase(null)
    setKbDialogOpen(true)
  }

  const openKbEditDialog = (knowledgeBase: KnowledgeBaseRecord) => {
    setKbDialogMode('edit')
    setSelectedKnowledgeBase(knowledgeBase)
    setKbDialogOpen(true)
  }

  const handleKnowledgeBaseSubmit = async (payload: KnowledgeBaseCreateRequest) => {
    try {
      setKbSubmitting(true)
      if (kbDialogMode === 'create') {
        const response = await createKnowledgeBase(currentWorkspaceId, payload)
        toast.success(response.message)
      } else if (selectedKnowledgeBase) {
        const response = await updateKnowledgeBase(currentWorkspaceId, selectedKnowledgeBase.id, {
          name: payload.name,
          description: payload.description,
          status: payload.status,
          config_override: payload.config_override,
          // Forward as-is; "" (blank) clears the tag back to uncategorised,
          // any other string sets/renames it.
          category: payload.category,
        })
        toast.success(response.message)
      }
      setKbDialogOpen(false)
      setSelectedKnowledgeBase(null)
      await loadKnowledgeBases()
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setKbSubmitting(false)
    }
  }

  const handleKnowledgeBaseDelete = async () => {
    if (!kbDeleteTarget) return
    try {
      setKbSubmitting(true)
      const response = await deleteKnowledgeBase(currentWorkspaceId, kbDeleteTarget.id)
      toast.success(response.message)
      setKbDeleteTarget(null)
      await loadKnowledgeBases()
    } catch (deleteError) {
      toast.error(errorMessage(deleteError))
    } finally {
      setKbSubmitting(false)
    }
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* Page header */}
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.workspaceDirectory.badge')}
            </Badge>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.workspaceDirectory.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.workspaceDirectory.description')}
          </p>
        </div>
        <Button onClick={openWorkspaceCreateDialog}>
          <PlusIcon className="size-4" />
          {t('platformShell.workspaceDirectory.createWorkspace', { defaultValue: '新建工作区' })}
        </Button>
      </header>

      {/* Access ribbon */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {t('platformShell.workspaceDirectory.activeWorkspace')}
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
        <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-6 px-6 py-6">
          {/* ---- Workspaces section ---- */}
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <FolderKanbanIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.workspaceDirectory.workspacesDirectory', {
                    defaultValue: 'Workspaces',
                  })}
                </h2>
                {!workspacesLoading && (
                  <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[10px]">
                    {workspaces.length}
                  </Badge>
                )}
              </div>
            </div>

            {workspaceError && (
              <Alert className="border-border/70 bg-muted/20">
                <AlertTitle>
                  {t('platformShell.workspaceDirectory.workspacesUnavailable', {
                    defaultValue: '暂时无法读取工作区',
                  })}
                </AlertTitle>
                <AlertDescription className="text-xs">
                  {t('platformShell.workspaceDirectory.workspacesUnavailableHint', {
                    defaultValue:
                      '后端尚未就绪，或未运行 migrate_workspaces_create_table 迁移。稍后重试即可。',
                  })}
                </AlertDescription>
              </Alert>
            )}

            {workspacesLoading ? (
              <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                {t('platformShell.workspaceDirectory.loadingWorkspaces', {
                  defaultValue: '加载工作区…',
                })}
              </div>
            ) : workspaces.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                {t('platformShell.workspaceDirectory.emptyWorkspaces', {
                  defaultValue: '还没有工作区 — 点击右上角按钮创建第一个。',
                })}
              </div>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                {workspaces.map((workspace) => (
                  <WorkspaceCard
                    key={workspace.id}
                    workspace={workspace}
                    onDelete={() => setWorkspaceDeleteTarget(workspace)}
                  />
                ))}
              </div>
            )}
          </section>

          {/* ---- KBs section ---- */}
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <LayoutGridIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.workspaceDirectory.knowledgeBaseDirectory')}
                </h2>
                {!kbLoading && (
                  <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[10px]">
                    {knowledgeBases.length}
                  </Badge>
                )}
                <span className="text-xs text-muted-foreground">· {currentWorkspaceId}</span>
              </div>
              {canManageKnowledgeBases && (
                <Button size="sm" variant="outline" onClick={openKbCreateDialog}>
                  <PlusIcon className="size-4" />
                  {t('platformShell.workspaceDirectory.createKb', { defaultValue: '新建知识库' })}
                </Button>
              )}
            </div>

            {kbError && (
              <Alert className="border-border/70 bg-muted/20">
                <AlertTitle>
                  {t('platformShell.workspaceDirectory.compatibilityMode')}
                </AlertTitle>
                <AlertDescription className="text-xs">
                  {t('platformShell.workspaceDirectory.compatibilityDescription')}
                </AlertDescription>
              </Alert>
            )}

            {kbLoading ? (
              <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                {t('platformShell.workspaceDirectory.loadingKnowledgeBases')}
              </div>
            ) : knowledgeBases.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                {t('platformShell.workspaceDirectory.empty')}
              </div>
            ) : (
              <div className="space-y-5">
                {knowledgeBaseGroups.map(([groupKey, items]) => {
                  const isUncategorized = groupKey === UNCATEGORIZED_KEY
                  const label = isUncategorized
                    ? t('platformShell.workspaceDirectory.uncategorized', {
                      defaultValue: '未分类',
                    })
                    : groupKey
                  return (
                    <div key={groupKey} className="space-y-3">
                      <div className="flex items-center gap-2">
                        <h3
                          className={`text-xs font-semibold uppercase tracking-[0.14em] ${
                            isUncategorized
                              ? 'text-muted-foreground/70'
                              : 'text-foreground'
                          }`}
                        >
                          {label}
                        </h3>
                        <Badge
                          variant="outline"
                          className="rounded-full px-2 py-0.5 text-[10px]"
                        >
                          {items.length}
                        </Badge>
                      </div>
                      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                        {items.map((kb) => (
                          <KnowledgeBaseCard
                            key={kb.id}
                            kb={kb}
                            canManage={canManageKnowledgeBases}
                            onOpen={appRoutes.kbDocuments(kb.workspace_id, kb.id)}
                            onEdit={() => openKbEditDialog(kb)}
                            onDelete={() => setKbDeleteTarget(kb)}
                          />
                        ))}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </section>
        </div>
      </div>

      {/* ---- Dialogs ---- */}

      {/* KB form */}
      <KnowledgeBaseFormDialog
        open={kbDialogOpen}
        mode={kbDialogMode}
        knowledgeBase={selectedKnowledgeBase}
        submitting={kbSubmitting}
        categorySuggestions={categorySuggestions}
        onOpenChange={setKbDialogOpen}
        onSubmit={handleKnowledgeBaseSubmit}
      />

      {/* KB delete confirm */}
      <AlertDialog
        open={Boolean(kbDeleteTarget)}
        onOpenChange={(open) => !open && setKbDeleteTarget(null)}
      >
        <AlertDialogContent className="rounded-2xl border-border/70">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('platformShell.workspaceDirectory.deleteDialog.title')}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {kbDeleteTarget
                ? t('platformShell.workspaceDirectory.deleteDialog.descriptionWithName', {
                  knowledgeBaseName: kbDeleteTarget.name || kbDeleteTarget.id,
                  workspaceId: currentWorkspaceId,
                })
                : t('platformShell.workspaceDirectory.deleteDialog.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={kbSubmitting}>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleKnowledgeBaseDelete}
              disabled={kbSubmitting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {kbSubmitting
                ? t('platformShell.common.deleting')
                : t('platformShell.workspaceDirectory.deleteKb')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Workspace create dialog */}
      <Dialog open={workspaceDialogOpen} onOpenChange={setWorkspaceDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {t('platformShell.workspaceDirectory.workspaceForm.title', {
                defaultValue: '新建工作区',
              })}
            </DialogTitle>
            <DialogDescription>
              {t('platformShell.workspaceDirectory.workspaceForm.description', {
                defaultValue:
                  '工作区分隔知识库与成员。创建后你会自动成为该工作区的 Owner。',
              })}
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={handleWorkspaceSubmit}>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('platformShell.workspaceDirectory.workspaceForm.nameLabel', {
                  defaultValue: '名称',
                })}
              </label>
              <Input
                required
                autoFocus
                value={workspaceForm.name}
                onChange={(event) =>
                  setWorkspaceForm((prev) => ({ ...prev, name: event.target.value }))
                }
                placeholder={t(
                  'platformShell.workspaceDirectory.workspaceForm.namePlaceholder',
                  { defaultValue: '例如：Marketing Research' }
                )}
                disabled={workspaceSubmitting}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('platformShell.workspaceDirectory.workspaceForm.descriptionLabel', {
                  defaultValue: '描述',
                })}
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
                  : t('platformShell.workspaceDirectory.workspaceForm.submit', {
                    defaultValue: '创建',
                  })}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Workspace delete confirm */}
      <AlertDialog
        open={Boolean(workspaceDeleteTarget)}
        onOpenChange={(open) => !open && setWorkspaceDeleteTarget(null)}
      >
        <AlertDialogContent className="rounded-2xl border-border/70">
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
              onClick={handleWorkspaceDelete}
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

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function WorkspaceCard({
  workspace,
  onDelete,
}: {
  workspace: WorkspaceRecord
  onDelete: () => void
}) {
  const { t } = useTranslation()
  const isDefault = workspace.id === defaultWorkspaceId

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border/60 bg-background/70 px-4 py-3 transition-colors hover:border-emerald-500/30">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-foreground">
            {workspace.name}
          </p>
        </div>
        {isDefault && (
          <Badge variant="outline" className="shrink-0 rounded-full px-2 py-0.5 text-[10px]">
            {t('platformShell.workspaceDirectory.defaultBadge')}
          </Badge>
        )}
      </div>

      {workspace.description ? (
        <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">
          {workspace.description}
        </p>
      ) : (
        <p className="text-xs leading-5 text-muted-foreground/60">—</p>
      )}

      <div className="mt-auto flex flex-wrap gap-2">
        <Button size="sm" variant="outline" asChild>
          <Link to={appRoutes.kbOverview(workspace.id, defaultKnowledgeBaseId)}>
            {t('platformShell.common.open', { defaultValue: '打开' })}
          </Link>
        </Button>
        {!isDefault && (
          <Button
            variant="ghost"
            size="sm"
            className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
            onClick={onDelete}
            tooltip={t('platformShell.common.delete')}
            side="bottom"
          >
            <Trash2Icon className="size-4" />
          </Button>
        )}
      </div>
    </div>
  )
}

function KnowledgeBaseCard({
  kb,
  canManage,
  onOpen,
  onEdit,
  onDelete,
}: {
  kb: KnowledgeBaseRecord
  canManage: boolean
  onOpen: string
  onEdit: () => void
  onDelete: () => void
}) {
  const { t } = useTranslation()
  const isDefault = kb.id === defaultKnowledgeBaseId

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border/60 bg-background/70 px-4 py-3 transition-colors hover:border-emerald-500/30">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
            <BookOpenTextIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-foreground">
              {kb.name || kb.id}
            </p>
            <p className="mt-0.5 truncate text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
              {kb.id}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {isDefault && (
            <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[10px]">
              {t('platformShell.workspaceDirectory.defaultBadge')}
            </Badge>
          )}
          {kb.status && kb.status !== 'active' && (
            <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[10px]">
              {kb.status}
            </Badge>
          )}
        </div>
      </div>

      <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">
        {kb.description || t('platformShell.workspaceDirectory.noDescription')}
      </p>

      <div className="mt-auto flex flex-wrap gap-2">
        <Button size="sm" asChild>
          <Link to={onOpen}>{t('platformShell.common.documents')}</Link>
        </Button>
        {canManage && (
          <>
            <Button variant="outline" size="sm" onClick={onEdit}>
              {t('platformShell.common.edit')}
            </Button>
            {!isDefault && (
              <Button
                variant="ghost"
                size="sm"
                className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                onClick={onDelete}
                tooltip={t('platformShell.common.delete')}
                side="bottom"
              >
                <Trash2Icon className="size-4" />
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  )
}
