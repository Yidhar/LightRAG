/**
 * ChunkImage — renders a single image chunk returned by the multimodal
 * retrieval pipeline (source_type === 'image_vector').
 *
 * The component:
 *   1. Fetches the raw bytes via the authenticated axios instance
 *      (so api-key / auth headers are honored) and wraps the blob in
 *      an object URL for <img src>.
 *   2. Fetches the sidecar metadata (caption JSON + source_doc_id
 *      backlink) so the thumbnail caption is meaningful.
 *   3. Opens a full-size modal (Dialog) on click with the image and
 *      its full detailed_description + detected_entities list.
 *
 * Designed to degrade gracefully: if metadata is 404 we still render
 * the image with file_path as the caption; if the blob fails to load
 * we show a placeholder with the blob_id so the user knows which image
 * is missing.
 *
 * Phase 6 — LightRAG multimodal WebUI.
 */

import type { TFunction } from 'i18next'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ImageIcon, LoaderIcon } from 'lucide-react'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import {
  fetchImageBlobUrl,
  fetchImageMetadata,
  type ImageContextChunk,
  type ImageMetadata,
  type RetrievedChunk
} from '@/api/lightrag'

interface ChunkImageProps {
  /** The chunk returned by /query/data — must have image_blob_id set. */
  chunk: RetrievedChunk
}

/**
 * Short display caption for the thumbnail row.
 *
 * Prefers the vision model's single-line `caption` field, falls back to
 * the combined category/sub_type, and finally the filename. Never returns
 * an empty string so the UI always has something to show.
 */
function resolveShortCaption(
  chunk: RetrievedChunk,
  metadata: ImageMetadata | null
): string {
  const cj = metadata?.caption_json
  if (cj?.caption && cj.caption.trim()) {
    return cj.caption.trim()
  }
  const parts = [cj?.image_category, cj?.sub_type].filter(
    (p): p is string => Boolean(p && p.trim())
  )
  if (parts.length > 0) {
    return parts.join(' / ')
  }
  if (chunk.file_path) {
    // Pick just the filename — full paths are noisy.
    const segments = chunk.file_path.split(/[\\/]/)
    return segments[segments.length - 1] || chunk.file_path
  }
  return chunk.image_blob_id ?? chunk.chunk_id ?? 'image'
}

function resolveLocationSummary(metadata: ImageMetadata | null, t: TFunction): string | null {
  if (!metadata) {
    return null
  }

  const parts: string[] = []
  if (typeof metadata.source_page === 'number') {
    parts.push(t('retrievePanel.retrieval.imageLocation.pdfPage', { page: metadata.source_page }))
  }
  if (
    typeof metadata.source_printed_page === 'number' &&
    metadata.source_printed_page !== metadata.source_page
  ) {
    parts.push(t('retrievePanel.retrieval.imageLocation.documentPage', { page: metadata.source_printed_page }))
  }
  if (
    metadata.source_page_label &&
    metadata.source_page_label !== String(metadata.source_page ?? '') &&
    metadata.source_page_label !== String(metadata.source_printed_page ?? '')
  ) {
    parts.push(t('retrievePanel.retrieval.imageLocation.pageLabel', { label: metadata.source_page_label }))
  }

  const bbox = metadata.source_bbox
  if (bbox) {
    const x0 = Number(bbox.x0 ?? bbox.l)
    const y0 = Number(bbox.y0 ?? bbox.t)
    const width = Number(
      bbox.width ?? ((bbox.x1 ?? bbox.r) as number) - ((bbox.x0 ?? bbox.l) as number)
    )
    const height = Number(
      bbox.height ?? ((bbox.y1 ?? bbox.b) as number) - ((bbox.y0 ?? bbox.t) as number)
    )

    if ([x0, y0, width, height].every((v) => Number.isFinite(v))) {
      parts.push(
        t('retrievePanel.retrieval.imageLocation.bounds', {
          x: x0.toFixed(1),
          y: y0.toFixed(1),
          w: width.toFixed(1),
          h: height.toFixed(1),
        })
      )
    }
  }

  return parts.length > 0 ? parts.join(' · ') : null
}

function resolveContextText(metadata: ImageMetadata | null): string | null {
  const extra = metadata?.extra ?? null
  const fromExtra =
    extra && typeof extra === 'object'
      ? String(
        (extra as Record<string, unknown>).context_text ??
          (extra as Record<string, unknown>).page_text_excerpt ??
          ''
      ).trim()
      : ''

  const contextText = String(metadata?.context_text ?? fromExtra).trim()
  return contextText || null
}

function resolveContextChunks(metadata: ImageMetadata | null): ImageContextChunk[] {
  const direct = Array.isArray(metadata?.context_chunks) ? metadata.context_chunks : []
  if (direct.length > 0) {
    return direct
  }

  const extra = metadata?.extra
  if (extra && typeof extra === 'object') {
    const fromExtra = (extra as Record<string, unknown>).context_chunks
    if (Array.isArray(fromExtra)) {
      return fromExtra as ImageContextChunk[]
    }
  }

  return []
}

export function ChunkImage({ chunk }: ChunkImageProps) {
  const { t } = useTranslation()
  const [imageUrl, setImageUrl] = useState<string | null>(null)
  const [metadata, setMetadata] = useState<ImageMetadata | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const blobId = chunk.image_blob_id

  useEffect(() => {
    if (!blobId) {
      return
    }

    let cancelled = false
    let createdUrl: string | null = null

    ;(async () => {
      try {
        const [url, meta] = await Promise.all([
          fetchImageBlobUrl(blobId),
          fetchImageMetadata(blobId).catch(() => null)
        ])
        if (cancelled) {
          // Component unmounted before fetch resolved — release the
          // object URL we just created to avoid leaks.
          URL.revokeObjectURL(url)
          return
        }
        createdUrl = url
        setImageUrl(url)
        setMetadata(meta)
      } catch (err) {
        if (cancelled) return
        const message =
          err instanceof Error ? err.message : String(err)
        setLoadError(message)
      }
    })()

    return () => {
      cancelled = true
      if (createdUrl) {
        URL.revokeObjectURL(createdUrl)
      }
    }
  }, [blobId])

  if (!blobId) {
    return null
  }

  const shortCaption = resolveShortCaption(chunk, metadata)
  const cj = metadata?.caption_json
  const locationSummary = resolveLocationSummary(metadata, t)
  const contextText = resolveContextText(metadata)
  const contextChunks = resolveContextChunks(metadata)

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          className="group border-border bg-muted/30 hover:border-primary/50 hover:bg-muted/60 relative flex w-full max-w-[220px] flex-col gap-2 overflow-hidden rounded-md border p-2 text-left transition-colors"
          title={shortCaption}
        >
          <div className="bg-muted relative flex aspect-[4/3] w-full items-center justify-center overflow-hidden rounded">
            {imageUrl ? (
              <img
                src={imageUrl}
                alt={shortCaption}
                className="h-full w-full object-cover transition-transform group-hover:scale-[1.02]"
                loading="lazy"
              />
            ) : loadError ? (
              <div className="text-muted-foreground flex flex-col items-center justify-center gap-1 p-2 text-center text-[10px]">
                <ImageIcon className="size-6" />
                <span>{t('retrievePanel.retrieval.imageLoadError')}</span>
              </div>
            ) : (
              <LoaderIcon className="text-muted-foreground size-6 animate-spin" />
            )}
          </div>
          <div className="text-foreground line-clamp-2 text-xs leading-snug">
            {shortCaption}
          </div>
          {chunk.file_path && (
            <div className="text-muted-foreground truncate text-[10px]">
              {chunk.file_path.split(/[\\/]/).slice(-1)[0]}
            </div>
          )}
        </button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-3xl gap-3 overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="break-words">{shortCaption}</DialogTitle>
          {(cj?.image_category || cj?.sub_type) && (
            <DialogDescription className="text-xs">
              {[cj?.image_category, cj?.sub_type]
                .filter((p): p is string => Boolean(p && p.trim()))
                .join(' / ')}
            </DialogDescription>
          )}
        </DialogHeader>
        {locationSummary && (
          <div className="text-muted-foreground text-xs">
            {locationSummary}
          </div>
        )}
        <div className="bg-muted/40 flex w-full items-center justify-center overflow-hidden rounded">
          {imageUrl ? (
            <img
              src={imageUrl}
              alt={shortCaption}
              className="max-h-[60vh] w-auto max-w-full object-contain"
            />
          ) : loadError ? (
            <div className="text-muted-foreground flex flex-col items-center gap-2 p-8">
              <ImageIcon className="size-10" />
              <span className="text-xs">
                {t('retrievePanel.retrieval.imageLoadError')}
              </span>
            </div>
          ) : (
            <div className="flex items-center justify-center p-8">
              <LoaderIcon className="text-muted-foreground size-8 animate-spin" />
            </div>
          )}
        </div>
        {cj?.detailed_description && (
          <div className="space-y-1">
            <div className="text-muted-foreground text-[11px] font-medium uppercase tracking-wide">
              {t('retrievePanel.retrieval.imageDescription')}
            </div>
            <p className="text-foreground whitespace-pre-wrap text-sm leading-relaxed">
              {cj.detailed_description}
            </p>
          </div>
        )}
        {cj?.detected_entities && cj.detected_entities.length > 0 && (
          <div className="space-y-1">
            <div className="text-muted-foreground text-[11px] font-medium uppercase tracking-wide">
              {t('retrievePanel.retrieval.imageEntities')}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {cj.detected_entities.map((entity, i) => (
                <span
                  key={`${entity}-${i}`}
                  className="bg-muted text-foreground rounded-md px-2 py-0.5 text-[11px]"
                >
                  {entity}
                </span>
              ))}
            </div>
          </div>
        )}
        {contextText && (
          <div className="space-y-1">
            <div className="text-muted-foreground text-[11px] font-medium uppercase tracking-wide">
              {t('retrievePanel.retrieval.imageContext')}
            </div>
            <p className="text-foreground whitespace-pre-wrap text-sm leading-relaxed">
              {contextText}
            </p>
          </div>
        )}
        {contextChunks.length > 0 && (
          <div className="space-y-1">
            <div className="text-muted-foreground text-[11px] font-medium uppercase tracking-wide">
              {t('retrievePanel.retrieval.imageContextChunks')}
            </div>
            <div className="space-y-2">
              {contextChunks.map((contextChunk, index) => {
                const chunkKey =
                  contextChunk.chunk_id ??
                  `${contextChunk.chunk_order_index ?? 'chunk'}-${index}`
                const chunkTitle = t('retrievePanel.retrieval.imageContextChunk', {
                  index:
                    typeof contextChunk.chunk_order_index === 'number'
                      ? contextChunk.chunk_order_index + 1
                      : index + 1,
                })
                return (
                  <div
                    key={chunkKey}
                    className="bg-muted/30 border-border rounded-md border p-2"
                  >
                    <div className="text-muted-foreground mb-1 text-[10px] font-medium uppercase tracking-wide">
                      {chunkTitle}
                    </div>
                    <p className="text-foreground whitespace-pre-wrap text-sm leading-relaxed">
                      {contextChunk.content ?? ''}
                    </p>
                  </div>
                )
              })}
            </div>
          </div>
        )}
        <div className="text-muted-foreground space-y-0.5 border-t pt-2 text-[10px]">
          <div>
            <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.blobId')}</span> {blobId}
          </div>
          {metadata?.source_file_path && (
            <div className="break-all">
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.source')}</span>{' '}
              {metadata.source_file_path}
            </div>
          )}
          {metadata?.source_doc_id && (
            <div className="break-all">
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.docId')}</span>{' '}
              {metadata.source_doc_id}
            </div>
          )}
          {typeof metadata?.picture_index === 'number' && (
            <div>
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.pictureIndex')}</span>{' '}
              {metadata.picture_index}
            </div>
          )}
          {typeof metadata?.page_picture_index === 'number' && (
            <div>
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.pagePictureIndex')}</span>{' '}
              {metadata.page_picture_index}
            </div>
          )}
          {metadata?.extraction_mode && (
            <div>
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.extraction')}</span>{' '}
              {metadata.extraction_mode}
            </div>
          )}
          {typeof metadata?.native_xref === 'number' && (
            <div>
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.nativeXref')}</span>{' '}
              {metadata.native_xref}
            </div>
          )}
          {Array.isArray(metadata?.merged_extraction_modes) &&
            metadata.merged_extraction_modes.length > 0 && (
            <div className="break-all">
              <span className="font-medium">{t('retrievePanel.retrieval.imageMetadata.mergedExtractionModes')}</span>{' '}
              {metadata.merged_extraction_modes.join(', ')}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

interface ChunkImageGridProps {
  chunks: RetrievedChunk[]
}

/**
 * Grid wrapper that renders every image chunk in `chunks` as a
 * ChunkImage. Text chunks (source_type !== 'image_vector') are filtered
 * out so callers can pass the full chunks array unchanged.
 */
export function ChunkImageGrid({ chunks }: ChunkImageGridProps) {
  const { t } = useTranslation()
  const imageChunks = chunks.filter(
    (c) => c.source_type === 'image_vector' && Boolean(c.image_blob_id)
  )

  if (imageChunks.length === 0) {
    return null
  }

  return (
    <div className="mt-3 flex flex-col gap-2">
      <div className="text-muted-foreground text-[11px] font-medium uppercase tracking-wide">
        {t('retrievePanel.retrieval.retrievedImages')}{' '}
        ({imageChunks.length})
      </div>
      <div className="flex flex-wrap gap-2">
        {imageChunks.map((chunk) => (
          <ChunkImage
            key={chunk.image_blob_id ?? chunk.chunk_id}
            chunk={chunk}
          />
        ))}
      </div>
    </div>
  )
}
