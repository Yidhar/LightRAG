import { useState, useCallback, useEffect } from 'react'
import { FileRejection } from 'react-dropzone'
import Button from '@/components/ui/Button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import FileUploader from '@/components/ui/FileUploader'
import { toast } from 'sonner'
import { errorMessage } from '@/lib/utils'
import {
  DocActionResponse,
  listKnowledgeBases,
  type KnowledgeBaseRecord,
  uploadDocument,
} from '@/api/lightrag'
import {
  defaultMaxUploadSize,
  supportedFileTypesDescription
} from '@/lib/constants'
import { useKBStore } from '@/stores/kb'
import { defaultKnowledgeBaseId } from '@/app/routes'

import { BookOpenTextIcon, UploadIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface UploadDocumentsDialogProps {
  disabled?: boolean
  disabledReason?: string
  // Optional workspace override — defaults to the active workspace in
  // the global store. Passed down from DocumentsPage so the dialog
  // knows which workspace's KB list to offer as upload targets.
  workspaceId?: string
  onDocumentsUploaded?: (payload: {
    successfulUploads: Array<{
      fileName: string
      result: DocActionResponse
    }>
    takeoverRebuilds: Array<{
      fileName: string
      result: DocActionResponse
    }>
  }) => Promise<void> | void
}

export default function UploadDocumentsDialog({
  disabled = false,
  disabledReason,
  workspaceId,
  onDocumentsUploaded,
}: UploadDocumentsDialogProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [progresses, setProgresses] = useState<Record<string, number>>({})
  const [fileErrors, setFileErrors] = useState<Record<string, string>>({})

  // Target KB picker — defaults to whatever the user has already
  // selected via the KBTabs dropdown on the documents page. The user
  // can override here for a one-shot upload into a different KB
  // without disturbing the global active-KB state.
  const activeWorkspaceId = useKBStore((s) => s.activeWorkspaceId)
  const storeActiveKbId = useKBStore((s) => s.activeKbId) ?? defaultKnowledgeBaseId
  const resolvedWorkspaceId = workspaceId || activeWorkspaceId || ''
  const [kbs, setKbs] = useState<KnowledgeBaseRecord[]>([])
  const [targetKbId, setTargetKbId] = useState<string>(storeActiveKbId)

  useEffect(() => {
    let cancelled = false
    if (!open || !resolvedWorkspaceId) return
    // Refresh the KB list every time the dialog opens so newly-created
    // KBs show up immediately without a page reload.
    listKnowledgeBases(resolvedWorkspaceId)
      .then((response) => {
        if (cancelled) return
        setKbs(response.items)
        // If the current selection was deleted out from under us, fall
        // back to the store's active KB (which KBTabs keeps in sync).
        if (!response.items.some((kb) => kb.id === targetKbId)) {
          setTargetKbId(storeActiveKbId)
        }
      })
      .catch(() => {
        if (!cancelled) setKbs([])
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, resolvedWorkspaceId])

  // Re-seed selection whenever the global active KB changes so the
  // dialog's default matches the page context users just came from.
  useEffect(() => {
    setTargetKbId(storeActiveKbId)
  }, [storeActiveKbId])

  const buildTakeoverSuccessMessage = useCallback((fileName: string, result: DocActionResponse) => {
    if (result.doc_id) {
      return t('documentPanel.uploadDocuments.takeoverSuccessWithDocId', {
        name: fileName,
        docId: result.doc_id
      })
    }

    return t('documentPanel.uploadDocuments.takeoverSuccess', {
      name: fileName
    })
  }, [t])

  const handleRejectedFiles = useCallback(
    (rejectedFiles: FileRejection[]) => {
      // Process rejected files and add them to fileErrors
      rejectedFiles.forEach(({ file, errors }) => {
        // Get the first error message
        let errorMsg = errors[0]?.message || t('documentPanel.uploadDocuments.fileUploader.fileRejected', { name: file.name })

        // Simplify error message for unsupported file types
        if (errorMsg.includes('file-invalid-type')) {
          errorMsg = t('documentPanel.uploadDocuments.fileUploader.unsupportedType')
        }

        // Set progress to 100% to display error message
        setProgresses((pre) => ({
          ...pre,
          [file.name]: 100
        }))

        // Add error message to fileErrors
        setFileErrors(prev => ({
          ...prev,
          [file.name]: errorMsg
        }))
      })
    },
    [setProgresses, setFileErrors, t]
  )

  const handleDocumentsUpload = useCallback(
    async (filesToUpload: File[]) => {
      if (disabled) {
        return
      }
      setIsUploading(true)
      let hasSuccessfulUpload = false

      // Only clear errors for files that are being uploaded, keep errors for rejected files
      setFileErrors(prev => {
        const newErrors = { ...prev };
        filesToUpload.forEach(file => {
          delete newErrors[file.name];
        });
        return newErrors;
      });

      // Show uploading toast
      const toastId = toast.loading(t('documentPanel.uploadDocuments.batch.uploading'))

      try {
        // Track errors locally to ensure we have the final state
        const uploadErrors: Record<string, string> = {}
        const takeoverMessages: string[] = []
        const successfulUploads: Array<{ fileName: string; result: DocActionResponse }> = []

        // Create a collator that supports Chinese sorting
        const collator = new Intl.Collator(['zh-CN', 'en'], {
          sensitivity: 'accent',  // consider basic characters, accents, and case
          numeric: true           // enable numeric sorting, e.g., "File 10" will be after "File 2"
        });
        const sortedFiles = [...filesToUpload].sort((a, b) =>
          collator.compare(a.name, b.name)
        );

        // Upload files in sequence, not parallel
        for (const file of sortedFiles) {
          try {
            // Initialize upload progress
            setProgresses((pre) => ({
              ...pre,
              [file.name]: 0
            }))

            const result = await uploadDocument(
              file,
              (percentCompleted: number) => {
                console.debug(t('documentPanel.uploadDocuments.single.uploading', { name: file.name, percent: percentCompleted }))
                setProgresses((pre) => ({
                  ...pre,
                  [file.name]: percentCompleted
                }))
              },
              // Explicit target KB for this upload batch. Backend falls
              // back to the request's workspace context when we omit,
              // but passing it guarantees the dialog's selection wins
              // even if a tab-switch races mid-upload.
              targetKbId
            )

            if (result.status === 'duplicated') {
              const duplicateMessage = result.message || t('documentPanel.uploadDocuments.fileUploader.duplicateFile')
              uploadErrors[file.name] = duplicateMessage
              setFileErrors(prev => ({
                ...prev,
                [file.name]: duplicateMessage
              }))
            } else if (result.status !== 'success') {
              uploadErrors[file.name] = result.message
              setFileErrors(prev => ({
                ...prev,
                [file.name]: result.message
              }))
            } else {
              // Mark that we had at least one successful upload
              hasSuccessfulUpload = true
              successfulUploads.push({
                fileName: file.name,
                result
              })
              if (result.track_id?.startsWith('rebuild_multimodal')) {
                takeoverMessages.push(buildTakeoverSuccessMessage(file.name, result))
              }
            }
          } catch (err) {
            console.error(`Upload failed for ${file.name}:`, err)

            // Handle HTTP errors, including 400 errors
            let errorMsg = errorMessage(err)

            // If it's an axios error with response data, try to extract more detailed error info
            if (err && typeof err === 'object' && 'response' in err) {
              const axiosError = err as { response?: { status: number, data?: { detail?: string } } }
              if (axiosError.response?.status === 400) {
                // Extract specific error message from backend response
                errorMsg = axiosError.response.data?.detail || errorMsg
              }

              // Set progress to 100% to display error message
              setProgresses((pre) => ({
                ...pre,
                [file.name]: 100
              }))
            }

            // Record error message in both local tracking and state
            uploadErrors[file.name] = errorMsg
            setFileErrors(prev => ({
              ...prev,
              [file.name]: errorMsg
            }))
          }
        }

        // Check if any files failed to upload using our local tracking
        const hasErrors = Object.keys(uploadErrors).length > 0

        // Update toast status
        if (hasErrors) {
          toast.error(t('documentPanel.uploadDocuments.batch.error'), { id: toastId })
        } else if (takeoverMessages.length > 0) {
          toast.success(takeoverMessages.join('\n'), {
            id: toastId,
            duration: 8000
          })
        } else {
          toast.success(t('documentPanel.uploadDocuments.batch.success'), { id: toastId })
        }

        // Only update if at least one file was uploaded successfully
        if (hasSuccessfulUpload) {
          // Refresh document list
          if (onDocumentsUploaded) {
            Promise.resolve(onDocumentsUploaded({
              successfulUploads,
              takeoverRebuilds: successfulUploads.filter(upload =>
                upload.result.track_id?.startsWith('rebuild_multimodal')
              )
            })).catch(err => {
              console.error('Error refreshing documents:', err)
            })
          }
        }
      } catch (err) {
        console.error('Unexpected error during upload:', err)
        toast.error(t('documentPanel.uploadDocuments.generalError', { error: errorMessage(err) }), { id: toastId })
      } finally {
        setIsUploading(false)
      }
    },
    [buildTakeoverSuccessMessage, disabled, setIsUploading, setProgresses, setFileErrors, t, onDocumentsUploaded, targetKbId]
  )

  const trigger = (
    <span className="inline-flex" title={disabled ? disabledReason : undefined}>
      <Button
        variant="default"
        side="bottom"
        tooltip={disabled ? undefined : t('documentPanel.uploadDocuments.tooltip')}
        size="sm"
        disabled={disabled}
      >
        <UploadIcon /> {t('documentPanel.uploadDocuments.button')}
      </Button>
    </span>
  )

  return (
    <Dialog
      open={open}
      onOpenChange={(open) => {
        if (disabled) {
          return
        }
        if (isUploading) {
          return
        }
        if (!open) {
          setProgresses({})
          setFileErrors({})
        }
        setOpen(open)
      }}
    >
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="sm:max-w-xl" onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{t('documentPanel.uploadDocuments.title')}</DialogTitle>
          <DialogDescription>
            {t('documentPanel.uploadDocuments.description')}
          </DialogDescription>
        </DialogHeader>

        {/* Target KB selector — makes the destination explicit and
            overridable without the user having to close this dialog
            and switch tabs on the parent page. */}
        <div className="space-y-1.5 rounded-xl border border-border/60 bg-muted/10 px-3 py-2.5">
          <label htmlFor="upload-target-kb" className="text-xs font-medium text-muted-foreground">
            {t('documentPanel.uploadDocuments.targetKbLabel', {
              defaultValue: '上传到知识库',
            })}
          </label>
          {kbs.length > 0 ? (
            <Select
              value={targetKbId}
              onValueChange={setTargetKbId}
              disabled={isUploading}
            >
              <SelectTrigger id="upload-target-kb" className="h-9 rounded-lg">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {kbs.map((kb) => (
                  <SelectItem key={kb.id} value={kb.id}>
                    <div className="flex items-center gap-2">
                      <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
                      <span className="truncate">{kb.name || kb.id}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            // Before the KB list loads we still show the resolved id as
            // a disabled read-only pill so the user always sees *some*
            // indication of where the upload is going.
            <div className="flex items-center gap-2 rounded-lg border border-border/60 bg-background/60 px-3 py-1.5 text-sm text-muted-foreground">
              <BookOpenTextIcon className="size-3.5" aria-hidden="true" />
              <span className="truncate font-medium text-foreground">{targetKbId}</span>
            </div>
          )}
        </div>

        <FileUploader
          maxFileCount={Infinity}
          maxSize={defaultMaxUploadSize}
          description={supportedFileTypesDescription}
          onUpload={handleDocumentsUpload}
          onReject={handleRejectedFiles}
          progresses={progresses}
          fileErrors={fileErrors}
          disabled={isUploading}
        />
      </DialogContent>
    </Dialog>
  )
}
