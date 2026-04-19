import Badge from '@/components/ui/Badge'
import { cn } from '@/lib/utils'
import { useTranslation } from 'react-i18next'
import { type AccessRole, roleLabelKeys } from '@/lib/permissions'

const toneByRole: Record<AccessRole, string> = {
  owner:
    'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/12 dark:text-emerald-300',
  admin:
    'border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-500/30 dark:bg-sky-500/12 dark:text-sky-300',
  editor:
    'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/12 dark:text-amber-300',
  viewer:
    'border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-500/30 dark:bg-slate-500/12 dark:text-slate-300',
  // no_access short-circuits in the component body, so this tone is
  // never rendered — declared only to satisfy the exhaustive Record.
  no_access: '',
}

interface AccessBadgeProps {
  role: AccessRole
  className?: string
  compact?: boolean
}

export default function AccessBadge({ role, className, compact = false }: AccessBadgeProps) {
  const { t } = useTranslation()
  // no_access means "the user isn't a member of this workspace". The
  // app should never strand a user on such a workspace in the first
  // place (the redirect in RequireAuth handles it). Don't render a
  // "无权访问" chip as a fallback — it implies we support a permission
  // tier we intentionally don't.
  if (role === 'no_access') {
    return null
  }
  const roleLabel = t(roleLabelKeys[role])

  return (
    <Badge
      variant="outline"
      className={cn(
        'rounded-full border px-3 py-1 font-medium',
        compact ? 'text-[11px]' : 'text-xs',
        toneByRole[role],
        className
      )}
    >
      {compact ? roleLabel : t('permissions.accessBadge', { role: roleLabel })}
    </Badge>
  )
}
