import Button from '@/components/ui/Button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import AppSettings from '@/components/AppSettings'
import { useAuthStore } from '@/stores/state'
import { useTranslation } from 'react-i18next'
import { navigationService } from '@/services/navigation'
import {
  ChevronDownIcon,
  LogOutIcon,
  UserCircle2Icon,
  ZapIcon,
} from 'lucide-react'
import { TooltipProvider } from '@/components/ui/Tooltip'
import { Link, useParams } from 'react-router-dom'
import AppPrimaryNav from '@/components/navigation/AppPrimaryNav'
import WorkspaceSwitcher from '@/components/navigation/WorkspaceSwitcher'
import ContextBreadcrumbs from '@/components/navigation/ContextBreadcrumbs'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { resolveEffectiveRole } from '@/lib/permissions'
import AccessBadge from '@/components/navigation/AccessBadge'

/**
 * Top-bar chrome.
 *
 * IA pivot (2026-04-17): KB is no longer a top-bar tier. The primary
 * switcher is the workspace; retrieval federates over every KB the
 * workspace owns. Version chip and GitHub link have been removed per
 * product direction — the menu that remains is identity + settings.
 */
export default function SiteHeader() {
  const { t } = useTranslation()
  const {
    isAuthenticated,
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

  const handleLogout = () => {
    navigationService.navigateToLogin()
  }

  return (
    <TooltipProvider>
      <header className="surface-glass sticky top-0 z-50 flex h-14 w-full items-center gap-3 border-b px-3 sm:px-4">
        {/* Brand */}
        <Link
          to={appRoutes.workspace(currentWorkspaceId)}
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
            Exactly one of the children is visible per viewport band: */}
        <div className="flex min-w-0 flex-1 items-center">
          {/* xl+ — workspace switcher */}
          <div className="hidden min-w-0 items-center xl:flex">
            <WorkspaceSwitcher className="min-w-0 flex-1 max-w-[16rem]" />
          </div>

          {/* lg — breadcrumbs */}
          <div className="hidden min-w-0 flex-1 items-center justify-center lg:flex xl:hidden">
            <ContextBreadcrumbs />
          </div>

          {/* md — tablet primary nav */}
          <div className="hidden min-w-0 flex-1 items-center justify-center md:flex lg:hidden">
            <AppPrimaryNav />
          </div>
        </div>

        {/* Right cluster: settings • user */}
        <nav className="flex shrink-0 items-center gap-1.5">
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
                      {currentWorkspaceId}
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
