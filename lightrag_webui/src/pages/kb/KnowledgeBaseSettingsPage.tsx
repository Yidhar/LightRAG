import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Settings2Icon, ShieldCheckIcon, SlidersHorizontalIcon, UserPlusIcon, UsersIcon } from 'lucide-react'
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
  const [loading, setLoading] = useState(true)
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
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-6">
        <section className="surface-panel overflow-hidden rounded-[28px] border border-border/70 bg-gradient-to-br from-emerald-500/10 via-card to-card">
          <div className="grid gap-6 px-6 py-7 lg:grid-cols-[1.5fr_1fr] lg:px-8">
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                  {t('platformShell.common.kbSettings')}
                </Badge>
                <AccessBadge role={effectiveRole} />
              </div>
              <div className="space-y-2">
                <h1 className="text-3xl font-semibold tracking-tight text-foreground">
                  {t('platformShell.kbSettings.title')}
                </h1>
                <p className="max-w-2xl text-sm leading-7 text-muted-foreground">
                  {t('platformShell.kbSettings.description')}
                </p>
              </div>
              <div className="flex flex-wrap gap-3">
                <Button variant="outline" asChild>
                  <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
                    {t('platformShell.common.openDocuments')}
                  </Link>
                </Button>
                <Button variant="outline" asChild>
                  <Link to={appRoutes.workspaces}>{t('platformShell.kbSettings.manageDirectory')}</Link>
                </Button>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-2">
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.common.workspace')}</CardDescription>
                  <CardTitle className="text-xl">{currentWorkspaceId}</CardTitle>
                </CardHeader>
              </Card>
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.common.knowledgeBase')}</CardDescription>
                  <CardTitle className="text-xl">{knowledgeBase?.name || currentKnowledgeBaseId}</CardTitle>
                </CardHeader>
              </Card>
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.kbSettings.overrides')}</CardDescription>
                  <CardTitle className="text-xl">
                    {loading ? t('platformShell.common.loadingCount') : overrides.length}
                  </CardTitle>
                </CardHeader>
              </Card>
              <Card className="border-border/70 bg-background/75 shadow-none">
                <CardHeader className="pb-3">
                  <CardDescription>{t('platformShell.kbSettings.directMembers')}</CardDescription>
                  <CardTitle className="text-xl">
                    {canManageKnowledgeBaseMembers
                      ? membersLoading
                        ? t('platformShell.common.loadingCount')
                        : members.length
                      : t('platformShell.kbSettings.ownerOnly')}
                  </CardTitle>
                </CardHeader>
              </Card>
            </div>
          </div>
        </section>

        <section className="grid gap-6 xl:grid-cols-[1fr_1.45fr]">
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbSettings.effectiveAccessTitle')}
              </CardTitle>
              <CardDescription>{t('platformShell.kbSettings.effectiveAccessDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {summarizeRoleCapabilityKeys(effectiveRole).map((capabilityKey) => (
                  <Badge key={capabilityKey} variant="outline" className="rounded-full bg-muted/40 px-3 py-1">
                    {t(capabilityKey)}
                  </Badge>
                ))}
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-border/70 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.kbSettings.kbRole')}
                  </p>
                  <div className="mt-2">
                    <AccessBadge role={effectiveRole} compact />
                  </div>
                </div>
                <div className="rounded-2xl border border-border/70 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.kbSettings.workspaceFallback')}
                  </p>
                  <div className="mt-2">
                    <AccessBadge role={workspaceRole} compact />
                  </div>
                </div>
              </div>

              <div className="rounded-2xl border border-border/70 bg-muted/20 p-4">
                <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  {t('platformShell.kbSettings.currentStatus')}
                </p>
                <p className="mt-2 text-sm font-medium text-foreground">
                  {loading
                    ? t('platformShell.kbSettings.loadingMetadata')
                    : knowledgeBase?.status || t('platformShell.kbSettings.compatibilityMode')}
                </p>
              </div>
            </CardContent>
          </Card>

          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Settings2Icon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbSettings.metadataAndOverridesTitle')}
              </CardTitle>
              <CardDescription>{t('platformShell.kbSettings.metadataAndOverridesDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {error && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.kbSettings.registryUnavailableTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.kbSettings.registryUnavailableDescription')}</AlertDescription>
                </Alert>
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.workspaceDirectory.kbId')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">{currentKnowledgeBaseId}</p>
                </div>
                <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.workspaceDirectory.displayName')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {knowledgeBase?.name || currentKnowledgeBaseId}
                  </p>
                </div>
                <div className="rounded-2xl border border-border/70 bg-background/75 p-4 sm:col-span-2">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.workspaceDirectory.descriptionLabel')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {knowledgeBase?.description || t('platformShell.kbSettings.noDescription')}
                  </p>
                </div>
                <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.kbSettings.createdAt')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {knowledgeBase?.created_at || t('platformShell.kbSettings.notExposedYet')}
                  </p>
                </div>
                <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {t('platformShell.kbSettings.lifecycleStatus')}
                  </p>
                  <p className="mt-2 text-sm font-medium text-foreground">
                    {knowledgeBase?.status || t('platformShell.kbSettings.compatibilityMode')}
                  </p>
                </div>
              </div>

              <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                <div className="mb-3 flex items-center gap-2">
                  <SlidersHorizontalIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  <p className="font-medium text-foreground">{t('platformShell.kbSettings.configOverrideKeys')}</p>
                </div>

                {overrides.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {overrides.map(([key, value]) => (
                      <Badge key={key} variant="outline" className="rounded-full px-3 py-1">
                        <span className="mr-2 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                          {key}
                        </span>
                        <span className="font-medium text-foreground">{String(value)}</span>
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm leading-6 text-muted-foreground">{t('platformShell.kbSettings.noOverrides')}</p>
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
                    <AlertTitle>{t('platformShell.kbSettings.metadataReadOnlyTitle')}</AlertTitle>
                    <AlertDescription>{t('platformShell.kbSettings.metadataReadOnlyDescription')}</AlertDescription>
                  </Alert>
                )
              ) : (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.kbSettings.metadataPlaceholder')}
                </div>
              )}
            </CardContent>
          </Card>
        </section>

        <section className="grid gap-6 xl:grid-cols-[1fr_1.45fr]">
          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbSettings.permissionScopeTitle')}
              </CardTitle>
              <CardDescription>{t(roleDescriptionKeys[effectiveRole])}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {canManageKnowledgeBaseMembers ? (
                <div className="grid gap-3 sm:grid-cols-2">
                  {membershipRoles.map((entryRole) => (
                    <div key={entryRole} className="rounded-2xl border border-border/70 bg-muted/20 p-4">
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <AccessBadge role={entryRole} compact />
                        <span className="text-sm font-semibold text-foreground">{roleCounts[entryRole] || 0}</span>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {t('platformShell.kbSettings.directAssignmentsUsingRole')}
                      </p>
                    </div>
                  ))}
                </div>
              ) : (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.kbSettings.permissionsOwnerManagedTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.kbSettings.permissionsOwnerManagedDescription')}</AlertDescription>
                </Alert>
              )}

              <div className="rounded-2xl border border-border/70 bg-background/75 p-4">
                <p className="text-sm font-medium text-foreground">{t('platformShell.kbSettings.permissionLayeringTitle')}</p>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  {t('platformShell.kbSettings.permissionLayeringDescription')}
                </p>
              </div>

              {canManageWorkspaceMembers && (
                <Button variant="outline" asChild>
                  <Link to={appRoutes.workspaceMembers(currentWorkspaceId)}>
                    {t('platformShell.kbSettings.openWorkspaceMembers')}
                  </Link>
                </Button>
              )}
            </CardContent>
          </Card>

          <Card className="border-border/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <UsersIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                {t('platformShell.kbSettings.memberAssignmentsTitle')}
              </CardTitle>
              <CardDescription>{t('platformShell.kbSettings.memberAssignmentsDescription')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {canManageKnowledgeBaseMembers ? (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border/70 bg-background/75 p-4">
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-foreground">
                      {t('platformShell.kbSettings.directAssignmentManagementTitle')}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {t('platformShell.kbSettings.directAssignmentManagementDescription')}
                    </p>
                  </div>
                  <Button onClick={openCreateDialog}>
                    <UserPlusIcon className="size-4" />
                    {t('platformShell.kbSettings.members.dialog.addMember')}
                  </Button>
                </div>
              ) : (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.kbSettings.memberEditingHidden')}
                </div>
              )}

              {membersError && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.kbSettings.memberManagementUnavailableTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.kbSettings.memberManagementUnavailableDescription')}</AlertDescription>
                </Alert>
              )}

              {canManageKnowledgeBaseMembers ? (
                membersLoading ? (
                  <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                    {t('platformShell.kbSettings.loadingMembers')}
                  </div>
                ) : members.length > 0 ? (
                  <div className="space-y-3 rounded-2xl border border-border/70 bg-background/70 p-0">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>{t('platformShell.kbSettings.table.user')}</TableHead>
                          <TableHead>{t('platformShell.kbSettings.table.role')}</TableHead>
                          <TableHead>{t('platformShell.kbSettings.table.source')}</TableHead>
                          <TableHead>{t('platformShell.kbSettings.table.updated')}</TableHead>
                          <TableHead className="text-right">{t('platformShell.kbSettings.table.actions')}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {members.map((member) => {
                          const isCurrentUser = Boolean(username) && member.username === username

                          return (
                            <TableRow key={member.membership_id}>
                              <TableCell className="font-medium">
                                <div className="flex flex-wrap items-center gap-2">
                                  <span>{member.username}</span>
                                  {isCurrentUser && (
                                    <Badge variant="outline" className="rounded-full px-2.5 py-0.5 text-[11px]">
                                      {t('platformShell.kbSettings.currentSession')}
                                    </Badge>
                                  )}
                                </div>
                              </TableCell>
                              <TableCell>
                                <AccessBadge role={member.role} compact />
                              </TableCell>
                              <TableCell className="text-muted-foreground">{member.source}</TableCell>
                              <TableCell className="text-muted-foreground">{member.updated_at}</TableCell>
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
                      <div className="px-4 pb-4 text-xs leading-5 text-muted-foreground">
                        {t('platformShell.kbSettings.currentSessionLocked')}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                    {t('platformShell.kbSettings.noMembers')}
                  </div>
                )
              ) : null}
            </CardContent>
          </Card>
        </section>
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
