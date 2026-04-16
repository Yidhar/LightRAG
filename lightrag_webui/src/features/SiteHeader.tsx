import Button from '@/components/ui/Button'
import Badge from '@/components/ui/Badge'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { SiteInfo } from '@/lib/constants'
import AppSettings from '@/components/AppSettings'
import { useAuthStore } from '@/stores/state'
import { useTranslation } from 'react-i18next'
import { navigationService } from '@/services/navigation'
import {
  BookOpenTextIcon,
  BriefcaseBusinessIcon,
  ChevronDownIcon,
  GithubIcon,
  LogOutIcon,
  UserCircle2Icon,
  ZapIcon,
} from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/Tooltip'
import { Link, useParams } from 'react-router-dom'
import AppPrimaryNav from '@/components/navigation/AppPrimaryNav'
import WorkspaceSwitcher from '@/components/navigation/WorkspaceSwitcher'
import KnowledgeBaseSwitcher from '@/components/navigation/KnowledgeBaseSwitcher'
import ContextBreadcrumbs from '@/components/navigation/ContextBreadcrumbs'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { resolveEffectiveRole } from '@/lib/permissions'
import AccessBadge from '@/components/navigation/AccessBadge'

export default function SiteHeader() {
  const { t } = useTranslation()
  const {
    isAuthenticated,
    coreVersion,
    apiVersion,
    username,
    webuiTitle,
    webuiDescription,
    role,
    memberships,
  } = useAuthStore()
  const { workspaceId, kbId } = useParams()

  const versionDisplay = coreVersion && apiVersion ? `${coreVersion}/${apiVersion}` : null

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )

  const hasWarning = apiVersion?.endsWith('⚠️')
  const versionTooltip = hasWarning ? t('header.frontendNeedsRebuild') : versionDisplay ? `v${versionDisplay}` : ''

  const handleLogout = () => {
    navigationService.navigateToLogin()
  }

  return (
    <TooltipProvider>
      <header className="surface-glass sticky top-0 z-50 w-full border-b px-3 py-3 sm:px-4">
        <div className="flex min-h-16 items-center gap-3">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <Link
              to={appRoutes.kbOverview(currentWorkspaceId, currentKnowledgeBaseId)}
              className="flex min-w-0 items-center gap-3 rounded-[24px] border border-border/70 bg-background/70 px-3 py-2 shadow-sm transition-colors hover:border-emerald-500/20 hover:bg-emerald-500/[0.04]"
            >
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-emerald-500/12 text-emerald-600 dark:bg-emerald-500/16 dark:text-emerald-300">
                <ZapIcon className="brand-glow size-5" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate font-semibold tracking-tight text-foreground">{t('brand.name')}</span>
                  <AccessBadge role={effectiveRole} compact className="hidden xl:flex" />
                </div>
                <p className="truncate text-xs text-muted-foreground">
                  {webuiTitle || webuiDescription || `${currentWorkspaceId} / ${currentKnowledgeBaseId}`}
                </p>
              </div>
            </Link>

            <div className="hidden min-w-0 flex-1 items-center gap-3 2xl:flex">
              <WorkspaceSwitcher className="min-w-[13rem]" />
              <KnowledgeBaseSwitcher className="min-w-[14rem]" />
            </div>
          </div>

          <div className="hidden min-w-0 flex-1 items-center justify-center lg:flex">
            <ContextBreadcrumbs />
          </div>

          <div className="hidden min-w-0 items-center justify-center md:flex lg:hidden">
            <AppPrimaryNav />
          </div>

          <nav className="flex items-center justify-end gap-1.5">
            <AccessBadge role={effectiveRole} className="hidden md:flex xl:hidden" />

            <div className="hidden items-center gap-2 xl:flex 2xl:hidden">
              <Badge variant="outline" className="rounded-full bg-background/70 px-3 py-1 text-xs">
                <BriefcaseBusinessIcon className="mr-1 size-3.5" />
                {currentWorkspaceId}
              </Badge>
              <Badge variant="outline" className="rounded-full bg-background/70 px-3 py-1 text-xs">
                <BookOpenTextIcon className="mr-1 size-3.5" />
                {currentKnowledgeBaseId}
              </Badge>
            </div>

            {versionDisplay && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Badge variant="outline" className="cursor-default rounded-full bg-muted/70 px-3 py-1 text-xs">
                    v{versionDisplay}
                  </Badge>
                </TooltipTrigger>
                <TooltipContent side="bottom">{versionTooltip}</TooltipContent>
              </Tooltip>
            )}

            <Button
              variant="ghost"
              size="icon"
              side="bottom"
              tooltip={t('header.projectRepository')}
              className="rounded-full"
              asChild
            >
              <a href={SiteInfo.github} target="_blank" rel="noopener noreferrer">
                <GithubIcon className="size-4" aria-hidden="true" />
              </a>
            </Button>

            <AppSettings className="rounded-full" />

            {isAuthenticated && (
              <Popover>
                <PopoverTrigger asChild>
                  <Button variant="outline" className="rounded-full border-border/70 bg-background/80 px-3">
                    <UserCircle2Icon className="size-4" />
                    <span className="hidden max-w-[9rem] truncate text-sm sm:inline">{username}</span>
                    <ChevronDownIcon className="size-4 text-muted-foreground" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-[320px] rounded-[24px] border-border/70 p-0 shadow-xl">
                  <div className="border-b border-border/60 px-4 py-4">
                    <div className="flex items-start gap-3">
                      <div className="flex size-11 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                        <UserCircle2Icon className="size-5" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-base font-semibold text-foreground">{username}</p>
                        <p className="truncate text-sm text-muted-foreground">
                          {webuiTitle || t('brand.name')}
                        </p>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <AccessBadge role={effectiveRole} compact />
                          <Badge variant="outline" className="rounded-full bg-muted/20 px-3 py-1 text-xs">
                            {currentWorkspaceId}
                          </Badge>
                          <Badge variant="outline" className="rounded-full bg-muted/20 px-3 py-1 text-xs">
                            {currentKnowledgeBaseId}
                          </Badge>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="grid gap-3 px-4 py-4">
                    <div className="rounded-[20px] border border-border/70 bg-muted/20 p-3">
                      <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                        {t('platformShell.common.workspace')}
                      </p>
                      <p className="mt-1 text-sm font-medium text-foreground">{currentWorkspaceId}</p>
                    </div>
                    <div className="rounded-[20px] border border-border/70 bg-muted/20 p-3">
                      <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                        {t('platformShell.common.knowledgeBase')}
                      </p>
                      <p className="mt-1 text-sm font-medium text-foreground">{currentKnowledgeBaseId}</p>
                    </div>
                    <Button variant="outline" className="w-full justify-center rounded-full" onClick={handleLogout}>
                      <LogOutIcon className="size-4" />
                      {t('header.logout')}
                    </Button>
                  </div>
                </PopoverContent>
              </Popover>
            )}
          </nav>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2 md:hidden">
          <Badge variant="outline" className="rounded-full bg-background/70 px-3 py-1 text-xs">
            <BriefcaseBusinessIcon className="mr-1 size-3.5" />
            {currentWorkspaceId}
          </Badge>
          <Badge variant="outline" className="rounded-full bg-background/70 px-3 py-1 text-xs">
            <BookOpenTextIcon className="mr-1 size-3.5" />
            {currentKnowledgeBaseId}
          </Badge>
          <AccessBadge role={effectiveRole} compact />
        </div>
      </header>
    </TooltipProvider>
  )
}
