import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { BookOpenTextIcon, FolderKanbanIcon, Trash2Icon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { appRoutes, defaultWorkspaceId } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { hasPermission, resolveEffectiveRole } from '@/lib/permissions'
import {
  deleteWorkspace,
  getWorkspace,
  listKnowledgeBases,
  updateWorkspace,
  type KnowledgeBaseRecord,
  type WorkspaceRecord,
} from '@/api/lightrag'
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

/**
 * Workspace settings — DocumentsPage-style layout.
 *
 * Shell mirrors ``DocumentsPage`` on purpose: flat header strip,
 * full-width content, sections separated by horizontal rules rather
 * than card wrappers. User feedback: "和文档管理页用一个风格". The
 * goal is zero visual context-switch when an operator flips between
 * "manage the docs" and "manage the workspace that holds them".
 *
 * Three things happen here, in the order an operator touches them:
 *   1. Rename / re-describe the workspace (flat form).
 *   2. See the KBs inside it (table with jump-to actions).
 *   3. Delete the workspace (danger section, hidden for ``default``).
 */
export default function WorkspaceSettingsPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const canUpdate = hasPermission(effectiveRole, 'workspace:update')
  const canDelete = hasPermission(effectiveRole, 'workspace:delete')
  const isDefaultWorkspace = currentWorkspaceId === defaultWorkspaceId

  const [workspace, setWorkspace] = useState<WorkspaceRecord | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [kbs, setKbs] = useState<KnowledgeBaseRecord[]>([])
  const [kbsLoading, setKbsLoading] = useState(true)

  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

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

  const loadKbs = useCallback(async () => {
    try {
      setKbsLoading(true)
      const response = await listKnowledgeBases(currentWorkspaceId)
      setKbs(response.items)
    } catch {
      setKbs([])
    } finally {
      setKbsLoading(false)
    }
  }, [currentWorkspaceId])

  useEffect(() => {
    void loadWorkspace()
    void loadKbs()
  }, [loadWorkspace, loadKbs])

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

  const handleDelete = async () => {
    try {
      setDeleting(true)
      await deleteWorkspace(currentWorkspaceId)
      toast.success(
        t('platformShell.workspaceSettings.deleteSuccess', {
          defaultValue: '工作区已删除',
        })
      )
      navigate(appRoutes.workspaces)
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setDeleting(false)
      setDeleteConfirmOpen(false)
    }
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* Header — same shape as DocumentsPage. Primary action is the
          link to the workspace directory so "create a new workspace"
          is always one click away from here. */}
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.workspaceSettings.badge')}
            </Badge>
            <span className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
              {currentWorkspaceId}
            </span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {workspace?.name || t('platformShell.workspaceSettings.title')}
          </h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.workspaces}>
              <FolderKanbanIcon className="size-4" aria-hidden="true" />
              {t('platformShell.workspaceSettings.openDirectory', {
                defaultValue: '所有工作区',
              })}
            </Link>
          </Button>
        </div>
      </header>

      {/* Scroll body — full width, flat sections divided by border-b
          instead of nested cards. Matches DocumentsPage. */}
      <div className="min-h-0 flex-1 overflow-auto">
        {loadError && (
          <div className="border-b border-border/60 px-6 py-4">
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.workspaceSettings.loadError', {
                  defaultValue: '无法读取工作区信息',
                })}
              </AlertTitle>
              <AlertDescription className="text-xs">
                {loadError}
              </AlertDescription>
            </Alert>
          </div>
        )}

        {!canUpdate && workspace && (
          <div className="border-b border-border/60 px-6 py-4">
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
          </div>
        )}

        {/* Section 1: metadata form (flat fields, no card wrapper) */}
        <section className="border-b border-border/60 px-6 py-5">
          {loading ? (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
              {t('platformShell.common.loading', { defaultValue: '加载中…' })}
            </div>
          ) : workspace ? (
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="space-y-2">
                  <label
                    htmlFor="workspace-name"
                    className="text-sm font-medium text-foreground"
                  >
                    {t('platformShell.workspaceSettings.form.nameLabel', {
                      defaultValue: '名称',
                    })}
                  </label>
                  <Input
                    id="workspace-name"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    disabled={!canUpdate || submitting}
                    required
                    maxLength={255}
                    placeholder={t(
                      'platformShell.workspaceSettings.form.namePlaceholder',
                      { defaultValue: '例如：Marketing Research' }
                    )}
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
                      {workspace.id}
                    </dd>
                    <dt className="text-muted-foreground">
                      {t('platformShell.workspaceSettings.form.createdAt', {
                        defaultValue: '创建时间',
                      })}
                    </dt>
                    <dd className="truncate font-mono text-foreground">
                      {workspace.created_at}
                    </dd>
                    <dt className="text-muted-foreground">
                      {t('platformShell.workspaceSettings.form.lastUpdated', {
                        defaultValue: '上次更新',
                      })}
                    </dt>
                    <dd className="truncate font-mono text-foreground">
                      {workspace.updated_at}
                    </dd>
                  </dl>
                </div>
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
                  onClick={handleReset}
                  disabled={!dirty || submitting}
                >
                  {t('common.reset', { defaultValue: '还原' })}
                </Button>
                <Button type="submit" size="sm" disabled={!canSave}>
                  {submitting
                    ? t('platformShell.common.saving', {
                      defaultValue: '保存中…',
                    })
                    : t('platformShell.workspaceSettings.form.save', {
                      defaultValue: '保存',
                    })}
                </Button>
              </div>
            </form>
          ) : null}
        </section>

        {/* Section 2: KB table — mirrors the DocumentManager table at
            the same depth in the layout so switching pages feels
            continuous. */}
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
            {!kbsLoading && (
              <Badge
                variant="outline"
                className="rounded-full px-2 py-0.5 text-[10px]"
              >
                {kbs.length}
              </Badge>
            )}
            <Button variant="outline" size="sm" className="ml-auto" asChild>
              <Link to={appRoutes.workspaces}>
                {t('platformShell.workspaceSettings.manageInDirectory', {
                  defaultValue: '在工作区目录中管理',
                })}
              </Link>
            </Button>
          </div>

          {kbsLoading ? (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-6 text-center text-sm text-muted-foreground">
              {t('platformShell.common.loading', { defaultValue: '加载中…' })}
            </div>
          ) : kbs.length > 0 ? (
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
                  {kbs.map((kb) => (
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
                        <div className="flex items-center justify-end gap-2">
                          <Button variant="outline" size="sm" asChild>
                            <Link
                              to={appRoutes.kbDocuments(
                                currentWorkspaceId,
                                kb.id
                              )}
                            >
                              {t('platformShell.common.documents', {
                                defaultValue: '文档',
                              })}
                            </Link>
                          </Button>
                          <Button variant="ghost" size="sm" asChild>
                            <Link
                              to={appRoutes.kbSettings(
                                currentWorkspaceId,
                                kb.id
                              )}
                            >
                              {t('platformShell.common.edit', {
                                defaultValue: '编辑',
                              })}
                            </Link>
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
              {t('platformShell.workspaceSettings.noKbs', {
                defaultValue:
                  '这个工作区还没有知识库。在工作区目录页新建一个即可开始上传文档。',
              })}
            </div>
          )}
        </section>

        {/* Section 3: danger zone (delete workspace). Hidden for the
            reserved ``default`` id since the backend refuses that. */}
        {canDelete && !isDefaultWorkspace && workspace && (
          <section className="px-6 py-5">
            <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/[0.05] p-4">
              <div className="min-w-0 space-y-1">
                <h2 className="text-sm font-semibold text-destructive">
                  {t('platformShell.workspaceSettings.dangerZoneTitle', {
                    defaultValue: '删除工作区',
                  })}
                </h2>
                <p className="text-xs leading-5 text-muted-foreground">
                  {t('platformShell.workspaceSettings.dangerZoneDescription', {
                    defaultValue:
                      '删除后会一并移除成员授权。存储中的文档与图谱不会被自动清理，可稍后手动清理。',
                  })}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={() => setDeleteConfirmOpen(true)}
              >
                <Trash2Icon className="size-4" />
                {t('platformShell.workspaceSettings.deleteAction', {
                  defaultValue: '删除此工作区',
                })}
              </Button>
            </div>
          </section>
        )}
      </div>

      <AlertDialog
        open={deleteConfirmOpen}
        onOpenChange={(open) => !open && setDeleteConfirmOpen(false)}
      >
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
                name: workspace?.name || currentWorkspaceId,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>
              {t('common.cancel', { defaultValue: '取消' })}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              disabled={deleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleting
                ? t('platformShell.common.deleting', { defaultValue: '删除中…' })
                : t('platformShell.common.delete', { defaultValue: '删除' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
