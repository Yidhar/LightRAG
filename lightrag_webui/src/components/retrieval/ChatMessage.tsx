import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import {
  ChevronDownIcon,
  DownloadIcon,
  FileTextIcon,
  LoaderIcon,
  ScanSearchIcon,
  SparklesIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'

import {
  downloadSourceFile,
  type Message,
  type QueryReference,
  type RetrievedChunk,
} from '@/api/lightrag'
import { ChunkImageGrid } from '@/components/retrieval/ChunkImage'
import { cn } from '@/lib/utils'

const MarkdownMessageContent = lazy(() => import('@/components/retrieval/MarkdownMessageContent'))

/** Short HH:MM(:SS) label for a message timestamp; full date/time via title. */
const formatMessageTime = (ts: number): { label: string; full: string } => {
  try {
    const d = new Date(ts)
    return { label: d.toLocaleTimeString(), full: d.toLocaleString() }
  } catch {
    return { label: '', full: '' }
  }
}

export type MessageWithError = Message & {
  id: string
  isError?: boolean
  isThinking?: boolean
  mermaidRendered?: boolean
  latexRendered?: boolean
  imageChunks?: RetrievedChunk[]
  retrievedChunks?: RetrievedChunk[]
  references?: QueryReference[]
}

export const ChatMessage = ({
  message,
  isTabActive = true,
}: {
  message: MessageWithError
  isTabActive?: boolean
}) => {
  const { t } = useTranslation()
  const [isThinkingExpanded, setIsThinkingExpanded] = useState(false)
  const [areSourcesExpanded, setAreSourcesExpanded] = useState(false)
  // Tracks which filename is currently downloading so concurrent clicks
  // can't stack up duplicate save dialogs and the spinner is scoped to
  // the row the operator actually clicked.
  const [downloadingName, setDownloadingName] = useState<string | null>(null)

  const handleDownloadSource = async (name: string) => {
    if (!name || downloadingName) return
    setDownloadingName(name)
    try {
      await downloadSourceFile(name)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.error(t('retrievePanel.chatMessage.downloadFailed', { message }))
    } finally {
      setDownloadingName(null)
    }
  }

  const { thinkingContent, displayContent, thinkingTime, isThinking } = message

  useEffect(() => {
    if (isThinking) {
      const resetTimer = requestAnimationFrame(() => {
        setIsThinkingExpanded(false)
      })

      return () => cancelAnimationFrame(resetTimer)
    }
  }, [isThinking, message.id])

  const finalThinkingContent = thinkingContent
  const finalDisplayContent =
    message.role === 'user'
      ? message.content
      : displayContent !== undefined
        ? displayContent
        : (message.content || '')

  const textSources = useMemo(
    () =>
      (message.retrievedChunks || []).filter(
        (chunk) => chunk.source_type !== 'image_vector' && Boolean(chunk.content?.trim())
      ),
    [message.retrievedChunks]
  )
  const referenceItems = useMemo(() => message.references || [], [message.references])

  // In "全部知识库" (federated) retrieval the answer is streamed as one
  // block PER knowledge base ("### Knowledge base: X"), and each block's
  // LLM uses that KB's OWN local [1][2]… citation numbering. The sources
  // come back from /query/data merged across every KB with reference_ids
  // namespaced "<kb_id>:<local_id>". Rendering them as one flat, globally-
  // numbered list made the panel disagree with the per-block citations.
  // Group by KB and number locally so each group lines up with its answer
  // block. Single-KB queries have un-namespaced ids -> one null group ->
  // the flat rendering below, unchanged.
  const sourceGroups = useMemo(() => {
    const parseKb = (refId?: string): { kb: string | null; local: string } => {
      if (!refId) return { kb: null, local: '' }
      const i = refId.indexOf(':')
      if (i <= 0) return { kb: null, local: refId }
      return { kb: refId.slice(0, i), local: refId.slice(i + 1) }
    }
    type Group = {
      kb: string | null
      chunks: { chunk: RetrievedChunk; local: string }[]
      refs: { ref: QueryReference; local: string }[]
    }
    const order: (string | null)[] = []
    const byKb = new Map<string | null, Group>()
    const ensure = (kb: string | null): Group => {
      let g = byKb.get(kb)
      if (!g) {
        g = { kb, chunks: [], refs: [] }
        byKb.set(kb, g)
        order.push(kb)
      }
      return g
    }
    for (const chunk of textSources) {
      const { kb, local } = parseKb(chunk.reference_id)
      ensure(kb).chunks.push({ chunk, local })
    }
    for (const ref of referenceItems) {
      const { kb, local } = parseKb(ref.reference_id)
      ensure(kb).refs.push({ ref, local })
    }
    return order.map((kb) => byKb.get(kb)!)
  }, [textSources, referenceItems])
  // Federated iff any source carries a KB namespace prefix.
  const isFederatedSources = sourceGroups.some((g) => g.kb !== null)

  const renderChunkCard = (chunk: RetrievedChunk, label: string, key: string) => (
    <div
      key={key}
      className="rounded-2xl border border-border/70 bg-background/90 p-3"
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-emerald-500/10 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-emerald-700 dark:text-emerald-300">
          {label}
        </span>
        {chunk.file_path && (
          <button
            type="button"
            onClick={() => handleDownloadSource(chunk.file_path!)}
            disabled={downloadingName !== null}
            title={t('retrievePanel.chatMessage.downloadSource')}
            aria-label={t('retrievePanel.chatMessage.downloadSource')}
            className="motion-standard group inline-flex max-w-full items-center gap-1.5 rounded-md px-1 py-0.5 text-xs text-muted-foreground hover:bg-emerald-500/10 hover:text-emerald-700 disabled:cursor-wait disabled:opacity-60 dark:hover:text-emerald-300"
          >
            {downloadingName === chunk.file_path ? (
              <LoaderIcon className="size-3 shrink-0 animate-spin" />
            ) : (
              <DownloadIcon className="size-3 shrink-0 opacity-70 group-hover:opacity-100" />
            )}
            <span className="truncate">{chunk.file_path}</span>
          </button>
        )}
      </div>
      <p className="whitespace-pre-wrap text-sm leading-6 text-foreground/90">
        {chunk.content}
      </p>
    </div>
  )

  const renderRefChip = (reference: QueryReference, key: string, label?: string) => (
    <button
      key={key}
      type="button"
      onClick={() => handleDownloadSource(reference.file_path)}
      disabled={downloadingName !== null || !reference.file_path}
      title={t('retrievePanel.chatMessage.downloadSource')}
      aria-label={t('retrievePanel.chatMessage.downloadSource')}
      className="motion-standard group inline-flex max-w-full items-center gap-1.5 rounded-full border border-border/70 bg-muted/20 px-3 py-1 text-xs text-muted-foreground hover:border-emerald-500/40 hover:bg-emerald-500/10 hover:text-emerald-700 disabled:cursor-wait disabled:opacity-60 dark:hover:text-emerald-300"
    >
      {downloadingName === reference.file_path ? (
        <LoaderIcon className="size-3 shrink-0 animate-spin" />
      ) : (
        <DownloadIcon className="size-3 shrink-0 opacity-70 group-hover:opacity-100" />
      )}
      {label && <span className="shrink-0 font-semibold text-emerald-700 dark:text-emerald-300">[{label}]</span>}
      <span className="truncate">{reference.file_path}</span>
    </button>
  )

  return (
    <div
      className={cn(
        'rounded-[26px] border px-4 py-3 shadow-sm sm:px-5 sm:py-4',
        message.role === 'user'
          ? 'max-w-[86%] border-emerald-500/20 bg-emerald-600 text-primary-foreground shadow-[0_16px_40px_rgba(16,185,129,0.18)]'
          : message.isError
            ? 'w-[97%] border-red-300/70 bg-red-50 text-red-700 dark:border-red-500/30 dark:bg-red-950/40 dark:text-red-300'
            : 'w-[97%] border-border/70 bg-background/90'
      )}
    >
      {message.role === 'assistant' && (
        <div className="mb-3 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
          <SparklesIcon className="size-3.5 text-emerald-500" />
          <span>{t('retrievePanel.chatMessage.answerLabel')}</span>
        </div>
      )}

      {message.role === 'assistant' && (isThinking || thinkingTime !== null) && (
        <div className={cn('mb-3 rounded-2xl border border-dashed border-emerald-500/25 bg-emerald-500/[0.04] p-3', !isTabActive && 'opacity-60')}>
          <div
            className="flex cursor-pointer select-none items-center gap-2 text-sm text-slate-600 transition-colors duration-200 hover:text-slate-900 dark:text-slate-300 dark:hover:text-slate-100"
            onClick={() => {
              if (finalThinkingContent && finalThinkingContent.trim() !== '') {
                setIsThinkingExpanded(!isThinkingExpanded)
              }
            }}
          >
            {isThinking ? (
              <>
                {isTabActive && <LoaderIcon className="size-4 animate-spin text-emerald-500" />}
                <span>{t('retrievePanel.chatMessage.thinking')}</span>
              </>
            ) : (
              typeof thinkingTime === 'number' && (
                <span>{t('retrievePanel.chatMessage.thinkingTime', { time: thinkingTime })}</span>
              )
            )}
            {finalThinkingContent && finalThinkingContent.trim() !== '' && (
              <ChevronDownIcon
                className={`ml-auto size-4 shrink-0 transition-transform ${isThinkingExpanded ? 'rotate-180' : ''}`}
              />
            )}
          </div>

          {isThinkingExpanded && finalThinkingContent && finalThinkingContent.trim() !== '' && (
            <div className="prose mt-3 max-w-none break-words border-l-2 border-emerald-500/20 pl-4 text-sm text-foreground prose-p:my-1 prose-headings:my-2 dark:prose-invert [&_.footnotes]:mt-6 [&_.footnotes]:border-border [&_.footnotes]:pt-3 [&_.footnotes_ol]:text-xs [&_.footnotes_li]:my-0.5 [&_a[href^='#fn']]:text-primary [&_a[href^='#fn']]:no-underline [&_a[href^='#fn']]:hover:underline [&_a[href^='#fnref']]:text-primary [&_a[href^='#fnref']]:no-underline [&_a[href^='#fnref']]:hover:underline [&_del]:line-through [&_ins]:decoration-green-500 [&_ins]:underline [&_mark]:bg-yellow-200 [&_mark]:dark:bg-yellow-800 [&_sub]:text-[0.75em] [&_sub]:leading-[0] [&_sub]:align-[-0.2em] [&_sup]:text-[0.75em] [&_sup]:leading-[0] [&_sup]:align-[0.1em] [&_u]:underline">
              {isThinking && (
                <div className="mb-2 text-xs italic text-slate-400 dark:text-slate-300">
                  {t('retrievePanel.chatMessage.thinkingInProgress')}
                </div>
              )}

              <Suspense fallback={<div className="whitespace-pre-wrap">{finalThinkingContent}</div>}>
                <MarkdownMessageContent
                  content={finalThinkingContent}
                  messageRole={message.role}
                  renderAsDiagram={message.mermaidRendered ?? false}
                  latexRendered={message.latexRendered ?? true}
                  mode="thinking"
                />
              </Suspense>
            </div>
          )}
        </div>
      )}

      {finalDisplayContent && (
        <div className="relative">
          <div
            className={cn(
              'prose max-w-none break-words text-sm prose-headings:mb-2 prose-headings:mt-4 prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-1 dark:prose-invert [&_.katex]:text-current [&_.katex-display]:my-4 [&_.katex-display]:max-w-full [&_.katex-display_>.base]:overflow-x-auto [&_.footnotes]:mt-8 [&_.footnotes]:pt-4 [&_.footnotes_ol]:text-sm [&_.footnotes_li]:my-1 [&_del]:line-through [&_ins]:decoration-green-500 [&_ins]:underline [&_mark]:bg-yellow-200 [&_mark]:dark:bg-yellow-800 [&_sub]:text-[0.75em] [&_sub]:leading-[0] [&_sub]:align-[-0.2em] [&_sup]:text-[0.75em] [&_sup]:leading-[0] [&_sup]:align-[0.1em]',
              message.role === 'user' ? 'text-primary-foreground' : 'text-foreground'
            )}
          >
            <Suspense fallback={<div className="whitespace-pre-wrap">{finalDisplayContent}</div>}>
              <MarkdownMessageContent
                content={finalDisplayContent}
                messageRole={message.role}
                renderAsDiagram={message.mermaidRendered ?? false}
                latexRendered={message.latexRendered ?? true}
                mode="main"
              />
            </Suspense>
          </div>

          {message.role !== 'user' && message.imageChunks && message.imageChunks.length > 0 && (
            <div className="mt-5 space-y-3">
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                <ScanSearchIcon className="size-3.5 text-emerald-500" />
                <span>{t('retrievePanel.retrieval.retrievedImages')}</span>
              </div>
              <ChunkImageGrid chunks={message.imageChunks} />
            </div>
          )}

          {message.role !== 'user' && (textSources.length > 0 || referenceItems.length > 0) && (
            <div className="mt-5 rounded-2xl border border-border/70 bg-muted/25 p-3">
              <button
                type="button"
                onClick={() => setAreSourcesExpanded((prev) => !prev)}
                className="flex w-full items-center gap-3 text-left"
              >
                <div className="flex size-9 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                  <FileTextIcon className="size-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-foreground">
                    {t('retrievePanel.chatMessage.sources')}
                  </p>
                  <p className="truncate text-xs text-muted-foreground">
                    {t('retrievePanel.chatMessage.sourcesCount', { count: textSources.length || referenceItems.length })}
                  </p>
                </div>
                <ChevronDownIcon
                  className={`size-4 text-muted-foreground transition-transform ${areSourcesExpanded ? 'rotate-180' : ''}`}
                />
              </button>

              {areSourcesExpanded && (
                <div className="mt-4 grid gap-3">
                  {sourceGroups.map((group, groupIndex) => (
                    <div key={group.kb ?? '__single__'} className="grid gap-3">
                      {/* Per-KB header so a federated ("全部知识库") answer's
                          "### Knowledge base: X" block visually lines up with
                          its own sources. Single-KB queries (kb === null)
                          render no header — same look as before. */}
                      {group.kb !== null && (
                        <div className="flex items-center gap-2 pt-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                          <span className="rounded-full bg-emerald-500/10 px-2.5 py-1 text-emerald-700 dark:text-emerald-300">
                            {t('retrievePanel.chatMessage.kbGroupLabel', {
                              defaultValue: '知识库',
                            })}
                          </span>
                          <span className="truncate font-mono normal-case tracking-normal text-foreground/80">
                            {group.kb}
                          </span>
                        </div>
                      )}

                      {group.chunks.slice(0, 6).map(({ chunk, local }, index) =>
                        renderChunkCard(
                          chunk,
                          // Federated: use the KB-local citation number so it
                          // matches that block's [n]. Single-KB: running index.
                          isFederatedSources && local
                            ? local
                            : t('retrievePanel.chatMessage.sourceLabel', {
                              index: index + 1,
                            }),
                          `${group.kb ?? 's'}-${chunk.chunk_id || chunk.reference_id || 'chunk'}-${groupIndex}-${index}`
                        )
                      )}

                      {group.refs.length > 0 && (
                        <div className="rounded-2xl border border-dashed border-border/70 bg-background/70 p-3">
                          <div className="flex flex-wrap gap-2">
                            {group.refs.slice(0, 8).map(({ ref, local }, refIndex) =>
                              renderRefChip(
                                ref,
                                `${group.kb ?? 'r'}-${ref.reference_id}-${refIndex}`,
                                isFederatedSources ? local : undefined
                              )
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {isTabActive && (() => {
        const hasVisibleContent = finalDisplayContent && finalDisplayContent.trim() !== ''
        const isLoadingState = !hasVisibleContent && !isThinking && !thinkingTime
        return isLoadingState && <LoaderIcon className="animate-spin duration-2000" />
      })()}

      {/* Timestamp: labels when the question was asked / answer produced. */}
      {typeof message.timestamp === 'number' && message.timestamp > 0 && (() => {
        const { label, full } = formatMessageTime(message.timestamp)
        if (!label) return null
        return (
          <div
            title={full}
            className={cn(
              'mt-2 text-right text-[11px] tabular-nums',
              message.role === 'user'
                ? 'text-primary-foreground/70'
                : 'text-muted-foreground'
            )}
          >
            {label}
          </div>
        )
      })()}
    </div>
  )
}
