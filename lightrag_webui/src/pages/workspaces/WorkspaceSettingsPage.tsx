import { Link, useParams } from 'react-router-dom'
import { FolderKanbanIcon, ShieldCheckIcon, UsersIcon, WorkflowIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { appRoutes } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { useAuthStore } from '@/stores/state'
import { resolveEffectiveRole, roleLabelKeys, summarizeRoleCapabilityKeys } from '@/lib/permissions'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import AccessBadge from '@/components/navigation/AccessBadge'

export default function WorkspaceSettingsPage() {
  const { t } = useTranslation()
  const { workspaceId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: null }
  )

  return (
    <div className="h-full overflow-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)]">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6 px-4 py-6 sm:px-6">
        <section className="surface-panel overflow-hidden rounded-[32px] border border-border/70 bg-gradient-to-br from-emerald-500/10 via-card to-card">
          <div className="px-6 py-7 lg:px-8">
            <div className="space-y-6">
              <div className="space-y-5 rounded-[28px] border border-border/70 bg-background/80 p-6 shadow-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                    {t('platformShell.workspaceSettings.badge')}
                  </Badge>
                  <AccessBadge role={effectiveRole} />
                </div>

                <div className="space-y-2">
                  <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                    {t('platformShell.workspaceSettings.title')}
                  </h1>
                  <p className="max-w-3xl text-sm leading-7 text-muted-foreground sm:text-[15px]">
                    {t('platformShell.workspaceSettings.description')}
                  </p>
                </div>

                <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(280px,0.75fr)]">
                  <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                    <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      {t('platformShell.workspaceSettings.workspaceScope')}
                    </p>
                    <p className="mt-2 text-3xl font-semibold tracking-tight text-foreground">{currentWorkspaceId}</p>

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
                        {t('platformShell.workspaceSettings.currentAccess')}
                      </p>
                      <p className="mt-2 text-2xl font-semibold text-foreground">
                        {t(roleLabelKeys[effectiveRole])}
                      </p>
                    </div>

                    <div className="rounded-[24px] border border-border/70 bg-muted/20 p-5">
                      <div className="mb-3 flex items-center gap-2">
                        <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                        <p className="font-medium text-foreground">{t('platformShell.workspaceSettings.accessPosture')}</p>
                      </div>
                      <p className="text-sm leading-6 text-muted-foreground">
                        {t('platformShell.workspaceSettings.accessPostureDescription')}
                      </p>
                    </div>
                  </div>
                </div>

                <Button variant="outline" asChild className="mt-5 w-full justify-between">
                  <Link to={appRoutes.workspaceMembers(currentWorkspaceId)}>
                    {t('platformShell.workspaceSettings.openMembers')}
                    <UsersIcon className="size-4" />
                  </Link>
                </Button>
              </div>
            </div>
          </div>
        </section>

        <section>
          <Card className="border-border/70">
            <CardHeader>
              <div className="flex flex-wrap items-center gap-2">
                <CardTitle className="flex items-center gap-2">
                  <WorkflowIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  {t('platformShell.workspaceSettings.whatLandsHereNext')}
                </CardTitle>
              </div>
              <CardDescription>
                {t('platformShell.workspaceSettings.whatLandsHereNextDescription')}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-[28px] border border-border/70 bg-background/75 p-6">
                <div className="mb-4 flex items-center gap-2">
                  <FolderKanbanIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  <p className="font-medium text-foreground">{t('platformShell.workspaceSettings.workspacePolicy')}</p>
                </div>
                <p className="text-sm leading-6 text-muted-foreground">
                  {t('platformShell.workspaceSettings.workspacePolicyDescription')}
                </p>
              </div>
              <div className="rounded-[28px] border border-border/70 bg-background/75 p-6">
                <div className="mb-4 flex items-center gap-2">
                  <ShieldCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
                  <p className="font-medium text-foreground">{t('platformShell.workspaceSettings.securityPosture')}</p>
                </div>
                <p className="text-sm leading-6 text-muted-foreground">
                  {t('platformShell.workspaceSettings.securityPostureDescription')}
                </p>
              </div>
            </CardContent>
          </Card>
        </section>
      </div>
    </div>
  )
}
