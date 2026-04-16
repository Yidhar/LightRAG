import Button from '@/components/ui/Button'
import Badge from '@/components/ui/Badge'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { SiteInfo } from '@/lib/constants'
import AppSettings from '@/components/AppSettings'
import { useAuthStore } from '@/stores/state'
import { useTranslation } from 'react-i18next'
import { navigationService } from '@/services/navigation'
import {
  ChevronDownIcon,
  GithubIcon,
  LogOutIcon,
  UserCircle2Icon,
  ZapIcon,
} from 'lucide-react'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/Tooltip'
import { Link, useParams } from 'react-router-dom'
import AppPrimaryNav from '@/components/navigation/AppPrimaryNav'
import WorkspaceSwitcher from '@/components/navigation/WorkspaceSwitcher'
import KnowledgeBaseSwitcher from '@/components/navigation/KnowledgeBaseSwitcher'
import ContextBreadcrumbs from '@/components/navigation/ContextBreadcrumbs'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { resolveEffectiveRole } from '@/lib/permissions'
import AccessBadge from '@/components/navigation/AccessBadge'

/**
 * Top-bar chrome.
 *
 * PR-UI-1 collapses three prior duplicate surfaces down to one:
 *
 *   - AccessBadge now appears once, inside the user popover.
 *   - workspace/KB identity is shown by exactly one location indicator
 *     per viewport band (switchers xl+, breadcrumbs lg-xl, primary nav md-lg).
 *   - mobile mt-3 duplicate strip is gone; the sidebar/drawer owns mobile nav.
 *
 * Header height is constrained to h-14 on every viewport. See
 * ``docs/platform-v2/ui-audit.md`` for the full rationale.
 */
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

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )

  const versionDisplay = coreVersion && apiVersion ? `${coreVersion}/${apiVersion}` : null
  const hasWarning = apiVersion?.endsWith('⚠️')
  const versionTooltip = hasWarning
    ? t('header.frontendNeedsRebuild')
    : versionDisplay
      ? `v${versionDisplay}`
      : ''

  const handleLogout = () => {
    navigationService.navigateToLogin()
  }

  return (
    <TooltipProvider>
      <header className="surface-glass sticky top-0 z-50 flex h-14 w-full items-center gap-3 border-b px-3 sm:px-4">
        {/* Brand */}
        <Link
          to={appRoutes.kbOverview(currentWorkspaceId, currentKnowledgeBaseId)}
          className="flex min-w-0 shrink-0 items-center gap-2.5 rounded-full px-1.5 py-1 transition-colors hover:bg-muted/60"
          aria-label={t('brand.name')}
        >
          <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-emerald-500/12 text-emerald-600 dark:bg-emerald-500/16 dark:text-emerald-300">
            <ZapIcon className="brand-glow size-4" aria-hidden="true" />
          </span>
          <span className="hidden min-w-0 flex-col leading-tight sm:flex">
            <span className="truncate text-sm font-semibold tracking-tight text-foreground">
              {t('brand.name')}
            </span>
            {(webuiTitle || webuiDescription) && (
              <span className="truncate text-[11px] text-muted-foreground">
                {webuiTitle || webuiDescription}
              </span>
            )}
          </span>
        </Link>

        {/* Center location indicator.
            Exactly one of the three children is visible at any viewport: */}
        <div className="flex min-w-0 flex-1 items-center">
          {/* xl and wider — switchers (richest, includes names + metadata) */}
          <div className="hidden min-w-0 items-center gap-2 xl:flex">
            <WorkspaceSwitcher className="min-w-0 flex-1 max-w-[14rem]" />
            <KnowledgeBaseSwitcher className="min-w-0 flex-1 max-w-[15rem]" />
          </div>

          {/* lg — breadcrumbs bridge the gap between tablet nav and switchers */}
          <div className="hidden min-w-0 flex-1 items-center justify-center lg:flex xl:hidden">
            <ContextBreadcrumbs />
          </div>

          {/* md — the AppPrimaryNav is mobile/tablet's only navigation channel
              while the left sidebar is collapsed. Kept intentionally. */}
          <div className="hidden min-w-0 flex-1 items-center justify-center md:flex lg:hidden">
            <AppPrimaryNav />
          </div>
        </div>

        {/* Right cluster: version • repo • settings • user */}
        <nav className="flex shrink-0 items-center gap-1.5">
          {coreVersion && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge
                  variant="outline"
                  className="hidden cursor-default rounded-full bg-muted/70 px-2 py-0.5 text-xs sm:inline-flex"
                >
                  v{coreVersion}
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="bottom">{versionTooltip || `v${coreVersion}`}</TooltipContent>
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
                <Button
                  variant="outline"
                  className="rounded-full border-border/70 bg-background/80 px-3"
                >
                  <UserCircle2Icon className="size-4" />
                  <span className="hidden max-w-[8rem] truncate text-sm sm:inline">{username}</span>
                  <ChevronDownIcon className="size-4 text-muted-foreground" />
                </Button>
              </PopoverTrigger>
              <PopoverContent
                align="end"
                className="w-[288px] rounded-2xl border-border/70 p-0 shadow-xl"
              >
                <div className="flex items-start gap-3 border-b border-border/60 px-4 py-4">
                  <div className="flex size-10 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                    <UserCircle2Icon className="size-5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-foreground">{username}</p>
                    {webuiTitle && (
                      <p className="truncate text-xs text-muted-foreground">{webuiTitle}</p>
                    )}
                    <div className="mt-2">
                      <AccessBadge role={effectiveRole} compact />
                    </div>
                    <p className="mt-2 truncate text-[11px] text-muted-foreground">
                      {currentWorkspaceId} / {currentKnowledgeBaseId}
                    </p>
                  </div>
                </div>
                <div className="p-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-8 w-full justify-start gap-2 rounded-md"
                    onClick={handleLogout}
                  >
                    <LogOutIcon className="size-4" />
                    {t('header.logout')}
                  </Button>
                </div>
              </PopoverContent>
            </Popover>
          )}
        </nav>
      </header>
    </TooltipProvider>
  )
}
