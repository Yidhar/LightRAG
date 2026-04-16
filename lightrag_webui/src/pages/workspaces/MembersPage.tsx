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
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6 px-4 py-6 sm:px-6">
        <section className="surface-panel overflow-hidden rounded-[32px] border border-border/70 bg-gradient-to-br from-emerald-500/10 via-card to-card">
          <div className="px-6 py-7 lg:px-8">
            <div className="space-y-6">
              <div className="space-y-5 rounded-[28px] border border-border/70 bg-background/80 p-6 shadow-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                    {t('platformShell.workspaceMembers.badge')}
                  </Badge>
                  <AccessBadge role={effectiveRole} />
                </div>

                <div className="space-y-2">
                  <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                    {t('platformShell.workspaceMembers.title')}
                  </h1>
                  <p className="max-w-3xl text-sm leading-7 text-muted-foreground sm:text-[15px]">
                    {t('platformShell.workspaceMembers.description')}
                  </p>
                </div>

                <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(300px,0.8fr)]">
                  <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                    <div className="mb-3 flex items-center gap-2">
                      <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                      <p className="font-medium text-foreground">{t('platformShell.workspaceMembers.roleSummary')}</p>
                    </div>
                    <p className="text-sm leading-6 text-muted-foreground">{t(roleDescriptionKeys[effectiveRole])}</p>

                    <div className="mt-5 flex flex-wrap gap-2">
                      {summarizeRoleCapabilityKeys(effectiveRole).map((capabilityKey) => (
                        <Badge key={capabilityKey} variant="outline" className="rounded-full bg-background/80 px-3 py-1">
                          {t(capabilityKey)}
                        </Badge>
                      ))}
                    </div>

                    <div className="mt-5 flex flex-wrap gap-2">
                      <Badge variant="outline" className="rounded-full px-3 py-1">
                        {t('platformShell.workspaceMembers.workspaceScope')}: {currentWorkspaceId}
                      </Badge>
                      <Badge variant="outline" className="rounded-full px-3 py-1">
                        {t('platformShell.workspaceMembers.loadedMembers')}:{' '}
                        {loading ? t('platformShell.common.loadingCount') : members.length}
                      </Badge>
                    </div>
                  </div>

                  <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                    <div className="mb-3 flex items-center gap-2">
                      <UsersIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                      <p className="font-medium text-foreground">{t('platformShell.workspaceMembers.workspaceRoster')}</p>
                    </div>
                    <p className="text-sm leading-6 text-muted-foreground">
                      {t('platformShell.workspaceMembers.workspaceRosterDescription')}
                    </p>

                    <div className="mt-5 flex flex-wrap gap-2">
                      {membershipRoles.map((entryRole) => (
                        <div
                          key={entryRole}
                          className="inline-flex items-center gap-2 rounded-full border border-border/70 bg-background/80 px-3 py-2"
                        >
                          <AccessBadge role={entryRole} compact />
                          <span className="text-sm font-semibold text-foreground">
                            {loading ? t('platformShell.common.loadingCount') : roleCounts[entryRole] || 0}
                          </span>
                        </div>
                      ))}
                    </div>

                    <div className="mt-5 flex flex-wrap gap-3">
                      <Button variant="outline" asChild>
                        <Link to={appRoutes.workspaceSettings(currentWorkspaceId)}>
                          {t('platformShell.workspaceMembers.openWorkspaceSettings')}
                        </Link>
                      </Button>
                      {canManageMembers && (
                        <Button onClick={openCreateDialog}>
                          <UserPlusIcon className="size-4" />
                          {t('platformShell.workspaceMembers.addMember')}
                        </Button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader className="gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="space-y-2">
                <CardTitle className="flex items-center gap-2">
                  <UsersIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  {t('platformShell.workspaceMembers.workspaceRoster')}
                </CardTitle>
                <CardDescription>
                  {t('platformShell.workspaceMembers.workspaceRosterDescription')}
                </CardDescription>
              </div>
              {canManageMembers && (
                <Button onClick={openCreateDialog}>
                  <UserPlusIcon className="size-4" />
                  {t('platformShell.workspaceMembers.addMember')}
                </Button>
              )}
            </CardHeader>
            <CardContent className="space-y-4">
              {error && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.workspaceMembers.unavailableTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.workspaceMembers.unavailableDescription')}</AlertDescription>
                </Alert>
              )}

              {!canManageMembers && (
                <Alert className="border-border/70 bg-muted/20">
                  <AlertTitle>{t('platformShell.workspaceMembers.readOnlyTitle')}</AlertTitle>
                  <AlertDescription>{t('platformShell.workspaceMembers.readOnlyDescription')}</AlertDescription>
                </Alert>
              )}

              {!loading && members.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {membershipRoles
                    .filter((entryRole) => (roleCounts[entryRole] || 0) > 0)
                    .map((entryRole) => (
                      <div
                        key={entryRole}
                        className="inline-flex items-center gap-2 rounded-full border border-border/70 bg-muted/20 px-3 py-2"
                      >
                        <AccessBadge role={entryRole} compact />
                        <span className="text-sm font-semibold text-foreground">{roleCounts[entryRole] || 0}</span>
                      </div>
                    ))}
                </div>
              )}

              {loading ? (
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.workspaceMembers.loadingMembers')}
                </div>
              ) : members.length > 0 ? (
                <div className="rounded-2xl border border-border/70 bg-background/70">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t('platformShell.workspaceMembers.table.user')}</TableHead>
                        <TableHead>{t('platformShell.workspaceMembers.table.role')}</TableHead>
                        <TableHead>{t('platformShell.workspaceMembers.table.source')}</TableHead>
                        <TableHead>{t('platformShell.workspaceMembers.table.updated')}</TableHead>
                        {canManageMembers && <TableHead className="text-right">{t('platformShell.workspaceMembers.table.actions')}</TableHead>}
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
                                <Button variant="outline" size="sm" onClick={() => openEditDialog(member)}>
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
                <div className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-sm text-muted-foreground">
                  {t('platformShell.workspaceMembers.empty')}
                </div>
              )}
            </CardContent>
          </Card>
        </section>
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
