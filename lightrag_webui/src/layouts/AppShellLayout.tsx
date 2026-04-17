import { useEffect } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import SiteHeader from '@/features/SiteHeader'
import { resolveCurrentTabFromPath } from '@/app/routeHelpers'
import { useSettingsStore } from '@/stores/settings'
import AppSidebarNav from '@/components/navigation/AppSidebarNav'
import { useSyncKBScope } from '@/hooks/useSyncKBScope'

export default function AppShellLayout() {
  const location = useLocation()

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
