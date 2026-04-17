import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ShieldCheckIcon, UserPlusIcon, UsersIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { appRoutes } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import {
  createWorkspaceMember,
  deleteWorkspaceMember,
  listWorkspaceMembers,
  type MembershipEntry,
  updateWorkspaceMemberRole,
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
import AccessBadge from '@/components/navigation/AccessBadge'

const membershipRoles: AccessRole[] = ['owner', 'admin', 'editor', 'viewer']

interface MemberFormDialogProps {
  open: boolean
  mode: 'create' | 'edit'
  member: MembershipEntry | null
  submitting: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (payload: { username?: string; role: AccessRole }) => Promise<void>
}

interface MemberFormDialogContentProps {
  mode: 'create' | 'edit'
  member: MembershipEntry | null
  submitting: boolean
  onSubmit: (payload: { username?: string; role: AccessRole }) => Promise<void>
  onCancel: () => void
}

function MemberFormDialogContent({
  mode,
  member,
  submitting,
  onSubmit,
  onCancel,
}: MemberFormDialogContentProps) {
  const { t } = useTranslation()
  const [username, setUsername] = useState(mode === 'edit' ? member?.username || '' : '')
  const [role, setRole] = useState<AccessRole>(mode === 'edit' ? (member?.role || 'viewer') : 'viewer')

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (mode === 'create' && !username.trim()) {
      toast.error(t('platformShell.workspaceMembers.enterUsername'))
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
            ? t('platformShell.workspaceMembers.dialog.createTitle')
            : t('platformShell.workspaceMembers.dialog.editTitle')}
        </DialogTitle>
        <DialogDescription>
          {mode === 'create'
            ? t('platformShell.workspaceMembers.dialog.createDescription')
            : t('platformShell.workspaceMembers.dialog.editDescription')}
        </DialogDescription>
      </DialogHeader>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="space-y-2">
          <label htmlFor="workspace-member-username" className="text-sm font-medium text-foreground">
            {t('platformShell.workspaceMembers.username')}
          </label>
          <Input
            id="workspace-member-username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder={t('platformShell.workspaceMembers.usernamePlaceholder')}
            disabled={mode === 'edit' || submitting}
            className="h-11 rounded-xl border-border/70"
          />
          {mode === 'create' && (
            <p className="text-xs leading-5 text-muted-foreground">
              {t('platformShell.workspaceMembers.dialog.usernameHint')}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <label htmlFor="workspace-member-role" className="text-sm font-medium text-foreground">
            {t('platformShell.workspaceMembers.workspaceRole')}
          </label>
          <Select
            value={role}
            onValueChange={(value) => setRole(value as AccessRole)}
            disabled={submitting}
          >
            <SelectTrigger id="workspace-member-role" className="h-11 rounded-xl border-border/70">
              <SelectValue placeholder={t('platformShell.workspaceMembers.selectRole')} />
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
                ? t('platformShell.workspaceMembers.addMember')
                : t('platformShell.workspaceMembers.saveRole')}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  )
}

function MemberFormDialog({
  open,
  mode,
  member,
  submitting,
  onOpenChange,
  onSubmit,
}: MemberFormDialogProps) {
  const formKey = `${mode}-${member?.membership_id ?? 'new'}`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <MemberFormDialogContent
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

export default function MembersPage() {
  const { t } = useTranslation()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )
  const canManageMembers = hasPermission(effectiveRole, 'workspace:invite_member')

  const [members, setMembers] = useState<MembershipEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [dialogMode, setDialogMode] = useState<'create' | 'edit'>('create')
  const [memberDialogOpen, setMemberDialogOpen] = useState(false)
  const [selectedMember, setSelectedMember] = useState<MembershipEntry | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<MembershipEntry | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const loadMembers = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await listWorkspaceMembers(currentWorkspaceId)
      setMembers(response.members)
    } catch (loadError) {
      setError(errorMessage(loadError))
      setMembers([])
    } finally {
      setLoading(false)
    }
  }, [currentWorkspaceId])

  useEffect(() => {
    void loadMembers()
  }, [loadMembers])

  const roleCounts = useMemo(() => {
    return members.reduce<Record<string, number>>((accumulator, member) => {
      accumulator[member.role] = (accumulator[member.role] || 0) + 1
      return accumulator
    }, {})
  }, [members])

  const openCreateDialog = () => {
    setDialogMode('create')
    setSelectedMember(null)
    setMemberDialogOpen(true)
  }

  const openEditDialog = (member: MembershipEntry) => {
    setDialogMode('edit')
    setSelectedMember(member)
    setMemberDialogOpen(true)
  }

  const handleMemberSubmit = async (payload: { username?: string; role: AccessRole }) => {
    try {
      setSubmitting(true)
      if (dialogMode === 'create') {
        if (!payload.username) {
          toast.error(t('platformShell.workspaceMembers.usernameRequired'))
          return
        }
        const response = await createWorkspaceMember(currentWorkspaceId, {
          username: payload.username,
          role: payload.role,
        })
        toast.success(response.message)
      } else if (selectedMember) {
        const response = await updateWorkspaceMemberRole(
          currentWorkspaceId,
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
      setSubmitting(false)
    }
  }

  const handleDeleteMember = async () => {
    if (!deleteTarget) {
      return
    }

    try {
      setSubmitting(true)
      const response = await deleteWorkspaceMember(currentWorkspaceId, deleteTarget.user_id)
      toast.success(response.message)
      setDeleteTarget(null)
      await loadMembers()
    } catch (deleteError) {
      toast.error(errorMessage(deleteError))
    } finally {
      setSubmitting(false)
    }
  }

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
              {t('platformShell.workspaceMembers.badge')}
            </Badge>
            <span className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
              {currentWorkspaceId}
            </span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.workspaceMembers.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.workspaceMembers.description')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={appRoutes.workspaceSettings(currentWorkspaceId)}>
              {t('platformShell.workspaceMembers.openWorkspaceSettings')}
            </Link>
          </Button>
          {canManageMembers && (
            <Button size="sm" onClick={openCreateDialog}>
              <UserPlusIcon className="size-4" />
              {t('platformShell.workspaceMembers.addMember')}
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
            {t('platformShell.workspaceMembers.roleSummary')}
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
        <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 px-6 py-6">
          {/* Role-count strip — only roles with ≥1 member, inline, no */}
          {/* giant card. */}
          {!loading && members.length > 0 && (
            <div className="flex items-center gap-2">
              <UsersIcon
                className="size-4 text-emerald-600 dark:text-emerald-400"
                aria-hidden="true"
              />
              <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                {t('platformShell.workspaceMembers.workspaceRoster')}
              </h2>
              <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[10px]">
                {members.length}
              </Badge>
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
              </div>
            </div>
          )}

          {error && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.workspaceMembers.unavailableTitle')}
              </AlertTitle>
              <AlertDescription>
                {t('platformShell.workspaceMembers.unavailableDescription')}
              </AlertDescription>
            </Alert>
          )}

          {!canManageMembers && !error && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>{t('platformShell.workspaceMembers.readOnlyTitle')}</AlertTitle>
              <AlertDescription>
                {t('platformShell.workspaceMembers.readOnlyDescription')}
              </AlertDescription>
            </Alert>
          )}

          {loading ? (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
              {t('platformShell.workspaceMembers.loadingMembers')}
            </div>
          ) : members.length > 0 ? (
            <div className="rounded-xl border border-border/60 bg-background/70">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('platformShell.workspaceMembers.table.user')}</TableHead>
                    <TableHead>{t('platformShell.workspaceMembers.table.role')}</TableHead>
                    <TableHead>{t('platformShell.workspaceMembers.table.source')}</TableHead>
                    <TableHead>{t('platformShell.workspaceMembers.table.updated')}</TableHead>
                    {canManageMembers && (
                      <TableHead className="text-right">
                        {t('platformShell.workspaceMembers.table.actions')}
                      </TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {members.map((member) => (
                    <TableRow key={member.membership_id}>
                      <TableCell className="font-medium">{member.username}</TableCell>
                      <TableCell>
                        <AccessBadge role={member.role} compact />
                      </TableCell>
                      <TableCell className="text-muted-foreground">{member.source}</TableCell>
                      <TableCell className="text-muted-foreground">{member.updated_at}</TableCell>
                      {canManageMembers && (
                        <TableCell className="text-right">
                          <div className="flex items-center justify-end gap-2">
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => openEditDialog(member)}
                            >
                              {t('platformShell.common.edit')}
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
                              onClick={() => setDeleteTarget(member)}
                            >
                              {t('platformShell.common.remove')}
                            </Button>
                          </div>
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
              {t('platformShell.workspaceMembers.empty')}
            </div>
          )}
        </div>
      </div>

      <MemberFormDialog
        open={memberDialogOpen}
        mode={dialogMode}
        member={selectedMember}
        submitting={submitting}
        onOpenChange={setMemberDialogOpen}
        onSubmit={handleMemberSubmit}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent className="rounded-[1.5rem] border-border/70">
          <AlertDialogHeader>
            <AlertDialogTitle>{t('platformShell.workspaceMembers.removeDialog.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTarget
                ? t('platformShell.workspaceMembers.removeDialog.descriptionWithUser', {
                  username: deleteTarget.username,
                  workspaceId: currentWorkspaceId,
                })
                : t('platformShell.workspaceMembers.removeDialog.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submitting}>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteMember}
              disabled={submitting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {submitting ? t('platformShell.common.removing') : t('platformShell.workspaceMembers.removeMember')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
