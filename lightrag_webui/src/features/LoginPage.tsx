import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuthStore } from '@/stores/state'
import { useSettingsStore } from '@/stores/settings'
import { bootstrapAdmin, getAuthStatus, loginToServer } from '@/api/lightrag'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import Input from '@/components/ui/Input'
import Button from '@/components/ui/Button'
import Checkbox from '@/components/ui/Checkbox'
import AppSettings from '@/components/AppSettings'
import { EyeIcon, EyeOffIcon, ShieldCheckIcon, ZapIcon } from 'lucide-react'

const REMEMBERED_USERNAME_STORAGE_KEY = 'LIGHTRAG-REMEMBERED-USERNAME'

const LoginPage = () => {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { login, isAuthenticated, setVersion, setCustomTitle } = useAuthStore()
  const { t } = useTranslation()

  const [loading, setLoading] = useState(false)
  const [checkingAuth, setCheckingAuth] = useState(true)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [rememberUsername, setRememberUsername] = useState(true)
  const [usernameError, setUsernameError] = useState('')
  const [passwordError, setPasswordError] = useState('')
  // Bootstrap mode: shown when the backend reports auth_configured=false
  // but the DB-backed user directory is ready. Guest fallbacks have been
  // removed, so the UI must offer a path to create the first admin.
  const [needsBootstrap, setNeedsBootstrap] = useState(false)

  const authCheckRef = useRef(false)
  const requestedReturnTo = searchParams.get('returnTo') || '/'
  const returnTo = requestedReturnTo.startsWith('/login') ? '/' : requestedReturnTo

  useEffect(() => {
    const rememberedUsername = localStorage.getItem(REMEMBERED_USERNAME_STORAGE_KEY)
    if (rememberedUsername) {
      setUsername(rememberedUsername)
      setRememberUsername(true)
    }
  }, [])

  useEffect(() => {
    const checkAuthConfig = async () => {
      if (authCheckRef.current) return
      authCheckRef.current = true

      try {
        if (isAuthenticated) {
          navigate(returnTo)
          return
        }

        const status = await getAuthStatus()

        if (status.core_version || status.api_version) {
          setVersion(status.core_version || null, status.api_version || null)
          sessionStorage.setItem('VERSION_CHECKED_FROM_LOGIN', 'true')
        }

        if (status.webui_title || status.webui_description) {
          setCustomTitle(status.webui_title || null, status.webui_description || null)
        }

        const bootstrapReady =
          !status.auth_configured && Boolean(status.supports_user_management)
        setNeedsBootstrap(bootstrapReady)

        if (!status.auth_configured && !bootstrapReady && status.message) {
          toast.error(status.message)
        }
      } catch (error) {
        console.error('Failed to check auth configuration:', error)
      } finally {
        setCheckingAuth(false)
      }
    }

    checkAuthConfig()
  }, [isAuthenticated, navigate, returnTo, setCustomTitle, setVersion])

  const validateForm = () => {
    let valid = true

    if (!username.trim()) {
      setUsernameError(t('login.usernameRequired'))
      valid = false
    } else {
      setUsernameError('')
    }

    if (!password) {
      setPasswordError(t('login.passwordRequired'))
      valid = false
    } else {
      setPasswordError('')
    }

    if (!valid) {
      toast.error(t('login.errorEmptyFields'))
    }

    return valid
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!validateForm()) return

    if (needsBootstrap) {
      if (password.length < 8) {
        setPasswordError(
          t('login.passwordMinLength', { defaultValue: '密码至少 8 位' })
        )
        toast.error(t('login.errorEmptyFields'))
        return
      }
      try {
        setLoading(true)
        const bootstrapResponse = await bootstrapAdmin(username.trim(), password)

        if (rememberUsername) {
          localStorage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, username.trim())
        }
        localStorage.setItem('LIGHTRAG-PREVIOUS-USER', username.trim())

        // Re-read status to refresh versions / custom title after bootstrap.
        const fresh = await getAuthStatus().catch(() => null)
        login(
          bootstrapResponse.access_token,
          fresh?.core_version ?? null,
          fresh?.api_version ?? null,
          fresh?.webui_title ?? null,
          fresh?.webui_description ?? null
        )

        toast.success(
          t('login.bootstrapSuccess', {
            defaultValue: '管理员账号已创建，欢迎进入工作区。',
          })
        )
        navigate(returnTo)
      } catch (err: any) {
        const detail =
          err?.response?.data?.detail ||
          err?.message ||
          t('login.bootstrapFailed', { defaultValue: '创建管理员失败' })
        toast.error(detail)
      } finally {
        setLoading(false)
      }
      return
    }

    try {
      setLoading(true)
      const response = await loginToServer(username.trim(), password)

      const previousUsername = localStorage.getItem('LIGHTRAG-PREVIOUS-USER')
      const isSameUser = previousUsername === username.trim()
      if (!isSameUser) {
        useSettingsStore.getState().setRetrievalHistory([])
      }
      localStorage.setItem('LIGHTRAG-PREVIOUS-USER', username.trim())

      if (rememberUsername) {
        localStorage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, username.trim())
      } else {
        localStorage.removeItem(REMEMBERED_USERNAME_STORAGE_KEY)
      }

      login(
        response.access_token,
        response.core_version,
        response.api_version,
        response.webui_title || null,
        response.webui_description || null
      )

      if (response.core_version || response.api_version) {
        sessionStorage.setItem('VERSION_CHECKED_FROM_LOGIN', 'true')
      }

      toast.success(t('login.successMessage'))
      navigate(returnTo)
    } catch (error) {
      console.error('Login failed...', error)
      toast.error(t('login.errorInvalidCredentials'))
      useAuthStore.getState().logout()
      localStorage.removeItem('LIGHTRAG-API-TOKEN')
      setPassword('')
      setPasswordError(t('login.errorInvalidCredentials'))
    } finally {
      setLoading(false)
    }
  }

  if (checkingAuth) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-slate-100 dark:bg-slate-950">
        <div className="surface-panel flex w-full max-w-md flex-col items-center gap-4 rounded-[28px] border border-slate-200/90 px-8 py-10 text-center dark:border-slate-800">
          <div className="size-10 animate-spin rounded-full border-4 border-emerald-500/30 border-t-emerald-500" />
          <div className="space-y-1">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-600/80 dark:text-emerald-400/80">
              {t('login.materialConsoleLabel')}
            </p>
            <p className="text-sm text-slate-500 dark:text-slate-400">{t('app.initializing')}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="relative flex h-screen w-screen items-center justify-center overflow-hidden bg-slate-100 selection:bg-emerald-100 selection:text-emerald-900 dark:bg-slate-950">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.14),_transparent_34%),linear-gradient(180deg,_rgba(255,255,255,0.72),_rgba(248,250,252,0.96))] dark:bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.14),_transparent_28%),linear-gradient(180deg,_rgba(15,23,42,0.94),_rgba(2,6,23,0.98))]" />

      <div className="absolute inset-x-0 top-6 z-20 mx-auto flex w-full max-w-6xl items-center justify-between px-6">
        <div className="hidden items-center gap-3 md:flex">
          <div className="flex size-11 items-center justify-center rounded-2xl border border-emerald-200/70 bg-white/80 shadow-sm dark:border-emerald-500/20 dark:bg-slate-900/80">
            <ShieldCheckIcon className="size-5 text-emerald-600 dark:text-emerald-300" />
          </div>
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-emerald-700/70 dark:text-emerald-300/70">
              {t('login.secureWorkspaceLabel')}
            </p>
            <p className="text-sm text-slate-600 dark:text-slate-300">{t('brand.name')}</p>
          </div>
        </div>

        <AppSettings className="rounded-2xl border border-slate-200 bg-white/95 shadow-sm dark:border-slate-800 dark:bg-slate-900/95 hover:bg-white dark:hover:bg-slate-900" />
      </div>

      <div className="relative z-10 grid w-full max-w-6xl gap-8 px-6 lg:grid-cols-[1.15fr_minmax(0,480px)]">
        <section className="hidden flex-col justify-center lg:flex">
          <div className="max-w-2xl space-y-6">
            <div className="inline-flex items-center gap-2 rounded-full border border-emerald-200/80 bg-white/80 px-4 py-2 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700/80 shadow-sm dark:border-emerald-500/20 dark:bg-slate-900/70 dark:text-emerald-300/80">
              <ShieldCheckIcon className="size-4" />
              {t('login.materialConsoleLabel')}
            </div>

            <div className="space-y-4">
              <h1 className="text-5xl font-black tracking-tight text-slate-900 dark:text-slate-50">
                {t('brand.name')}
              </h1>
              <p className="max-w-xl text-base leading-8 text-slate-600 dark:text-slate-300">
                {t('login.description')}
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-[28px] border border-slate-200/80 bg-white/85 p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900/75">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
                  {t('login.brandCardTitle')}
                </p>
                <p className="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300">
                  {t('login.brandCardDescription')}
                </p>
              </div>
              <div className="rounded-[28px] border border-slate-200/80 bg-white/85 p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900/75">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
                  {t('login.workflowCardTitle')}
                </p>
                <p className="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300">
                  {t('login.workflowCardDescription')}
                </p>
              </div>
            </div>
          </div>
        </section>

        <section className="relative z-10 w-full max-w-[520px] justify-self-center">
          <div className="surface-panel animate-in fade-in slide-in-from-bottom-4 rounded-[32px] border border-slate-200/90 px-8 py-10 shadow-[0_24px_80px_rgba(15,23,42,0.12)] duration-700 dark:border-slate-800 sm:px-10">
            <div className="mb-10 flex flex-col items-center space-y-5">
              <div className="relative flex items-center gap-3 rounded-[28px] border border-emerald-100 bg-emerald-500 px-5 py-4 shadow-[0_14px_36px_rgba(16,185,129,0.24)] dark:border-emerald-500/30 dark:bg-emerald-500/90">
                <img src="logo.svg" alt={t('brand.logoAlt', { name: t('brand.name') })} className="h-10 w-10" />
                <ZapIcon className="size-7 text-white" aria-hidden="true" />
              </div>

              <div className="space-y-2 text-center">
                <p className="text-xs font-semibold uppercase tracking-[0.24em] text-emerald-600/80 dark:text-emerald-400/80">
                  {t('login.secureWorkspaceLabel')}
                </p>
                <h2 className="text-[2rem] font-extrabold tracking-tight text-slate-900 dark:text-slate-50">
                  {t('login.loginButton')}
                </h2>
                <p className="mx-auto max-w-sm text-sm leading-relaxed text-slate-500 dark:text-slate-400">
                  {t('login.continueDescription')}
                </p>
              </div>
            </div>

            <div className="mb-8 h-px bg-slate-200 dark:bg-slate-800" />

            {needsBootstrap && (
              <div
                className="mb-6 rounded-2xl border border-emerald-400/40 bg-emerald-500/[0.08] px-4 py-3 text-sm leading-6 text-emerald-800 dark:border-emerald-400/30 dark:text-emerald-200"
                role="status"
              >
                <p className="font-semibold">
                  {t('login.bootstrapTitle', { defaultValue: '首次使用：创建管理员账号' })}
                </p>
                <p className="mt-1 text-[13px] text-emerald-700 dark:text-emerald-300/90">
                  {t('login.bootstrapDescription', {
                    defaultValue:
                      '本部署还没有任何账号。输入想用的用户名和密码（至少 8 位），提交后将作为 Owner 创建并自动登录。',
                  })}
                </p>
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-5">
              <div className="flex flex-col gap-2">
                <label htmlFor="username-input" className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                  {t('login.username')}
                </label>
                <Input
                  id="username-input"
                  placeholder={t('login.usernamePlaceholder')}
                  value={username}
                  onChange={(e) => {
                    setUsername(e.target.value)
                    if (usernameError) setUsernameError('')
                  }}
                  required
                  className={`h-12 w-full rounded-2xl bg-slate-50 shadow-none focus-visible:ring-2 focus-visible:ring-emerald-500/30 dark:bg-slate-950 ${
                    usernameError
                      ? 'border-red-400 focus-visible:ring-red-500/20 dark:border-red-500/70'
                      : 'border-slate-300 dark:border-slate-800'
                  }`}
                />
                {usernameError && (
                  <p className="text-xs text-red-500 dark:text-red-400">{usernameError}</p>
                )}
              </div>

              <div className="flex flex-col gap-2">
                <label htmlFor="password-input" className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                  {t('login.password')}
                </label>
                <div className="relative">
                  <Input
                    id="password-input"
                    type={showPassword ? 'text' : 'password'}
                    placeholder={t('login.passwordPlaceholder')}
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value)
                      if (passwordError) setPasswordError('')
                    }}
                    required
                    className={`h-12 w-full rounded-2xl bg-slate-50 pr-12 shadow-none focus-visible:ring-2 focus-visible:ring-emerald-500/30 dark:bg-slate-950 ${
                      passwordError
                        ? 'border-red-400 focus-visible:ring-red-500/20 dark:border-red-500/70'
                        : 'border-slate-300 dark:border-slate-800'
                    }`}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((prev) => !prev)}
                    className="absolute inset-y-0 right-3 inline-flex items-center text-slate-400 transition-colors hover:text-slate-600 dark:text-slate-500 dark:hover:text-slate-300"
                    aria-label={showPassword ? t('login.hidePassword') : t('login.showPassword')}
                  >
                    {showPassword ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
                  </button>
                </div>
                {passwordError && (
                  <p className="text-xs text-red-500 dark:text-red-400">{passwordError}</p>
                )}
              </div>

              <div className="flex items-center justify-between gap-4 rounded-2xl border border-slate-200/80 bg-slate-50/80 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/60">
                <label htmlFor="remember-username" className="flex cursor-pointer items-center gap-3 text-sm text-slate-600 dark:text-slate-300">
                  <Checkbox
                    id="remember-username"
                    checked={rememberUsername}
                    onCheckedChange={(checked) => setRememberUsername(checked === true)}
                  />
                  <span>{t('login.rememberUsername')}</span>
                </label>
                <span className="text-xs uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                  {t('login.localAuthOnly')}
                </span>
              </div>

              <Button
                type="submit"
                className="mt-2 h-12 w-full rounded-2xl border-0 bg-emerald-600 text-base font-semibold text-white shadow-[0_10px_20px_rgba(5,150,105,0.22)] transition-all duration-200 hover:bg-emerald-500 hover:shadow-[0_14px_24px_rgba(5,150,105,0.28)] active:scale-[0.98]"
                disabled={loading}
              >
                {loading
                  ? t('login.loggingIn')
                  : needsBootstrap
                    ? t('login.bootstrapSubmit', { defaultValue: '创建管理员并进入' })
                    : t('login.continueToWorkspace')}
              </Button>
            </form>
          </div>
        </section>
      </div>
    </div>
  )
}

export default LoginPage
