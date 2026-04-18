import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { BookOpenTextIcon, PlusIcon } from 'lucide-react'

import { listKnowledgeBases, type KnowledgeBaseRecord } from '@/api/lightrag'
import { useKBStore } from '@/stores/kb'
import { defaultKnowledgeBaseId } from '@/app/routes'
import Button from '@/components/ui/Button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'

// Query-string key used to persist the active KB selection across reloads.
// Keeping the key short so shared URLs read cleanly.
const KB_QUERY_KEY = 'kb'

interface KBTabsProps {
  workspaceId: string
  onChange?: (kbId: string) => void
  /** Parent can pass a handler for opening a KB-create dialog. */
  onCreate?: () => void
  /** Show a synthetic "All KBs in workspace" option at the top. */
  allowAllOption?: boolean
  /** Parent-driven flag indicating the all-KBs sentinel is active. */
  allKbsSelected?: boolean
  /** Fired when the sentinel option is picked — parent owns the flag. */
  onPickAll?: () => void
}

// Sentinel value used in the Select for the aggregate option. Kept out of
// the KB store so a real KB id never collides with it.
const ALL_KBS_SENTINEL = '__all_kbs__'

/**
 * KB picker: dropdown Select (previously a horizontal pills row).
 *
 * The component is still named ``KBTabs`` because every page imports
 * it under that name and the behavior contract — writing the picked
 * id to ``useKBStore.activeKbId`` so the axios interceptor injects
 * ``X-KB-Id`` — is unchanged. Switched from pills to a Select so
 * workspaces with many KBs stay scannable, and to match the user's
 * request: "直接提供下拉表单来选取配置的知识库".
 */
export default function KBTabs({
  workspaceId,
  onChange,
  onCreate,
  allowAllOption = false,
  allKbsSelected = false,
  onPickAll,
}: KBTabsProps) {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const activeKbId = useKBStore((s) => s.activeKbId) ?? defaultKnowledgeBaseId
  const setActiveKb = useKBStore((s) => s.setActiveKb)

  // Writes the active KB into the URL with replaceState so we don't
  // pollute history with a back-button entry per pick.
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
  // Runs before the list loads so the correct option is selected as
  // soon as the fetch returns. Intentionally [] — we do not want URL
  // edits made *after* mount to fight the picker.
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
      const firstKbId = response.items[0]?.id ?? null
      if (response.items.length > 0) {
        // If nothing is selected yet, or the selection doesn't exist in
        // this workspace (stale URL ``?kb=`` / workspace switch / deleted
        // KB), snap to the first actually-linked KB. Falling back to the
        // string "default" is wrong — a personal workspace might not have
        // any KB named "default" linked, which 404s /documents/paginated.
        if (
          !activeKbId ||
          !response.items.some((kb) => kb.id === activeKbId)
        ) {
          if (firstKbId) {
            setActiveKb(firstKbId)
            writeKbQuery(firstKbId)
          }
        }
      } else {
        // No KBs linked to this workspace at all — clear the selection so
        // downstream callers (DocumentManager interceptor header injector)
        // don't keep sending a stale id.
        if (activeKbId) {
          setActiveKb(null)
          writeKbQuery(null)
        }
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
    // Refresh on cross-component KB mutations (create / rename / delete).
    const handler = () => {
      void load()
    }
    window.addEventListener('lightrag:kb-updated', handler)
    return () => {
      window.removeEventListener('lightrag:kb-updated', handler)
    }
  }, [load])

  // Intentionally no "init to default" effect here — `load()` above
  // picks the first *actually-linked* KB once the list returns. Seeding
  // a hardcoded "default" triggered spurious 404s on workspaces where
  // no KB with that id is linked.

  const handlePick = (kbId: string) => {
    if (kbId === ALL_KBS_SENTINEL) {
      if (onPickAll) onPickAll()
      return
    }
    setActiveKb(kbId)
    writeKbQuery(kbId)
    if (onChange) onChange(kbId)
  }

  const selectValue = allKbsSelected ? ALL_KBS_SENTINEL : activeKbId

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
    <div className="flex items-center gap-3 border-b border-border/60 bg-muted/10 px-4 py-2 text-sm">
      <label
        htmlFor="kb-picker"
        className="shrink-0 text-[11px] uppercase tracking-[0.12em] text-muted-foreground"
      >
        {t('platformShell.documents.kbTabs.label', { defaultValue: '知识库' })}
      </label>

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
        <Select value={selectValue} onValueChange={handlePick}>
          <SelectTrigger id="kb-picker" className="h-8 w-[260px] rounded-lg">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {allowAllOption && (
              <SelectItem value={ALL_KBS_SENTINEL}>
                <div className="flex items-center gap-2">
                  <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
                  <span className="truncate">
                    {t('platformShell.documents.kbTabs.allKbs', {
                      defaultValue: '全部知识库',
                    })}
                  </span>
                </div>
              </SelectItem>
            )}
            {knowledgeBases.map((kb) => (
              <SelectItem key={kb.id} value={kb.id}>
                <div className="flex items-center gap-2">
                  <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
                  <span className="truncate">{kb.name || kb.id}</span>
                </div>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
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
