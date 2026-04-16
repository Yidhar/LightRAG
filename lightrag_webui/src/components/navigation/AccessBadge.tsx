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
}

interface AccessBadgeProps {
  role: AccessRole
  className?: string
  compact?: boolean
}

export default function AccessBadge({ role, className, compact = false }: AccessBadgeProps) {
  const { t } = useTranslation()
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
