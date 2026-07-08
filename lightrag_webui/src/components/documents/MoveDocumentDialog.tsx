import { useEffect, useState } from 'react'
import { BookOpenTextIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import {
  copyDocument,
  listKnowledgeBases,
  moveDocument,
  type KnowledgeBaseRecord,
} from '@/api/lightrag'
import { errorMessage } from '@/lib/utils'
import Button from '@/components/ui/Button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'

interface MoveDocumentDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The document being moved/copied. Used for labels + pre-op display. */
  docId: string | null
  docLabel?: string | null
  /** Current workspace id — used to fetch the candidate KB list. */
  workspaceId: string
  /** Current KB id — excluded from the selector (you can't move/copy to self). */
  currentKbId: string
  /**
   * ``move`` (default): re-ingest in target + background delete from source.
   * ``copy``: re-ingest in target, leave the source copy intact.
   */
  mode?: 'move' | 'copy'
  /** Called once the backend confirms; caller typically refreshes the list. */
  onMoved?: () => void
}

/**
 * Move one document between KBs inside the same workspace.
 *
 * Implementation notes:
 * - The KB list is loaded lazily on open so newly-created KBs appear
 *   without forcing a page reload.
 * - We filter out the current KB from the dropdown; moving to self is
 *   a no-op and the backend would 400 anyway.
 * - The actual move is "re-ingest in target + background delete from
 *   source" on the backend. The UI just triggers it and toasts the
 *   result; the target KB's pipeline will run the extraction async.
 */
export default function MoveDocumentDialog({
  open,
  onOpenChange,
  docId,
  docLabel,
  workspaceId,
  currentKbId,
  mode = 'move',
  onMoved,
}: MoveDocumentDialogProps) {
  const { t } = useTranslation()
  const isCopy = mode === 'copy'
  const [kbs, setKbs] = useState<KnowledgeBaseRecord[]>([])
  const [loadingKbs, setLoadingKbs] = useState(false)
  const [targetKbId, setTargetKbId] = useState<string>('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoadingKbs(true)
    listKnowledgeBases(workspaceId)
      .then((response) => {
        if (cancelled) return
        const candidates = response.items.filter((kb) => kb.id !== currentKbId)
        setKbs(candidates)
        setTargetKbId(candidates[0]?.id ?? '')
      })
      .catch(() => {
        if (!cancelled) setKbs([])
      })
      .finally(() => {
        if (!cancelled) setLoadingKbs(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, workspaceId, currentKbId])

  const handleSubmit = async () => {
    if (!docId || !targetKbId) return
    try {
      setSubmitting(true)
      const response = isCopy
        ? await copyDocument(docId, targetKbId)
        : await moveDocument(docId, targetKbId)
      toast.success(response.message)
      onOpenChange(false)
      if (onMoved) onMoved()
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (submitting) return
        onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isCopy
              ? t('documentPanel.copyDocument.title', {
                defaultValue: '复制到其他知识库',
              })
              : t('documentPanel.moveDocument.title', {
                defaultValue: '移动到其他知识库',
              })}
          </DialogTitle>
          <DialogDescription>
            {isCopy
              ? t('documentPanel.copyDocument.description', {
                defaultValue:
                  '目标知识库会重新抽取文档的实体与关系；源知识库中的内容保持不变。',
              })
              : t('documentPanel.moveDocument.description', {
                defaultValue:
                  '目标知识库会重新抽取文档的实体与关系；源知识库中的内容会在后台清理。',
              })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="rounded-xl border border-border/60 bg-muted/10 px-3 py-2 text-sm">
            <div className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
              {isCopy
                ? t('documentPanel.copyDocument.movingLabel', {
                  defaultValue: '待复制文档',
                })
                : t('documentPanel.moveDocument.movingLabel', {
                  defaultValue: '待移动文档',
                })}
            </div>
            <div className="mt-1 flex items-center gap-2 truncate font-medium text-foreground">
              <BookOpenTextIcon
                className="size-3.5 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
              <span className="truncate">{docLabel || docId}</span>
            </div>
          </div>

          <div className="space-y-1.5">
            <label
              htmlFor="move-target-kb"
              className="text-xs font-medium text-muted-foreground"
            >
              {t('documentPanel.moveDocument.targetKbLabel', {
                defaultValue: '目标知识库',
              })}
            </label>
            {loadingKbs ? (
              <div className="rounded-lg border border-dashed border-border/60 px-3 py-2 text-sm text-muted-foreground">
                {t('platformShell.common.loading', { defaultValue: '加载中…' })}
              </div>
            ) : kbs.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border/60 px-3 py-3 text-sm text-muted-foreground">
                {t('documentPanel.moveDocument.noOtherKb', {
                  defaultValue:
                    '该工作区下没有其他知识库 — 先在"工作区管理"里新建一个再试。',
                })}
              </div>
            ) : (
              <Select
                value={targetKbId}
                onValueChange={setTargetKbId}
                disabled={submitting}
              >
                <SelectTrigger id="move-target-kb" className="h-10 rounded-lg">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {kbs.map((kb) => (
                    <SelectItem key={kb.id} value={kb.id}>
                      <div className="flex items-center gap-2">
                        <BookOpenTextIcon
                          className="size-3.5"
                          aria-hidden="true"
                        />
                        <span className="truncate">{kb.name || kb.id}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            {t('common.cancel', { defaultValue: '取消' })}
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!targetKbId || submitting || kbs.length === 0}
          >
            {submitting
              ? isCopy
                ? t('documentPanel.copyDocument.copying', { defaultValue: '复制中…' })
                : t('documentPanel.moveDocument.moving', { defaultValue: '移动中…' })
              : isCopy
                ? t('documentPanel.copyDocument.confirm', { defaultValue: '确认复制' })
                : t('documentPanel.moveDocument.confirm', { defaultValue: '确认移动' })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
