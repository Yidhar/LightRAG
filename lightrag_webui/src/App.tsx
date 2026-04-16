import { useState, useCallback, useEffect, useRef } from 'react'
import TabVisibilityProvider from '@/contexts/TabVisibilityProvider'
import ApiKeyAlert from '@/components/ApiKeyAlert'
import StatusIndicator from '@/components/status/StatusIndicator'
import { useBackendState, useAuthStore } from '@/stores/state'
import { useSettingsStore } from '@/stores/settings'
import { getAuthStatus } from '@/api/lightrag'
import { InvalidApiKeyError, RequireApiKeError } from '@/api/lightrag'
import { ZapIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import AppShellLayout from '@/layouts/AppShellLayout'
import { Link } from 'react-router-dom'
import { appRoutes } from '@/app/routes'

function App() {
  const { t } = useTranslation()
  const message = useBackendState.use.message()
  const enableHealthCheck = useSettingsStore.use.enableHealthCheck()
  const [apiKeyAlertOpen, setApiKeyAlertOpen] = useState(false)
  const [initializing, setInitializing] = useState(true) // Add initializing state
  const versionCheckRef = useRef(false); // Prevent duplicate calls in Vite dev mode
  const healthCheckInitializedRef = useRef(false); // Prevent duplicate health checks in Vite dev mode

  const handleApiKeyAlertOpenChange = useCallback((open: boolean) => {
    setApiKeyAlertOpen(open)
    if (!open) {
      useBackendState.getState().clear()
    }
  }, [])

  // Track component mount status with useRef
  const isMountedRef = useRef(true);

  // Set up mount/unmount status tracking
  useEffect(() => {
    isMountedRef.current = true;

    // Handle page reload/unload
    const handleBeforeUnload = () => {
      isMountedRef.current = false;
    };

    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      isMountedRef.current = false;
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, []);

  // Health check - can be disabled
  useEffect(() => {
    // Health check function
    const performHealthCheck = async () => {
      try {
        // Only perform health check if component is still mounted
        if (isMountedRef.current) {
          await useBackendState.getState().check();
        }
      } catch (error) {
        console.error('Health check error:', error);
      }
    };

    // Set health check function in the store
    useBackendState.getState().setHealthCheckFunction(performHealthCheck);

    if (!enableHealthCheck || apiKeyAlertOpen) {
      useBackendState.getState().clearHealthCheckTimer();
      return;
    }

    // On first mount or when enableHealthCheck becomes true and apiKeyAlertOpen is false,
    // perform an immediate health check and start the timer
    if (!healthCheckInitializedRef.current) {
      healthCheckInitializedRef.current = true;
    }

    // Start/reset the health check timer using the store
    useBackendState.getState().resetHealthCheckTimer();

    // Component unmount cleanup
    return () => {
      useBackendState.getState().clearHealthCheckTimer();
    };
  }, [enableHealthCheck, apiKeyAlertOpen]);

  // Version check - independent and executed only once
  useEffect(() => {
    const checkVersion = async () => {
      // Prevent duplicate calls in Vite dev mode
      if (versionCheckRef.current) return;
      versionCheckRef.current = true;

      // Check if version info was already obtained in login page
      const versionCheckedFromLogin = sessionStorage.getItem('VERSION_CHECKED_FROM_LOGIN') === 'true';
      if (versionCheckedFromLogin) {
        setInitializing(false); // Skip initialization if already checked
        return;
      }

      try {
        setInitializing(true); // Start initialization

        // Get version info
        const token = localStorage.getItem('LIGHTRAG-API-TOKEN');
        const status = await getAuthStatus();

        if (token && (status.core_version || status.api_version || status.webui_title || status.webui_description)) {
          useAuthStore.getState().login(
            token,
            status.core_version,
            status.api_version,
            status.webui_title || null,
            status.webui_description || null
          );
        } else if (status.core_version || status.api_version || status.webui_title || status.webui_description) {
          useAuthStore.getState().setVersion(status.core_version || null, status.api_version || null)
          useAuthStore.getState().setCustomTitle(
            status.webui_title || null,
            status.webui_description || null
          )
        }

        // Set flag to indicate version info has been checked
        sessionStorage.setItem('VERSION_CHECKED_FROM_LOGIN', 'true');
      } catch (error) {
        console.error('Failed to get version info:', error);
      } finally {
        // Ensure initializing is set to false even if there's an error
        setInitializing(false);
      }
    };

    // Execute version check
    checkVersion();
  }, []); // Empty dependency array ensures it only runs once on mount

  useEffect(() => {
    if (message) {
      if (message.includes(InvalidApiKeyError) || message.includes(RequireApiKeError)) {
        setApiKeyAlertOpen(true)
      }
    }
  }, [message])

  return (
    <TabVisibilityProvider>
      {initializing ? (
        <div className="flex h-screen w-screen flex-col bg-background">
          <header className="border-border/60 bg-background/95 supports-[backdrop-filter]:bg-background/70 sticky top-0 z-50 flex min-h-14 w-full items-center border-b px-4 backdrop-blur">
            <Link to={appRoutes.kbDocuments()} className="flex items-center gap-2">
              <ZapIcon className="size-5 text-emerald-400" aria-hidden="true" />
              <span className="font-semibold tracking-tight md:inline-block">{t('brand.name')}</span>
            </Link>
          </header>

          <div className="flex flex-1 items-center justify-center">
            <div className="text-center">
              <div className="mx-auto mb-2 h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent"></div>
              <p>{t('app.initializing')}</p>
            </div>
          </div>
        </div>
      ) : (
        <>
          <AppShellLayout />
          {enableHealthCheck && <StatusIndicator />}
          <ApiKeyAlert open={apiKeyAlertOpen} onOpenChange={handleApiKeyAlertOpenChange} />
        </>
      )}
    </TabVisibilityProvider>
  )
}

export default App
