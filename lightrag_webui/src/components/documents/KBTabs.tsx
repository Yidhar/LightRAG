import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { BookOpenTextIcon, PlusIcon } from 'lucide-react'

import { listKnowledgeBases, type KnowledgeBaseRecord } from '@/api/lightrag'
import { useKBStore } from '@/stores/kb'
import { defaultKnowledgeBaseId } from '@/app/routes'
import { cn } from '@/lib/utils'
import Button from '@/components/ui/Button'

interface KBTabsProps {
  workspaceId: string
  onChange?: (kbId: string) => void
  /** Parent can pass a handler for opening a KB-create dialog. */
  onCreate?: () => void
}

/**
 * Horizontal tab picker for KBs inside a workspace (Phase C).
 *
 * Writes the selected KB id to ``useKBStore.activeKbId`` so the axios
 * request interceptor injects ``X-KB-Id`` on every following API call.
 * Parents that render DocumentManager (or other KB-scoped features)
 * should key the child on the returned id so a switch triggers a
 * refetch.
 */
export default function KBTabs({ workspaceId, onChange, onCreate }: KBTabsProps) {
  const { t } = useTranslation()
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const activeKbId = useKBStore((s) => s.activeKbId) ?? defaultKnowledgeBaseId
  const setActiveKb = useKBStore((s) => s.setActiveKb)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setLoadError(false)
      const response = await listKnowledgeBases(workspaceId)
      setKnowledgeBases(response.items)
      // If the currently-active KB no longer exists (deleted, workspace
      // switched), fall back to the default.
      if (
        response.items.length > 0 &&
        activeKbId &&
        !response.items.some((kb) => kb.id === activeKbId)
      ) {
        setActiveKb(defaultKnowledgeBaseId)
      }
    } catch {
      setKnowledgeBases([])
      setLoadError(true)
    } finally {
      setLoading(false)
    }
  }, [workspaceId, activeKbId, setActiveKb])

  useEffect(() => {
    void load()
    // Refresh on cross-component KB mutations (create / delete).
    const handler = () => {
      void load()
    }
    window.addEventListener('lightrag:kb-updated', handler)
    return () => {
      window.removeEventListener('lightrag:kb-updated', handler)
    }
  }, [load])

  // Initialise activeKbId to default on first render if not already set.
  useEffect(() => {
    if (activeKbId == null) {
      setActiveKb(defaultKnowledgeBaseId)
    }
  }, [activeKbId, setActiveKb])

  const handlePick = (kbId: string) => {
    setActiveKb(kbId)
    if (onChange) onChange(kbId)
  }

  if (loadError) {
    return (
      <div className="flex items-center gap-2 border-b border-border/60 px-6 py-2 text-xs text-muted-foreground">
        <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
        {t('platformShell.documents.kbTabs.loadError', {
          defaultValue: '无法读取知识库列表，使用默认知识库。',
        })}
      </div>
    )
  }

  return (
    <div
      className="flex items-center gap-2 overflow-x-auto border-b border-border/60 bg-muted/10 px-4 py-2 text-sm"
      role="tablist"
      aria-label={t('platformShell.documents.kbTabs.label', { defaultValue: '知识库' })}
    >
      <span className="shrink-0 text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
        {t('platformShell.documents.kbTabs.label', { defaultValue: '知识库' })}
      </span>

      {loading && knowledgeBases.length === 0 ? (
        <span className="text-xs text-muted-foreground">
          {t('platformShell.common.loading', { defaultValue: '加载中…' })}
        </span>
      ) : knowledgeBases.length === 0 ? (
        <span className="text-xs text-muted-foreground">
          {t('platformShell.documents.kbTabs.empty', {
            defaultValue: '暂无知识库 — 在"知识库管理"中新建。',
          })}
        </span>
      ) : (
        <div className="flex min-w-0 flex-wrap items-center gap-1">
          {knowledgeBases.map((kb) => {
            const isActive = kb.id === activeKbId
            return (
              <button
                key={kb.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => handlePick(kb.id)}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                  isActive
                    ? 'border-emerald-500/40 bg-emerald-500/[0.10] text-emerald-700 dark:text-emerald-300'
                    : 'border-border/60 bg-background/70 text-muted-foreground hover:border-emerald-500/30 hover:text-foreground'
                )}
                title={kb.description || kb.name || kb.id}
              >
                <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
                <span className="truncate">{kb.name || kb.id}</span>
              </button>
            )
          })}
        </div>
      )}

      {onCreate && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={onCreate}
          className="ml-auto shrink-0 rounded-full text-xs"
        >
          <PlusIcon className="size-3.5" />
          {t('platformShell.documents.kbTabs.create', { defaultValue: '新建知识库' })}
        </Button>
      )}
    </div>
  )
}
