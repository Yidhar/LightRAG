import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Settings2Icon, ShieldCheckIcon, UserPlusIcon, UsersIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import {
  createKnowledgeBaseMember,
  deleteKnowledgeBaseMember,
  getKnowledgeBase,
  listKnowledgeBaseMembers,
  type KnowledgeBaseRecord,
  type KnowledgeBaseUpdateRequest,
  type MembershipEntry,
  updateKnowledgeBase,
  updateKnowledgeBaseMemberRole,
} from '@/api/lightrag'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleDescriptionKeys,
  roleLabelKeys,
  summarizeRoleCapabilityKeys,
  type AccessRole,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import Textarea from '@/components/ui/Textarea'
import AccessBadge from '@/components/navigation/AccessBadge'

const membershipRoles: AccessRole[] = ['owner', 'admin', 'editor', 'viewer']
const defaultConfigOverrideText = '{\n  \n}'

interface KnowledgeBaseMemberFormDialogProps {
  open: boolean
  mode: 'create' | 'edit'
  member: MembershipEntry | null
  submitting: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (payload: { username?: string; role: AccessRole }) => Promise<void>
}

interface KnowledgeBaseMemberFormDialogContentProps {
  mode: 'create' | 'edit'
  member: MembershipEntry | null
  submitting: boolean
  onSubmit: (payload: { username?: string; role: AccessRole }) => Promise<void>
  onCancel: () => void
}

function KnowledgeBaseMemberFormDialogContent({
  mode,
  member,
  submitting,
  onSubmit,
  onCancel,
}: KnowledgeBaseMemberFormDialogContentProps) {
  const { t } = useTranslation()
  const [username, setUsername] = useState(mode === 'edit' ? member?.username || '' : '')
  const [role, setRole] = useState<AccessRole>(mode === 'edit' ? (member?.role || 'viewer') : 'viewer')

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (mode === 'create' && !username.trim()) {
      toast.error(t('platformShell.kbSettings.members.dialog.enterUsername'))
      return
    }

    await onSubmit({
      username: username.trim(),
      role,
    })
  }

  return (
    <DialogContent className="rounded-[1.5rem] border-border/70 sm:max-w-md">
      <DialogHeader>
        <DialogTitle>
          {mode === 'create'
            ? t('platformShell.kbSettings.members.dialog.createTitle')
            : t('platformShell.kbSettings.members.dialog.editTitle')}
        </DialogTitle>
        <DialogDescription>
          {mode === 'create'
            ? t('platformShell.kbSettings.members.dialog.createDescription')
            : t('platformShell.kbSettings.members.dialog.editDescription')}
        </DialogDescription>
      </DialogHeader>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="space-y-2">
          <label htmlFor="kb-member-username" className="text-sm font-medium text-foreground">
            {t('platformShell.kbSettings.members.dialog.username')}
          </label>
          <Input
            id="kb-member-username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder={t('platformShell.kbSettings.members.dialog.usernamePlaceholder')}
            disabled={mode === 'edit' || submitting}
            className="h-11 rounded-xl border-border/70"
          />
          {mode === 'create' && (
            <p className="text-xs leading-5 text-muted-foreground">
              {t('platformShell.kbSettings.members.dialog.usernameHint')}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <label htmlFor="kb-member-role" className="text-sm font-medium text-foreground">
            {t('platformShell.kbSettings.members.dialog.kbRole')}
          </label>
          <Select value={role} onValueChange={(value) => setRole(value as AccessRole)} disabled={submitting}>
            <SelectTrigger id="kb-member-role" className="h-11 rounded-xl border-border/70">
              <SelectValue placeholder={t('platformShell.kbSettings.members.dialog.selectRole')} />
            </SelectTrigger>
            <SelectContent>
              {membershipRoles.map((entryRole) => (
                <SelectItem key={entryRole} value={entryRole}>
                  {t(roleLabelKeys[entryRole])}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <DialogFooter className="gap-2 sm:gap-2">
          <Button type="button" variant="outline" onClick={onCancel} disabled={submitting}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={submitting}>
            {submitting
              ? t('common.saving')
              : mode === 'create'
                ? t('platformShell.kbSettings.members.dialog.addMember')
                : t('platformShell.kbSettings.members.dialog.saveRole')}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  )
}

function KnowledgeBaseMemberFormDialog({
  open,
  mode,
  member,
  submitting,
  onOpenChange,
  onSubmit,
}: KnowledgeBaseMemberFormDialogProps) {
  const formKey = `${mode}-${member?.membership_id ?? 'new'}`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <KnowledgeBaseMemberFormDialogContent
          key={formKey}
          mode={mode}
          member={member}
          submitting={submitting}
          onSubmit={onSubmit}
          onCancel={() => onOpenChange(false)}
        />
      ) : null}
    </Dialog>
  )
}

interface KnowledgeBaseMetadataEditorProps {
  knowledgeBase: KnowledgeBaseRecord
  submitting: boolean
  onSubmit: (payload: KnowledgeBaseUpdateRequest) => Promise<void>
}

function KnowledgeBaseMetadataEditor({
  knowledgeBase,
  submitting,
  onSubmit,
}: KnowledgeBaseMetadataEditorProps) {
  const { t } = useTranslation()
  const [name, setName] = useState(knowledgeBase.name || '')
  const [description, setDescription] = useState(knowledgeBase.description || '')
  const [status, setStatus] = useState(knowledgeBase.status || 'active')
  const [configOverrideText, setConfigOverrideText] = useState(
    Object.keys(knowledgeBase.config_override || {}).length > 0
      ? JSON.stringify(knowledgeBase.config_override || {}, null, 2)
      : defaultConfigOverrideText
  )

  const initialName = knowledgeBase.name || ''
  const initialDescription = knowledgeBase.description || ''
  const initialStatus = knowledgeBase.status || 'active'
  const initialConfig = knowledgeBase.config_override || {}

  const handleReset = () => {
    setName(initialName)
    setDescription(initialDescription)
    setStatus(initialStatus)
    setConfigOverrideText(
      Object.keys(initialConfig).length > 0 ? JSON.stringify(initialConfig, null, 2) : defaultConfigOverrideText
    )
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    const trimmedName = name.trim()
    const trimmedDescription = description.trim()
    const trimmedStatus = status.trim()

    if (!trimmedName) {
      toast.error(t('platformShell.kbSettings.metadataEditor.errors.displayNameEmpty'))
      return
    }

    if (!trimmedStatus) {
      toast.error(t('platformShell.kbSettings.metadataEditor.errors.statusEmpty'))
      return
    }

    let parsedConfig: Record<string, unknown> = {}
    const normalizedConfigText = configOverrideText.trim()

    if (normalizedConfigText) {
      try {
        const parsed = JSON.parse(normalizedConfigText)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          toast.error(t('platformShell.workspaceDirectory.configOverrideObject'))
          return
        }
        parsedConfig = parsed as Record<string, unknown>
      } catch (parseError) {
        toast.error(t('platformShell.workspaceDirectory.invalidJsonConfigOverride', { error: errorMessage(parseError) }))
        return
      }
    }

    const payload: KnowledgeBaseUpdateRequest = {}

    if (trimmedName !== initialName) {
      payload.name = trimmedName
    }

    if (trimmedDescription !== initialDescription) {
      payload.description = trimmedDescription
    }

    if (trimmedStatus !== initialStatus) {
      payload.status = trimmedStatus
    }

    if (JSON.stringify(parsedConfig) !== JSON.stringify(initialConfig)) {
      payload.config_override = parsedConfig
    }

    if (Object.keys(payload).length === 0) {
      toast.message(t('platformShell.kbSettings.metadataEditor.noChanges'))
      return
    }

    await onSubmit(payload)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 rounded-2xl border border-border/70 bg-background/75 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="text-sm font-medium text-foreground">{t('platformShell.kbSettings.metadataEditor.title')}</p>
          <p className="text-sm leading-6 text-muted-foreground">
            {t('platformShell.kbSettings.metadataEditor.description')}
          </p>
        </div>
        <Badge variant="outline" className="rounded-full px-3 py-1 text-[11px] uppercase tracking-[0.12em]">
          {t('platformShell.kbSettings.metadataEditor.idBadge', { kbId: knowledgeBase.id })}
        </Badge>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <label htmlFor="kb-settings-name" className="text-sm font-medium text-foreground">
            {t('platformShell.workspaceDirectory.displayName')}
          </label>
          <Input
            id="kb-settings-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={submitting}
            className="h-11 rounded-xl border-border/70"
          />
        </div>
        <div className="space-y-2">
          <label htmlFor="kb-settings-status" className="text-sm font-medium text-foreground">
            {t('platformShell.common.status')}
          </label>
          <Input
            id="kb-settings-status"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            placeholder={t('platformShell.workspaceDirectory.statusPlaceholder')}
            disabled={submitting}
            className="h-11 rounded-xl border-border/70"
          />
          <p className="text-xs leading-5 text-muted-foreground">{t('platformShell.workspaceDirectory.statusHint')}</p>
        </div>
      </div>

      <div className="space-y-2">
        <label htmlFor="kb-settings-description" className="text-sm font-medium text-foreground">
          {t('platformShell.workspaceDirectory.descriptionLabel')}
        </label>
        <Textarea
          id="kb-settings-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={t('platformShell.workspaceDirectory.descriptionPlaceholder')}
          disabled={submitting}
          className="min-h-[100px] rounded-xl border-border/70"
        />
      </div>

      <div className="space-y-2">
        <label htmlFor="kb-settings-config-override" className="text-sm font-medium text-foreground">
          {t('platformShell.workspaceDirectory.configOverride')}
        </label>
        <Textarea
          id="kb-settings-config-override"
          value={configOverrideText}
          onChange={(event) => setConfigOverrideText(event.target.value)}
          spellCheck={false}
          disabled={submitting}
          className="min-h-[164px] rounded-xl border-border/70 font-mono text-xs leading-6"
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs leading-5 text-muted-foreground">
          {t('platformShell.kbSettings.metadataEditor.idFixedHint')}
        </p>
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={handleReset} disabled={submitting}>
            {t('platformShell.kbSettings.metadataEditor.reset')}
          </Button>
          <Button type="submit" disabled={submitting}>
            {submitting ? t('common.saving') : t('platformShell.kbSettings.metadataEditor.saveChanges')}
          </Button>
        </div>
      </div>
    </form>
  )
}

export default function KnowledgeBaseSettingsPage() {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships, username } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const workspaceRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )
  const canManageKnowledgeBaseMembers = hasPermission(effectiveRole, 'kb:manage_permissions')
  const canManageKnowledgeBaseSettings = hasPermission(effectiveRole, 'kb:manage_settings')
  const canManageWorkspaceMembers = hasPermission(workspaceRole, 'workspace:invite_member')

  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBaseRecord | null>(null)
  const [, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [members, setMembers] = useState<MembershipEntry[]>([])
  const [membersLoading, setMembersLoading] = useState(true)
  const [membersError, setMembersError] = useState<string | null>(null)
  const [memberDialogMode, setMemberDialogMode] = useState<'create' | 'edit'>('create')
  const [memberDialogOpen, setMemberDialogOpen] = useState(false)
  const [selectedMember, setSelectedMember] = useState<MembershipEntry | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<MembershipEntry | null>(null)
  const [memberSubmitting, setMemberSubmitting] = useState(false)
  const [metadataSubmitting, setMetadataSubmitting] = useState(false)

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
          const message = loadError instanceof Error ? loadError.message : t('platformShell.kbSettings.loadFailed')
          setError(message)
          setKnowledgeBase(null)
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    void loadKnowledgeBase()

    return () => {
      cancelled = true
    }
  }, [currentKnowledgeBaseId, currentWorkspaceId, t])

  const loadMembers = useCallback(async () => {
    if (!canManageKnowledgeBaseMembers) {
      setMembers([])
      setMembersError(null)
      setMembersLoading(false)
      return
    }

    try {
      setMembersLoading(true)
      setMembersError(null)
      const response = await listKnowledgeBaseMembers(currentWorkspaceId, currentKnowledgeBaseId)
      setMembers(response.members)
    } catch (loadError) {
      setMembersError(errorMessage(loadError))
      setMembers([])
    } finally {
      setMembersLoading(false)
    }
  }, [canManageKnowledgeBaseMembers, currentKnowledgeBaseId, currentWorkspaceId])

  useEffect(() => {
    void loadMembers()
  }, [loadMembers])

  const overrides = useMemo(
    () => Object.entries(knowledgeBase?.config_override || {}),
    [knowledgeBase?.config_override]
  )

  const roleCounts = useMemo(() => {
    return members.reduce<Record<string, number>>((accumulator, member) => {
      accumulator[member.role] = (accumulator[member.role] || 0) + 1
      return accumulator
    }, {})
  }, [members])

  const hasCurrentUserAssignment = useMemo(
    () => members.some((member) => member.username === username),
    [members, username]
  )

  const openCreateDialog = () => {
    setMemberDialogMode('create')
    setSelectedMember(null)
    setMemberDialogOpen(true)
  }

  const openEditDialog = (member: MembershipEntry) => {
    setMemberDialogMode('edit')
    setSelectedMember(member)
    setMemberDialogOpen(true)
  }

  const handleMemberSubmit = async (payload: { username?: string; role: AccessRole }) => {
    try {
      setMemberSubmitting(true)
      if (memberDialogMode === 'create') {
        if (!payload.username) {
          toast.error(t('platformShell.kbSettings.members.usernameRequired'))
          return
        }
        const response = await createKnowledgeBaseMember(currentWorkspaceId, currentKnowledgeBaseId, {
          username: payload.username,
          role: payload.role,
        })
        toast.success(response.message)
      } else if (selectedMember) {
        const response = await updateKnowledgeBaseMemberRole(
          currentWorkspaceId,
          currentKnowledgeBaseId,
          selectedMember.user_id,
          payload.role
        )
        toast.success(response.message)
      }

      setMemberDialogOpen(false)
      setSelectedMember(null)
      await loadMembers()
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setMemberSubmitting(false)
    }
  }

  const handleDeleteMember = async () => {
    if (!deleteTarget) {
      return
    }

    try {
      setMemberSubmitting(true)
      const response = await deleteKnowledgeBaseMember(
        currentWorkspaceId,
        currentKnowledgeBaseId,
        deleteTarget.user_id
      )
      toast.success(response.message)
      setDeleteTarget(null)
      await loadMembers()
    } catch (deleteError) {
      toast.error(errorMessage(deleteError))
    } finally {
      setMemberSubmitting(false)
    }
  }

  const handleMetadataSubmit = async (payload: KnowledgeBaseUpdateRequest) => {
    try {
      setMetadataSubmitting(true)
      const response = await updateKnowledgeBase(currentWorkspaceId, currentKnowledgeBaseId, payload)
      setKnowledgeBase(response.kb)
      toast.success(response.message)
      window.dispatchEvent(
        new CustomEvent('lightrag:kb-updated', {
          detail: {
            workspaceId: currentWorkspaceId,
            kbId: currentKnowledgeBaseId,
          },
        })
      )
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setMetadataSubmitting(false)
    }
  }

  const metadataConfigKey = JSON.stringify(knowledgeBase?.config_override || {})
  const metadataEditorKey = knowledgeBase
    ? `${knowledgeBase.id}:${knowledgeBase.name}:${knowledgeBase.description}:${knowledgeBase.status}:${metadataConfigKey}`
    : 'kb-metadata-form'

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
              {t('platformShell.common.kbSettings')}
            </Badge>
            <span className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
              {currentWorkspaceId} / {knowledgeBase?.name || currentKnowledgeBaseId}
            </span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.kbSettings.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.kbSettings.description')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
              {t('platformShell.common.openDocuments')}
            </Link>
          </Button>
          {canManageWorkspaceMembers && (
            <Button variant="outline" size="sm" asChild>
              <Link to={appRoutes.workspaceMembers(currentWorkspaceId)}>
                {t('platformShell.kbSettings.openWorkspaceMembers')}
              </Link>
            </Button>
          )}
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
            {t('platformShell.kbSettings.effectiveAccessTitle')}
          </span>
          <span className="text-muted-foreground">·</span>
          <span className="truncate text-muted-foreground">
            {t(roleDescriptionKeys[effectiveRole])}
          </span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {summarizeRoleCapabilityKeys(effectiveRole).map((capabilityKey) => (
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
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-6 py-6">
          {error && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.kbSettings.registryUnavailableTitle')}
              </AlertTitle>
              <AlertDescription>
                {t('platformShell.kbSettings.registryUnavailableDescription')}
              </AlertDescription>
            </Alert>
          )}

          {/* Metadata editor — the primary reason this page exists */}
          <section className="space-y-3">
            <div className="flex items-center gap-2">
              <Settings2Icon
                className="size-4 text-emerald-600 dark:text-emerald-400"
                aria-hidden="true"
              />
              <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                {t('platformShell.kbSettings.metadataAndOverridesTitle')}
              </h2>
              {overrides.length > 0 && (
                <Badge
                  variant="outline"
                  className="rounded-full px-2 py-0.5 text-[10px]"
                >
                  {overrides.length}{' '}
                  {t('platformShell.kbSettings.overrides', { defaultValue: 'overrides' })}
                </Badge>
              )}
            </div>

            {knowledgeBase ? (
              canManageKnowledgeBaseSettings ? (
                <KnowledgeBaseMetadataEditor
                  key={metadataEditorKey}
                  knowledgeBase={knowledgeBase}
                  submitting={metadataSubmitting}
                  onSubmit={handleMetadataSubmit}
                />
              ) : (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>
                    {t('platformShell.kbSettings.metadataReadOnlyTitle')}
                  </AlertTitle>
                  <AlertDescription>
                    {t('platformShell.kbSettings.metadataReadOnlyDescription')}
                  </AlertDescription>
                </Alert>
              )
            ) : (
              <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                {t('platformShell.kbSettings.metadataPlaceholder')}
              </div>
            )}
          </section>

          {/* KB-scoped members — only when the caller can manage them */}
          {canManageKnowledgeBaseMembers ? (
            <section className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <UsersIcon
                  className="size-4 text-emerald-600 dark:text-emerald-400"
                  aria-hidden="true"
                />
                <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.kbSettings.memberAssignmentsTitle')}
                </h2>
                {!membersLoading && (
                  <Badge
                    variant="outline"
                    className="rounded-full px-2 py-0.5 text-[10px]"
                  >
                    {members.length}
                  </Badge>
                )}
                <div className="ml-auto flex flex-wrap items-center gap-1.5">
                  {membershipRoles
                    .filter((entryRole) => (roleCounts[entryRole] || 0) > 0)
                    .map((entryRole) => (
                      <div
                        key={entryRole}
                        className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-background/70 px-2.5 py-0.5"
                      >
                        <AccessBadge role={entryRole} compact />
                        <span className="text-xs font-semibold text-foreground">
                          {roleCounts[entryRole] || 0}
                        </span>
                      </div>
                    ))}
                  <Button size="sm" onClick={openCreateDialog}>
                    <UserPlusIcon className="size-4" />
                    {t('platformShell.kbSettings.members.dialog.addMember')}
                  </Button>
                </div>
              </div>

              {membersError && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>
                    {t('platformShell.kbSettings.memberManagementUnavailableTitle')}
                  </AlertTitle>
                  <AlertDescription>
                    {t('platformShell.kbSettings.memberManagementUnavailableDescription')}
                  </AlertDescription>
                </Alert>
              )}

              {membersLoading ? (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                  {t('platformShell.kbSettings.loadingMembers')}
                </div>
              ) : members.length > 0 ? (
                <div className="rounded-xl border border-border/60 bg-background/70">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t('platformShell.kbSettings.table.user')}</TableHead>
                        <TableHead>{t('platformShell.kbSettings.table.role')}</TableHead>
                        <TableHead>{t('platformShell.kbSettings.table.source')}</TableHead>
                        <TableHead>{t('platformShell.kbSettings.table.updated')}</TableHead>
                        <TableHead className="text-right">
                          {t('platformShell.kbSettings.table.actions')}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {members.map((member) => {
                        const isCurrentUser =
                          Boolean(username) && member.username === username
                        return (
                          <TableRow key={member.membership_id}>
                            <TableCell className="font-medium">
                              <div className="flex flex-wrap items-center gap-2">
                                <span>{member.username}</span>
                                {isCurrentUser && (
                                  <Badge
                                    variant="outline"
                                    className="rounded-full px-2.5 py-0.5 text-[11px]"
                                  >
                                    {t('platformShell.kbSettings.currentSession')}
                                  </Badge>
                                )}
                              </div>
                            </TableCell>
                            <TableCell>
                              <AccessBadge role={member.role} compact />
                            </TableCell>
                            <TableCell className="text-muted-foreground">
                              {member.source}
                            </TableCell>
                            <TableCell className="text-muted-foreground">
                              {member.updated_at}
                            </TableCell>
                            <TableCell className="text-right">
                              <div className="flex items-center justify-end gap-2">
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() => openEditDialog(member)}
                                  disabled={isCurrentUser}
                                >
                                  {t('platformShell.common.edit')}
                                </Button>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
                                  onClick={() => setDeleteTarget(member)}
                                  disabled={isCurrentUser}
                                >
                                  {t('platformShell.common.remove')}
                                </Button>
                              </div>
                            </TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                  {hasCurrentUserAssignment && (
                    <div className="border-t border-border/60 px-4 py-2 text-xs leading-5 text-muted-foreground">
                      {t('platformShell.kbSettings.currentSessionLocked')}
                    </div>
                  )}
                </div>
              ) : (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
                  {t('platformShell.kbSettings.noMembers')}
                </div>
              )}
            </section>
          ) : null}
        </div>
      </div>

      <KnowledgeBaseMemberFormDialog
        open={memberDialogOpen}
        mode={memberDialogMode}
        member={selectedMember}
        submitting={memberSubmitting}
        onOpenChange={setMemberDialogOpen}
        onSubmit={handleMemberSubmit}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent className="rounded-[1.5rem] border-border/70">
          <AlertDialogHeader>
            <AlertDialogTitle>{t('platformShell.kbSettings.removeDialog.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTarget
                ? t('platformShell.kbSettings.removeDialog.descriptionWithUser', {
                  username: deleteTarget.username,
                  knowledgeBaseName: knowledgeBase?.name || currentKnowledgeBaseId,
                })
                : t('platformShell.kbSettings.removeDialog.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={memberSubmitting}>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteMember}
              disabled={memberSubmitting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {memberSubmitting ? t('platformShell.common.removing') : t('platformShell.kbSettings.members.removeMember')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
