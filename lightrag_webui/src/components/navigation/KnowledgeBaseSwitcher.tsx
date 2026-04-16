import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { getKnowledgeBase, type KnowledgeBaseRecord } from '@/api/lightrag'
import { cn } from '@/lib/utils'
import { BookOpenTextIcon, ChevronRightIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

interface KnowledgeBaseSwitcherProps {
  className?: string
}

export default function KnowledgeBaseSwitcher({ className }: KnowledgeBaseSwitcherProps) {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBaseRecord | null>(null)
  const [refreshVersion, setRefreshVersion] = useState(0)

  useEffect(() => {
    let cancelled = false

    const loadKnowledgeBase = async () => {
      try {
        const response = await getKnowledgeBase(currentWorkspaceId, currentKnowledgeBaseId)
        if (!cancelled) {
          setKnowledgeBase(response)
        }
      } catch {
        if (!cancelled) {
          setKnowledgeBase(null)
        }
      }
    }

    void loadKnowledgeBase()

    return () => {
      cancelled = true
    }
  }, [currentKnowledgeBaseId, currentWorkspaceId, refreshVersion])

  useEffect(() => {
    const handleKnowledgeBaseUpdated = (
      event: Event
    ) => {
      const detail = (event as CustomEvent<{ workspaceId?: string; kbId?: string }>).detail
      if (
        detail?.workspaceId === currentWorkspaceId &&
        detail?.kbId === currentKnowledgeBaseId
      ) {
        setRefreshVersion((value) => value + 1)
      }
    }

    window.addEventListener('lightrag:kb-updated', handleKnowledgeBaseUpdated as EventListener)
    return () => {
      window.removeEventListener('lightrag:kb-updated', handleKnowledgeBaseUpdated as EventListener)
    }
  }, [currentKnowledgeBaseId, currentWorkspaceId])

  const knowledgeBaseLabel = knowledgeBase?.name || currentKnowledgeBaseId
  const details: string[] = []

  if (knowledgeBase?.name && knowledgeBase.name !== currentKnowledgeBaseId) {
    details.push(currentKnowledgeBaseId)
  } else {
    details.push(t('navigation.knowledgeBaseSwitcher.workspaceDetail', { workspaceId: currentWorkspaceId }))
  }

  if (knowledgeBase?.status) {
    details.push(knowledgeBase.status)
  }

  const detailLine = details.join(' · ')

  return (
    <Link
      to={appRoutes.kbOverview(currentWorkspaceId, currentKnowledgeBaseId)}
      className={cn(
        'motion-standard inline-flex min-w-[13rem] items-center gap-3 rounded-2xl border border-slate-200 bg-white px-3.5 py-2 text-sm shadow-[0_6px_20px_rgba(15,23,42,0.06)] active:scale-95 hover:border-emerald-500/30 hover:bg-emerald-50/70 dark:border-slate-800 dark:bg-slate-900 dark:hover:bg-slate-800',
        className
      )}
    >
      <div className="flex size-9 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-300">
        <BookOpenTextIcon className="size-4" aria-hidden="true" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
          {t('header.knowledgeBase')}
        </div>
        <div className="truncate font-medium text-foreground">{knowledgeBaseLabel}</div>
        <div className="truncate text-[11px] text-muted-foreground">{detailLine}</div>
      </div>
      <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
    </Link>
  )
}
