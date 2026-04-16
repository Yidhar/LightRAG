import '@/lib/extensions'; // Import all global extensions
import { HashRouter as Router, Routes, Route, useNavigate, Navigate, useLocation } from 'react-router-dom'
import { Suspense, lazy, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useAuthStore } from '@/stores/state'
import { navigationService } from '@/services/navigation'
import { Toaster } from 'sonner'
import ThemeProvider from '@/components/ThemeProvider'
import { appRoutes, defaultKnowledgeBaseId, defaultWorkspaceId } from '@/app/routes'

const AppShell = lazy(() => import('./App'))
const LoginPage = lazy(() => import('@/features/LoginPage'))
const WorkspaceListPage = lazy(() => import('@/pages/workspaces/WorkspaceListPage'))
const MembersPage = lazy(() => import('@/pages/workspaces/MembersPage'))
const WorkspaceSettingsPage = lazy(() => import('@/pages/workspaces/WorkspaceSettingsPage'))
const KnowledgeBaseOverviewPage = lazy(() => import('@/pages/kb/KnowledgeBaseOverviewPage'))
const DocumentsPage = lazy(() => import('@/pages/kb/DocumentsPage'))
const RetrievalPage = lazy(() => import('@/pages/kb/RetrievalPage'))
const GraphPage = lazy(() => import('@/pages/kb/GraphPage'))
const ApiPage = lazy(() => import('@/pages/kb/ApiPage'))
const KnowledgeBaseSettingsPage = lazy(() => import('@/pages/kb/KnowledgeBaseSettingsPage'))

const FullScreenLoadingFallback = () => {
  const { t } = useTranslation()

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.08),_transparent_28%)] px-6">
      <div className="surface-panel flex min-h-[220px] w-full max-w-md flex-col items-center justify-center gap-4 rounded-[28px] border border-border/70 bg-card/95 p-8 text-center shadow-sm">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        <div className="space-y-1">
          <p className="font-medium text-foreground">
            {t('appRouter.fullScreenTitle', { name: t('brand.name') })}
          </p>
          <p className="text-sm text-muted-foreground">{t('appRouter.fullScreenDescription')}</p>
        </div>
      </div>
    </div>
  )
}

const RouteLoadingFallback = () => {
  const { t } = useTranslation()

  return (
    <div className="flex h-full min-h-[320px] items-center justify-center p-6">
      <div className="surface-panel flex min-h-[220px] w-full max-w-lg flex-col items-center justify-center gap-4 rounded-[28px] border border-border/70 bg-card/95 p-8 text-center shadow-sm">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        <div className="space-y-1">
          <p className="font-medium text-foreground">{t('appRouter.routeTitle')}</p>
          <p className="text-sm text-muted-foreground">{t('appRouter.routeDescription')}</p>
        </div>
      </div>
    </div>
  )
}

const withRouteSuspense = (element: React.ReactNode) => (
  <Suspense fallback={<RouteLoadingFallback />}>{element}</Suspense>
)

const RequireAuth = () => {
  const { isAuthenticated } = useAuthStore()
  const location = useLocation()

  if (!isAuthenticated) {
    const returnTo = `${location.pathname}${location.search}`
    return <Navigate to={`${appRoutes.login}?returnTo=${encodeURIComponent(returnTo)}`} replace />
  }

  return (
    <Suspense fallback={<FullScreenLoadingFallback />}>
      <AppShell />
    </Suspense>
  )
}

const AppContent = () => {
  const [initializing, setInitializing] = useState(true)
  const { isAuthenticated } = useAuthStore()
  const navigate = useNavigate()

  // Set navigate function for navigation service
  useEffect(() => {
    navigationService.setNavigate(navigate)
  }, [navigate])

  // Token validity check
  useEffect(() => {

    const checkAuth = async () => {
      try {
        const token = localStorage.getItem('LIGHTRAG-API-TOKEN')

        if (token && isAuthenticated) {
          setInitializing(false);
          return;
        }

        if (!token) {
          useAuthStore.getState().logout()
        }
      } catch (error) {
        console.error('Auth initialization error:', error)
        if (!isAuthenticated) {
          useAuthStore.getState().logout()
        }
      } finally {
        setInitializing(false)
      }
    }

    checkAuth()

    return () => {
    }
  }, [isAuthenticated])

  // Show nothing while initializing
  if (initializing) {
    return null
  }

  return (
    <Routes>
      <Route path="/login" element={withRouteSuspense(<LoginPage />)} />
      <Route path="/" element={<Navigate to={appRoutes.kbDocuments()} replace />} />
      <Route path="/app" element={<RequireAuth />}>
        <Route index element={<Navigate to={`workspaces/${defaultWorkspaceId}/kb/${defaultKnowledgeBaseId}/documents`} replace />} />
        <Route path="workspaces">
          <Route index element={withRouteSuspense(<WorkspaceListPage />)} />
          <Route path=":workspaceId">
            <Route index element={<Navigate to={`kb/${defaultKnowledgeBaseId}/documents`} replace />} />
            <Route path="members" element={withRouteSuspense(<MembersPage />)} />
            <Route path="settings" element={withRouteSuspense(<WorkspaceSettingsPage />)} />
            <Route path="kb/:kbId">
              <Route index element={<Navigate to="documents" replace />} />
              <Route path="overview" element={withRouteSuspense(<KnowledgeBaseOverviewPage />)} />
              <Route path="documents" element={withRouteSuspense(<DocumentsPage />)} />
              <Route path="retrieval" element={withRouteSuspense(<RetrievalPage />)} />
              <Route path="graph" element={withRouteSuspense(<GraphPage />)} />
              <Route path="api" element={withRouteSuspense(<ApiPage />)} />
              <Route path="settings" element={withRouteSuspense(<KnowledgeBaseSettingsPage />)} />
            </Route>
          </Route>
        </Route>
      </Route>
      <Route
        path="*"
        element={<Navigate to={isAuthenticated ? appRoutes.kbDocuments() : appRoutes.login} replace />}
      />
    </Routes>
  )
}

const AppRouter = () => {
  return (
    <ThemeProvider>
      <Router>
        <AppContent />
        <Toaster
          position="bottom-center"
          theme="system"
          closeButton
          richColors
        />
      </Router>
    </ThemeProvider>
  )
}

export default AppRouter
