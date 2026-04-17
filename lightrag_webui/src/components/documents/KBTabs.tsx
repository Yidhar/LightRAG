import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { BookOpenTextIcon, PlusIcon } from 'lucide-react'

import { listKnowledgeBases, type KnowledgeBaseRecord } from '@/api/lightrag'
import { useKBStore } from '@/stores/kb'
import { defaultKnowledgeBaseId } from '@/app/routes'
import { cn } from '@/lib/utils'
import Button from '@/components/ui/Button'

// Query-string key used to persist the active KB selection across reloads.
// Keeping the key short so shared URLs read cleanly.
const KB_QUERY_KEY = 'kb'

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
  const [searchParams, setSearchParams] = useSearchParams()
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const activeKbId = useKBStore((s) => s.activeKbId) ?? defaultKnowledgeBaseId
  const setActiveKb = useKBStore((s) => s.setActiveKb)

  // Writes the active KB into the URL with replaceState so we don't
  // pollute history with a back-button entry per tab click.
  const writeKbQuery = useCallback(
    (kbId: string | null) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (kbId) {
            next.set(KB_QUERY_KEY, kbId)
          } else {
            next.delete(KB_QUERY_KEY)
          }
          return next
        },
        { replace: true }
      )
    },
    [setSearchParams]
  )

  // Seed activeKbId from ``?kb=`` on the first mount of this component.
  // Runs before the list loads so the correct tab is highlighted as
  // soon as the fetch returns. Intentionally [] — we do not want URL
  // edits made *after* mount to fight the tab picker.
  useEffect(() => {
    const fromUrl = searchParams.get(KB_QUERY_KEY)
    if (fromUrl && fromUrl !== activeKbId) {
      setActiveKb(fromUrl)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setLoadError(false)
      const response = await listKnowledgeBases(workspaceId)
      setKnowledgeBases(response.items)
      // If the currently-active KB no longer exists (deleted, workspace
      // switched, or stale URL ``?kb=``), fall back to the default and
      // clean up the URL so share-links stay valid.
      if (
        response.items.length > 0 &&
        activeKbId &&
        !response.items.some((kb) => kb.id === activeKbId)
      ) {
        setActiveKb(defaultKnowledgeBaseId)
        writeKbQuery(null)
      }
    } catch {
      setKnowledgeBases([])
      setLoadError(true)
    } finally {
      setLoading(false)
    }
  }, [workspaceId, activeKbId, setActiveKb, writeKbQuery])

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
    writeKbQuery(kbId)
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
