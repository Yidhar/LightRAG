import { useEffect, useMemo, useState } from 'react'
import { BookOpenTextIcon, Link2Icon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import {
  linkKnowledgeBaseToWorkspace,
  listAllKnowledgeBases,
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

interface LinkKnowledgeBaseDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The workspace we're linking a KB *into*. */
  workspaceId: string
  /** KB ids already linked to this workspace — filtered out of the picker. */
  alreadyLinkedIds: string[]
  /** Fires after a successful link; caller typically refreshes the KB list. */
  onLinked?: (kb: KnowledgeBaseRecord) => void
}

/**
 * Pick a KB from the global pool and link it into the current workspace.
 *
 * Backs onto the new v2 sharing model: one KB, many workspace
 * references. The dialog lazily fetches ``GET /kb`` on open so KBs
 * newly created in other workspaces during this session show up
 * without a page reload.
 */
export default function LinkKnowledgeBaseDialog({
  open,
  onOpenChange,
  workspaceId,
  alreadyLinkedIds,
  onLinked,
}: LinkKnowledgeBaseDialogProps) {
  const { t } = useTranslation()
  const [allKbs, setAllKbs] = useState<KnowledgeBaseRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedKbId, setSelectedKbId] = useState<string>('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    listAllKnowledgeBases()
      .then((response) => {
        if (cancelled) return
        setAllKbs(response.items)
      })
      .catch((err) => {
        if (!cancelled) {
          toast.error(errorMessage(err))
          setAllKbs([])
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  // Candidates = global pool minus KBs already linked here. Sorted by
  // name so the dropdown reads naturally.
  const candidates = useMemo(() => {
    const alreadyLinkedSet = new Set(alreadyLinkedIds)
    return allKbs
      .filter((kb) => !alreadyLinkedSet.has(kb.id))
      .sort((a, b) =>
        (a.name || a.id).localeCompare(b.name || b.id, undefined, {
          sensitivity: 'base',
        })
      )
  }, [allKbs, alreadyLinkedIds])

  // Reset the selected id whenever the candidate list changes so it
  // always points at something valid.
  useEffect(() => {
    setSelectedKbId(candidates[0]?.id ?? '')
  }, [candidates])

  const handleLink = async () => {
    if (!selectedKbId) return
    try {
      setSubmitting(true)
      const response = await linkKnowledgeBaseToWorkspace(
        workspaceId,
        selectedKbId
      )
      toast.success(response.message)
      onOpenChange(false)
      const linkedKb = allKbs.find((kb) => kb.id === selectedKbId)
      if (onLinked && linkedKb) onLinked(linkedKb)
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
            {t('platformShell.workspaceDirectory.linkDialog.title', {
              defaultValue: '链接已有知识库',
            })}
          </DialogTitle>
          <DialogDescription>
            {t('platformShell.workspaceDirectory.linkDialog.description', {
              defaultValue:
                '把一个已存在的知识库加入当前工作区。知识库本身（数据、图谱、向量）不会被复制，多个工作区共享同一份内容。',
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          <label
            htmlFor="link-kb-picker"
            className="text-xs font-medium text-muted-foreground"
          >
            {t('platformShell.workspaceDirectory.linkDialog.pickerLabel', {
              defaultValue: '选择知识库',
            })}
          </label>
          {loading ? (
            <div className="rounded-lg border border-dashed border-border/60 px-3 py-2 text-sm text-muted-foreground">
              {t('platformShell.common.loading', { defaultValue: '加载中…' })}
            </div>
          ) : candidates.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border/60 px-3 py-3 text-sm text-muted-foreground">
              {t('platformShell.workspaceDirectory.linkDialog.noCandidates', {
                defaultValue:
                  '没有可链接的知识库 — 所有已存在的知识库都已经在这个工作区里了。',
              })}
            </div>
          ) : (
            <Select
              value={selectedKbId}
              onValueChange={setSelectedKbId}
              disabled={submitting}
            >
              <SelectTrigger id="link-kb-picker" className="h-10 rounded-lg">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {candidates.map((kb) => (
                  <SelectItem key={kb.id} value={kb.id}>
                    <div className="flex min-w-0 items-center gap-2">
                      <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
                      <span className="truncate">{kb.name || kb.id}</span>
                      {kb.category && (
                        <span className="ml-1 shrink-0 text-[10px] text-muted-foreground">
                          · {kb.category}
                        </span>
                      )}
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
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
            onClick={handleLink}
            disabled={!selectedKbId || submitting || candidates.length === 0}
          >
            <Link2Icon className="size-4" aria-hidden="true" />
            {submitting
              ? t('platformShell.common.saving', { defaultValue: '保存中…' })
              : t('platformShell.workspaceDirectory.linkDialog.confirm', {
                defaultValue: '链接到工作区',
              })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
