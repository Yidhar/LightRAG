import { useEffect, useMemo } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import SiteHeader from '@/features/SiteHeader'
import { appRoutes } from '@/app/routes'
import { resolveCurrentTabFromPath } from '@/app/routeHelpers'
import { useSettingsStore } from '@/stores/settings'
import { useAuthStore } from '@/stores/state'
import AppSidebarNav from '@/components/navigation/AppSidebarNav'
import { useSyncKBScope } from '@/hooks/useSyncKBScope'

/** Extract ``<workspaceId>`` from a URL like
 *  ``/app/workspaces/<id>[/...]``. Returns null for non-workspace URLs
 *  (including ``/app/workspaces`` and ``/app/workspaces/``). */
function matchUrlWorkspaceId(pathname: string): string | null {
  const m = pathname.match(/^\/app\/workspaces\/([^/]+)(?:\/.*)?$/)
  return m ? m[1] : null
}

export default function AppShellLayout() {
  const location = useLocation()
  const memberships = useAuthStore((s) => s.memberships)

  // Mirror the URL workspace into the KB scope store so the axios
  // interceptor can inject X-Workspace-Id on root-level API calls.
  useSyncKBScope()

  useEffect(() => {
    const nextTab = resolveCurrentTabFromPath(location.pathname)
    const state = useSettingsStore.getState()

    if (state.currentTab !== nextTab) {
      state.setCurrentTab(nextTab)
    }
  }, [location.pathname])

  // Membership gate: if the URL's workspace isn't one the user belongs
  // to, redirect them to their primary workspace (or the landing page
  // if they have no memberships at all). This is what keeps a self-
  // registered user from getting stranded on /workspaces/default/*
  // with a "无权访问" sidebar — we never let them land there in the
  // first place. useParams at this level doesn't see ``:workspaceId``
  // because AppShellLayout is rendered at ``/app``, not at the child
  // route, so we parse the pathname directly.
  const primaryWorkspaceId = useMemo(() => {
    for (const claim of memberships || []) {
      if (claim.workspace_id) return claim.workspace_id
    }
    return null
  }, [memberships])

  const urlWorkspaceId = matchUrlWorkspaceId(location.pathname)
  const isMember = urlWorkspaceId
    ? memberships?.some((c) => c.workspace_id === urlWorkspaceId)
    : true

  if (urlWorkspaceId && !isMember) {
    if (primaryWorkspaceId) {
      return <Navigate to={appRoutes.kbDocuments(primaryWorkspaceId)} replace />
    }
    return <Navigate to={appRoutes.workspaces} replace />
  }

  return (
    <main className="flex h-screen w-screen overflow-hidden bg-background">
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <SiteHeader />
        <div className="flex min-h-0 flex-1 overflow-hidden">
          <AppSidebarNav />
          <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
            <Outlet />
          </div>
        </div>
      </div>
    </main>
  )
}
