import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  BookOpenTextIcon,
  BriefcaseBusinessIcon,
  FolderKanbanIcon,
  PlusIcon,
  Trash2Icon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { appRoutes, defaultWorkspaceId } from '@/app/routes'
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
  updateWorkspace,
} from '@/api/lightrag'
import { useAuthStore } from '@/stores/state'
import { hasPermission, resolveEffectiveRole } from '@/lib/permissions'
import { errorMessage } from '@/lib/utils'
import { cn } from '@/lib/utils'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'

// ---------------------------------------------------------------------------
// Workspace management — one page, everything.
//
// User feedback ("交互很奇怪… 集成到一个子页. 在这个子页面我可以创建或移除
// 工作区 也可以配置某一个工作区可以访问哪些知识库") merged the old
// WorkspaceDirectory + WorkspaceSettings into a single screen:
//   - Tab strip at the top lists every workspace the user belongs to,
//     plus a + tile for creating a new one.
//   - Selecting a tab swaps in that workspace's metadata form + KB
//     list below.
//   - Deleting the current workspace lives in a danger-zone strip at
//     the bottom (hidden for the reserved ``default`` id, backend
//     refuses that anyway).
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
  const [name, setName] = useState(mode === 'edit' ? knowledgeBase?.name || '' : '')
  const [description, setDescription] = useState(
    mode === 'edit' ? knowledgeBase?.description || '' : ''
  )
  const [category, setCategory] = useState(
    mode === 'edit' ? knowledgeBase?.category || '' : ''
  )

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmedName = name.trim()
    if (!trimmedName) {
      toast.error(
        t('platformShell.workspaceDirectory.kbForm.nameRequired', {
          defaultValue: '请填写知识库名称',
        })
      )
      return
    }
    await onSubmit({
      // kb_id omitted → backend auto-generates kb_<uuid4[:8]>.
      name: trimmedName,
      description: description.trim(),
      category: category.trim(),
    })
  }

  return (
    <DialogContent className="rounded-2xl border-border/70 sm:max-w-lg">
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
            autoFocus
            required
            className="h-10 rounded-xl border-border/70"
          />
        </div>

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
            rows={3}
            className="rounded-xl border-border/70"
          />
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
  const params = useParams()

  const [workspaces, setWorkspaces] = useState<WorkspaceRecord[]>([])
  const [workspacesLoading, setWorkspacesLoading] = useState(true)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)

  // Which workspace tab is active. Defaults to the URL workspaceId if
  // this page is rendered at ``/workspaces/:workspaceId/settings`` (the
  // legacy settings URL now redirects through this component); otherwise
  // the first workspace we load.
  const [selectedId, setSelectedId] = useState<string | null>(
    params.workspaceId ?? null
  )

  // Workspace metadata edit buffer — scoped to the currently selected tab.
  const [metaName, setMetaName] = useState('')
  const [metaDescription, setMetaDescription] = useState('')
  const [metaSubmitting, setMetaSubmitting] = useState(false)

  // Create-workspace dialog
  const [wsCreateOpen, setWsCreateOpen] = useState(false)
  const [wsCreateForm, setWsCreateForm] = useState({ name: '', description: '' })
  const [wsCreateSubmitting, setWsCreateSubmitting] = useState(false)

  // Delete-workspace confirm
  const [wsDeleteOpen, setWsDeleteOpen] = useState(false)
  const [wsDeleteSubmitting, setWsDeleteSubmitting] = useState(false)

  // KB state for the selected workspace
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRecord[]>([])
  const [kbLoading, setKbLoading] = useState(true)
  const [kbError, setKbError] = useState<string | null>(null)

  // KB form dialog
  const [kbDialogOpen, setKbDialogOpen] = useState(false)
  const [kbDialogMode, setKbDialogMode] = useState<'create' | 'edit'>('create')
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] =
    useState<KnowledgeBaseRecord | null>(null)
  const [kbSubmitting, setKbSubmitting] = useState(false)
  const [kbDeleteTarget, setKbDeleteTarget] = useState<KnowledgeBaseRecord | null>(
    null
  )

  const loadWorkspaces = useCallback(async () => {
    try {
      setWorkspacesLoading(true)
      setWorkspaceError(null)
      const response = await listWorkspaces()
      setWorkspaces(response.items)
      // Pick the first available workspace when nothing is selected yet
      // or when the selection points at a workspace that just got
      // deleted out from under us.
      setSelectedId((current) => {
        if (current && response.items.some((ws) => ws.id === current)) {
          return current
        }
        return response.items[0]?.id ?? null
      })
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

  const selectedWorkspace = useMemo(
    () => workspaces.find((ws) => ws.id === selectedId) ?? null,
    [workspaces, selectedId]
  )

  // Seed the metadata form whenever the active tab changes.
  useEffect(() => {
    if (selectedWorkspace) {
      setMetaName(selectedWorkspace.name)
      setMetaDescription(selectedWorkspace.description ?? '')
    } else {
      setMetaName('')
      setMetaDescription('')
    }
  }, [selectedWorkspace])

  const loadKnowledgeBases = useCallback(async () => {
    if (!selectedId) {
      setKnowledgeBases([])
      setKbLoading(false)
      return
    }
    try {
      setKbLoading(true)
      setKbError(null)
      const response = await listKnowledgeBases(selectedId)
      setKnowledgeBases(response.items)
    } catch (loadError) {
      setKbError(errorMessage(loadError))
      setKnowledgeBases([])
    } finally {
      setKbLoading(false)
    }
  }, [selectedId])

  useEffect(() => {
    void loadKnowledgeBases()
  }, [loadKnowledgeBases])

  const effectiveRole = selectedId
    ? resolveEffectiveRole(
      { role, memberships },
      { workspaceId: selectedId, kbId: null }
    )
    : 'viewer'
  const canManage = hasPermission(effectiveRole, 'workspace:update')
  const canDelete = hasPermission(effectiveRole, 'workspace:delete')
  const isDefaultWorkspace = selectedId === defaultWorkspaceId

  const metaDirty = selectedWorkspace
    ? metaName.trim() !== selectedWorkspace.name ||
      (metaDescription.trim() || null) !== (selectedWorkspace.description ?? null)
    : false
  const canSaveMeta =
    canManage && metaDirty && !metaSubmitting && metaName.trim().length > 0

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

  // ---------- workspace handlers ----------

  const openWorkspaceCreateDialog = () => {
    setWsCreateForm({ name: '', description: '' })
    setWsCreateOpen(true)
  }

  const handleWorkspaceCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmedName = wsCreateForm.name.trim()
    if (!trimmedName) {
      toast.error(
        t('platformShell.workspaceDirectory.workspaceForm.nameRequired', {
          defaultValue: '请填写工作区名称',
        })
      )
      return
    }
    try {
      setWsCreateSubmitting(true)
      const created = await createWorkspace({
        name: trimmedName,
        description: wsCreateForm.description.trim() || null,
      })
      toast.success(
        t('platformShell.workspaceDirectory.workspaceForm.createSuccess', {
          defaultValue: '工作区 "{{name}}" 已创建',
          name: created.name,
        })
      )
      setWsCreateOpen(false)
      setSelectedId(created.id)
      await loadWorkspaces()
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setWsCreateSubmitting(false)
    }
  }

  const handleWorkspaceMetadataSave = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!selectedWorkspace || !canSaveMeta) return
    try {
      setMetaSubmitting(true)
      const payload: { name?: string; description?: string | null } = {}
      if (metaName.trim() !== selectedWorkspace.name) {
        payload.name = metaName.trim()
      }
      if ((metaDescription.trim() || null) !== (selectedWorkspace.description ?? null)) {
        payload.description = metaDescription.trim() || null
      }
      await updateWorkspace(selectedWorkspace.id, payload)
      toast.success(
        t('platformShell.workspaceSettings.saveSuccess', {
          defaultValue: '工作区已更新',
        })
      )
      await loadWorkspaces()
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setMetaSubmitting(false)
    }
  }

  const handleWorkspaceMetadataReset = () => {
    if (!selectedWorkspace) return
    setMetaName(selectedWorkspace.name)
    setMetaDescription(selectedWorkspace.description ?? '')
  }

  const handleWorkspaceDelete = async () => {
    if (!selectedWorkspace) return
    try {
      setWsDeleteSubmitting(true)
      await deleteWorkspace(selectedWorkspace.id)
      toast.success(
        t('platformShell.workspaceDirectory.workspaceForm.deleteSuccess', {
          defaultValue: '工作区 "{{name}}" 已删除',
          name: selectedWorkspace.name,
        })
      )
      setWsDeleteOpen(false)
      setSelectedId(null)
      await loadWorkspaces()
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setWsDeleteSubmitting(false)
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
    if (!selectedId) return
    try {
      setKbSubmitting(true)
      if (kbDialogMode === 'create') {
        const response = await createKnowledgeBase(selectedId, payload)
        toast.success(response.message)
      } else if (selectedKnowledgeBase) {
        const response = await updateKnowledgeBase(
          selectedId,
          selectedKnowledgeBase.id,
          {
            name: payload.name,
            description: payload.description,
            category: payload.category,
          }
        )
        toast.success(response.message)
      }
      setKbDialogOpen(false)
      setSelectedKnowledgeBase(null)
      await loadKnowledgeBases()
      // Notify the KB picker dropdown on the documents page.
      window.dispatchEvent(
        new CustomEvent('lightrag:kb-updated', {
          detail: { workspaceId: selectedId },
        })
      )
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setKbSubmitting(false)
    }
  }

  const handleKnowledgeBaseDelete = async () => {
    if (!kbDeleteTarget || !selectedId) return
    try {
      setKbSubmitting(true)
      const response = await deleteKnowledgeBase(selectedId, kbDeleteTarget.id)
      toast.success(response.message)
      setKbDeleteTarget(null)
      await loadKnowledgeBases()
      window.dispatchEvent(
        new CustomEvent('lightrag:kb-updated', {
          detail: { workspaceId: selectedId },
        })
      )
    } catch (deleteError) {
      toast.error(errorMessage(deleteError))
    } finally {
      setKbSubmitting(false)
    }
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* Header — DocumentsPage-shaped. Primary action is "新建工作区". */}
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.workspaceDirectory.badge', {
                defaultValue: '工作区',
              })}
            </Badge>
            {!workspacesLoading && (
              <span className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
                {workspaces.length}{' '}
                {t('platformShell.workspaceDirectory.workspaceCount', {
                  defaultValue: '个',
                })}
              </span>
            )}
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.workspaceDirectory.title', {
              defaultValue: '工作区管理',
            })}
          </h1>
        </div>
        <Button size="sm" onClick={openWorkspaceCreateDialog}>
          <PlusIcon className="size-4" />
          {t('platformShell.workspaceDirectory.createWorkspace', {
            defaultValue: '新建工作区',
          })}
        </Button>
      </header>

      {/* Workspace tab strip. Tabs + a "+" shortcut at the end. Uses
          native <button> so no Radix focus chrome leaks in — the user
          specifically complained about the previous Badge/pill row
          looking "clicked" by default. */}
      <div className="flex items-center gap-2 overflow-x-auto border-b border-border/60 bg-muted/10 px-4 py-2">
        {workspacesLoading ? (
          <span className="text-xs text-muted-foreground">
            {t('platformShell.common.loading', { defaultValue: '加载中…' })}
          </span>
        ) : workspaces.length === 0 ? (
          <span className="text-xs text-muted-foreground">
            {t('platformShell.workspaceDirectory.emptyWorkspaces', {
              defaultValue: '还没有工作区 — 点击右上角按钮创建第一个。',
            })}
          </span>
        ) : (
          workspaces.map((ws) => {
            const isActive = ws.id === selectedId
            return (
              <button
                key={ws.id}
                type="button"
                onClick={() => setSelectedId(ws.id)}
                className={cn(
                  'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                  isActive
                    ? 'border-emerald-500/50 bg-emerald-500/[0.12] text-emerald-700 dark:text-emerald-200'
                    : 'border-border/60 bg-background/70 text-muted-foreground hover:border-emerald-500/30 hover:text-foreground'
                )}
              >
                <BriefcaseBusinessIcon className="size-3.5" aria-hidden="true" />
                <span className="max-w-[200px] truncate">{ws.name}</span>
                {ws.id === defaultWorkspaceId && (
                  <span className="text-[10px] text-muted-foreground">
                    ·{' '}
                    {t('platformShell.workspaceDirectory.defaultBadge', {
                      defaultValue: '默认',
                    })}
                  </span>
                )}
              </button>
            )
          })
        )}
        <button
          type="button"
          onClick={openWorkspaceCreateDialog}
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-dashed border-border/60 bg-transparent px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:border-emerald-500/40 hover:text-foreground"
        >
          <PlusIcon className="size-3.5" aria-hidden="true" />
          {t('platformShell.workspaceDirectory.createWorkspace', {
            defaultValue: '新建工作区',
          })}
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {workspaceError && (
          <div className="border-b border-border/60 px-6 py-4">
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.workspaceDirectory.workspacesUnavailable', {
                  defaultValue: '暂时无法读取工作区',
                })}
              </AlertTitle>
              <AlertDescription className="text-xs">
                {workspaceError}
              </AlertDescription>
            </Alert>
          </div>
        )}

        {selectedWorkspace && (
          <>
            {/* Metadata section — rename + re-describe + inline meta */}
            <section className="border-b border-border/60 px-6 py-5">
              <form
                onSubmit={handleWorkspaceMetadataSave}
                className="flex flex-col gap-4"
              >
                <div className="grid gap-4 lg:grid-cols-2">
                  <div className="space-y-2">
                    <label
                      htmlFor="ws-name"
                      className="text-sm font-medium text-foreground"
                    >
                      {t('platformShell.workspaceSettings.form.nameLabel', {
                        defaultValue: '名称',
                      })}
                    </label>
                    <Input
                      id="ws-name"
                      value={metaName}
                      onChange={(event) => setMetaName(event.target.value)}
                      disabled={!canManage || metaSubmitting}
                      required
                      maxLength={255}
                      className="h-10 rounded-xl border-border/70"
                    />
                  </div>
                  <div className="space-y-2">
                    <span className="text-sm font-medium text-foreground">
                      {t('platformShell.workspaceSettings.form.meta', {
                        defaultValue: '标识',
                      })}
                    </span>
                    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 rounded-xl border border-border/60 bg-muted/10 px-3 py-2 text-xs leading-5">
                      <dt className="text-muted-foreground">ID</dt>
                      <dd className="truncate font-mono text-foreground">
                        {selectedWorkspace.id}
                      </dd>
                      <dt className="text-muted-foreground">
                        {t('platformShell.workspaceSettings.form.createdAt', {
                          defaultValue: '创建时间',
                        })}
                      </dt>
                      <dd className="truncate font-mono text-foreground">
                        {selectedWorkspace.created_at}
                      </dd>
                      <dt className="text-muted-foreground">
                        {t('platformShell.workspaceSettings.form.lastUpdated', {
                          defaultValue: '上次更新',
                        })}
                      </dt>
                      <dd className="truncate font-mono text-foreground">
                        {selectedWorkspace.updated_at}
                      </dd>
                    </dl>
                  </div>
                </div>

                <div className="space-y-2">
                  <label
                    htmlFor="ws-description"
                    className="text-sm font-medium text-foreground"
                  >
                    {t('platformShell.workspaceSettings.form.descriptionLabel', {
                      defaultValue: '描述',
                    })}
                  </label>
                  <Textarea
                    id="ws-description"
                    value={metaDescription}
                    onChange={(event) => setMetaDescription(event.target.value)}
                    disabled={!canManage || metaSubmitting}
                    rows={3}
                    maxLength={1024}
                    placeholder={t(
                      'platformShell.workspaceSettings.form.descriptionPlaceholder',
                      {
                        defaultValue:
                          '简要说明这个工作区的用途，方便在切换器里找到它。',
                      }
                    )}
                    className="rounded-xl border-border/70"
                  />
                </div>

                <div className="flex flex-wrap items-center justify-end gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={handleWorkspaceMetadataReset}
                    disabled={!metaDirty || metaSubmitting}
                  >
                    {t('common.reset', { defaultValue: '还原' })}
                  </Button>
                  <Button type="submit" size="sm" disabled={!canSaveMeta}>
                    {metaSubmitting
                      ? t('platformShell.common.saving', {
                        defaultValue: '保存中…',
                      })
                      : t('platformShell.workspaceSettings.form.save', {
                        defaultValue: '保存',
                      })}
                  </Button>
                </div>
              </form>
            </section>

            {/* KB section — this workspace's knowledge bases */}
            <section className="border-b border-border/60 px-6 py-5">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <FolderKanbanIcon
                  className="size-4 text-emerald-600 dark:text-emerald-400"
                  aria-hidden="true"
                />
                <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.workspaceSettings.knowledgeBases', {
                    defaultValue: '本工作区的知识库',
                  })}
                </h2>
                {!kbLoading && (
                  <Badge
                    variant="outline"
                    className="rounded-full px-2 py-0.5 text-[10px]"
                  >
                    {knowledgeBases.length}
                  </Badge>
                )}
                {canManage && (
                  <Button
                    size="sm"
                    className="ml-auto"
                    onClick={openKbCreateDialog}
                  >
                    <PlusIcon className="size-4" />
                    {t('platformShell.workspaceDirectory.createKb', {
                      defaultValue: '新建知识库',
                    })}
                  </Button>
                )}
              </div>

              {kbError && (
                <Alert className="mb-3 border-border/70 bg-muted/20">
                  <AlertTitle>
                    {t('platformShell.workspaceDirectory.compatibilityMode')}
                  </AlertTitle>
                  <AlertDescription className="text-xs">
                    {kbError}
                  </AlertDescription>
                </Alert>
              )}

              {kbLoading ? (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-6 text-center text-sm text-muted-foreground">
                  {t('platformShell.common.loading', { defaultValue: '加载中…' })}
                </div>
              ) : knowledgeBases.length > 0 ? (
                <div className="overflow-hidden rounded-xl border border-border/60 bg-background/70">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>
                          {t('platformShell.workspaceDirectory.displayName', {
                            defaultValue: '名称',
                          })}
                        </TableHead>
                        <TableHead>
                          {t('platformShell.workspaceDirectory.categoryLabel', {
                            defaultValue: '分类',
                          })}
                        </TableHead>
                        <TableHead>
                          {t('platformShell.workspaceDirectory.descriptionLabel', {
                            defaultValue: '描述',
                          })}
                        </TableHead>
                        <TableHead className="text-right">
                          {t('platformShell.common.actions', {
                            defaultValue: '操作',
                          })}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {knowledgeBases.map((kb) => (
                        <TableRow key={kb.id}>
                          <TableCell className="font-medium">
                            <div className="flex min-w-0 items-center gap-2">
                              <BookOpenTextIcon
                                className="size-3.5 shrink-0 text-muted-foreground"
                                aria-hidden="true"
                              />
                              <span className="truncate">{kb.name || kb.id}</span>
                            </div>
                          </TableCell>
                          <TableCell className="text-muted-foreground">
                            {kb.category ? (
                              <Badge
                                variant="outline"
                                className="rounded-full px-2 py-0.5 text-[11px]"
                              >
                                {kb.category}
                              </Badge>
                            ) : (
                              <span className="text-xs text-muted-foreground/60">
                                —
                              </span>
                            )}
                          </TableCell>
                          <TableCell className="max-w-[320px] truncate text-muted-foreground">
                            {kb.description || (
                              <span className="text-muted-foreground/60">—</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="flex items-center justify-end gap-1.5">
                              <Button variant="outline" size="sm" asChild>
                                <Link
                                  to={appRoutes.kbDocuments(selectedId!, kb.id)}
                                >
                                  {t('platformShell.common.documents', {
                                    defaultValue: '文档',
                                  })}
                                </Link>
                              </Button>
                              {canManage && (
                                <>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => openKbEditDialog(kb)}
                                  >
                                    {t('platformShell.common.edit', {
                                      defaultValue: '编辑',
                                    })}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                                    onClick={() => setKbDeleteTarget(kb)}
                                  >
                                    <Trash2Icon className="size-3.5" />
                                  </Button>
                                </>
                              )}
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              ) : (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                  {t('platformShell.workspaceDirectory.empty', {
                    defaultValue:
                      '还没有知识库 — 点击"新建知识库"创建第一个。',
                  })}
                </div>
              )}
            </section>

            {/* Danger zone — delete this workspace */}
            {canDelete && !isDefaultWorkspace && (
              <section className="px-6 py-5">
                <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/[0.05] p-4">
                  <div className="min-w-0 space-y-1">
                    <h2 className="text-sm font-semibold text-destructive">
                      {t('platformShell.workspaceSettings.dangerZoneTitle', {
                        defaultValue: '删除工作区',
                      })}
                    </h2>
                    <p className="text-xs leading-5 text-muted-foreground">
                      {t(
                        'platformShell.workspaceSettings.dangerZoneDescription',
                        {
                          defaultValue:
                            '删除后会一并移除成员授权。存储中的文档与图谱不会被自动清理，可稍后手动清理。',
                        }
                      )}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => setWsDeleteOpen(true)}
                  >
                    <Trash2Icon className="size-4" />
                    {t('platformShell.workspaceSettings.deleteAction', {
                      defaultValue: '删除此工作区',
                    })}
                  </Button>
                </div>
              </section>
            )}
          </>
        )}

        {!workspacesLoading && !selectedWorkspace && workspaces.length === 0 && (
          <div className="flex flex-1 items-center justify-center px-6 py-12">
            <div className="flex max-w-sm flex-col items-center gap-3 text-center">
              <div className="flex size-12 items-center justify-center rounded-full bg-muted/40">
                <BriefcaseBusinessIcon
                  className="size-5 text-muted-foreground"
                  aria-hidden="true"
                />
              </div>
              <p className="text-sm text-muted-foreground">
                {t('platformShell.workspaceDirectory.emptyWorkspaces', {
                  defaultValue: '还没有工作区 — 点击右上角按钮创建第一个。',
                })}
              </p>
              <Button size="sm" onClick={openWorkspaceCreateDialog}>
                <PlusIcon className="size-4" />
                {t('platformShell.workspaceDirectory.createWorkspace', {
                  defaultValue: '新建工作区',
                })}
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* --- dialogs --- */}

      <KnowledgeBaseFormDialog
        open={kbDialogOpen}
        mode={kbDialogMode}
        knowledgeBase={selectedKnowledgeBase}
        submitting={kbSubmitting}
        categorySuggestions={categorySuggestions}
        onOpenChange={setKbDialogOpen}
        onSubmit={handleKnowledgeBaseSubmit}
      />

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
                ? t(
                  'platformShell.workspaceDirectory.deleteDialog.descriptionWithName',
                  {
                    knowledgeBaseName: kbDeleteTarget.name || kbDeleteTarget.id,
                    workspaceId: selectedId ?? '',
                  }
                )
                : t('platformShell.workspaceDirectory.deleteDialog.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={kbSubmitting}>
              {t('common.cancel')}
            </AlertDialogCancel>
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

      <Dialog open={wsCreateOpen} onOpenChange={setWsCreateOpen}>
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
          <form className="space-y-4" onSubmit={handleWorkspaceCreate}>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('platformShell.workspaceDirectory.workspaceForm.nameLabel', {
                  defaultValue: '名称',
                })}
              </label>
              <Input
                required
                autoFocus
                value={wsCreateForm.name}
                onChange={(event) =>
                  setWsCreateForm((prev) => ({
                    ...prev,
                    name: event.target.value,
                  }))
                }
                placeholder={t(
                  'platformShell.workspaceDirectory.workspaceForm.namePlaceholder',
                  { defaultValue: '例如：Marketing Research' }
                )}
                disabled={wsCreateSubmitting}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t(
                  'platformShell.workspaceDirectory.workspaceForm.descriptionLabel',
                  { defaultValue: '描述' }
                )}
              </label>
              <Textarea
                value={wsCreateForm.description}
                onChange={(event) =>
                  setWsCreateForm((prev) => ({
                    ...prev,
                    description: event.target.value,
                  }))
                }
                rows={3}
                disabled={wsCreateSubmitting}
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setWsCreateOpen(false)}
                disabled={wsCreateSubmitting}
              >
                {t('common.cancel')}
              </Button>
              <Button type="submit" disabled={wsCreateSubmitting}>
                {wsCreateSubmitting
                  ? t('platformShell.common.saving', { defaultValue: '保存中…' })
                  : t(
                    'platformShell.workspaceDirectory.workspaceForm.submit',
                    { defaultValue: '创建' }
                  )}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog open={wsDeleteOpen} onOpenChange={setWsDeleteOpen}>
        <AlertDialogContent className="rounded-2xl border-border/70">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('platformShell.workspaceSettings.deleteDialogTitle', {
                defaultValue: '确认删除工作区',
              })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('platformShell.workspaceSettings.deleteDialogDescription', {
                defaultValue:
                  '删除工作区 "{{name}}" 将同时移除其成员授权。此操作不可撤销。',
                name: selectedWorkspace?.name ?? '',
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={wsDeleteSubmitting}>
              {t('common.cancel')}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleWorkspaceDelete}
              disabled={wsDeleteSubmitting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {wsDeleteSubmitting
                ? t('platformShell.common.deleting')
                : t('platformShell.common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
