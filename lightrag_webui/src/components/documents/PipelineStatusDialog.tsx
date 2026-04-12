import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AlignCenter, AlignLeft, AlignRight } from 'lucide-react'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from '@/components/ui/Dialog'
import Button from '@/components/ui/Button'
import Progress from '@/components/ui/Progress'
import { getPipelineStatus, cancelPipeline, PipelineStatusResponse } from '@/api/lightrag'
import { errorMessage } from '@/lib/utils'
import { cn } from '@/lib/utils'

type DialogPosition = 'left' | 'center' | 'right'

interface PipelineStatusDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

const STATUS_POLL_INTERVAL_MS = 2000
const CLOCK_TICK_INTERVAL_MS = 1000

function formatDuration(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) {
    return '—'
  }

  const totalSeconds = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (hours > 0) {
    return `${hours}h ${minutes}m`
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`
  }
  return `${seconds}s`
}

function getProgressMetrics(status: PipelineStatusResponse | null, nowMs: number) {
  const totalBatches = Math.max(status?.batchs ?? 0, 0)
  const currentBatch = Math.min(Math.max(status?.cur_batch ?? 0, 0), totalBatches || Number.MAX_SAFE_INTEGER)
  const processedBatches = totalBatches > 0 ? currentBatch : 0
  const remainingBatches = totalBatches > 0 ? Math.max(totalBatches - processedBatches, 0) : 0
  const progressPercent = totalBatches > 0
    ? Math.min(100, Math.max(0, (processedBatches / totalBatches) * 100))
    : 0

  const jobStartMs = status?.job_start ? new Date(status.job_start).getTime() : Number.NaN
  const elapsedMs = Number.isFinite(jobStartMs)
    ? Math.max(0, nowMs - jobStartMs)
    : null

  const etaMs = elapsedMs !== null && progressPercent > 0 && progressPercent < 100
    ? elapsedMs * ((100 - progressPercent) / progressPercent)
    : progressPercent >= 100
      ? 0
      : null

  return {
    totalBatches,
    processedBatches,
    remainingBatches,
    progressPercent,
    elapsedMs,
    etaMs
  }
}

export default function PipelineStatusDialog({
  open,
  onOpenChange
}: PipelineStatusDialogProps) {
  const { t } = useTranslation()
  const [status, setStatus] = useState<PipelineStatusResponse | null>(null)
  const [position, setPosition] = useState<DialogPosition>('center')
  const [isUserScrolled, setIsUserScrolled] = useState(false)
  const [showCancelConfirm, setShowCancelConfirm] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())
  const historyRef = useRef<HTMLDivElement>(null)

  // Reset UI state whenever the controlling open prop changes.
  useEffect(() => {
    if (open) {
      // Resetting local dialog UI when the controlling prop changes is intentional.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPosition('center')
      setIsUserScrolled(false)
      return
    }

    setShowCancelConfirm(false)
  }, [open])

  useEffect(() => {
    if (!open) return

    setNowMs(Date.now())
    const interval = setInterval(() => setNowMs(Date.now()), CLOCK_TICK_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [open])

  // Handle scroll position
  useEffect(() => {
    const container = historyRef.current
    if (!container || isUserScrolled) return

    container.scrollTop = container.scrollHeight
  }, [status?.history_messages, isUserScrolled])

  const handleScroll = () => {
    const container = historyRef.current
    if (!container) return

    const isAtBottom = Math.abs(
      (container.scrollHeight - container.scrollTop) - container.clientHeight
    ) < 1

    if (isAtBottom) {
      setIsUserScrolled(false)
    } else {
      setIsUserScrolled(true)
    }
  }

  // Refresh status every 2 seconds
  useEffect(() => {
    if (!open) return

    const fetchStatus = async () => {
      try {
        const data = await getPipelineStatus()
        setStatus(data)
      } catch (err) {
        toast.error(t('documentPanel.pipelineStatus.errors.fetchFailed', { error: errorMessage(err) }))
      }
    }

    fetchStatus()
    const interval = setInterval(fetchStatus, STATUS_POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [open, t])

  // Handle cancel pipeline confirmation
  const handleConfirmCancel = async () => {
    setShowCancelConfirm(false)
    try {
      const result = await cancelPipeline()
      if (result.status === 'cancellation_requested') {
        toast.success(t('documentPanel.pipelineStatus.cancelSuccess'))
      } else if (result.status === 'not_busy') {
        toast.info(t('documentPanel.pipelineStatus.cancelNotBusy'))
      }
    } catch (err) {
      toast.error(t('documentPanel.pipelineStatus.cancelFailed', { error: errorMessage(err) }))
    }
  }

  // Determine if cancel button should be enabled
  const canCancel = status?.busy === true && !status?.cancellation_requested

  const metrics = useMemo(() => getProgressMetrics(status, nowMs), [status, nowMs])

  const currentStep = status?.latest_message?.trim()
    || (status?.busy
      ? t('documentPanel.pipelineStatus.currentStepStarting', 'Starting…')
      : t('documentPanel.pipelineStatus.noActiveJob', 'No active job'))

  const progressSummary = status
    ? `${Math.round(metrics.progressPercent)}% · ${metrics.processedBatches}/${metrics.totalBatches || 0} ${t('documentPanel.pipelineStatus.unit')}`
    : '—'

  const accessibleDescription = status?.job_name
    ? `${t('documentPanel.pipelineStatus.jobName')}: ${status.job_name}, ${t('documentPanel.pipelineStatus.progress')}: ${progressSummary}`
    : t('documentPanel.pipelineStatus.noActiveJob', 'No active job')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          'sm:max-w-[860px] transition-all duration-200 fixed',
          position === 'left' && '!left-[25%] !translate-x-[-50%] !mx-4',
          position === 'center' && '!left-1/2 !-translate-x-1/2',
          position === 'right' && '!left-[75%] !translate-x-[-50%] !mx-4'
        )}
      >
        <DialogDescription className="sr-only">
          {accessibleDescription}
        </DialogDescription>
        <DialogHeader className="flex flex-row items-center">
          <DialogTitle className="flex-1">
            {t('documentPanel.pipelineStatus.title')}
          </DialogTitle>

          {/* Position control buttons */}
          <div className="flex items-center gap-2 mr-8">
            <Button
              variant="ghost"
              size="icon"
              className={cn(
                'h-6 w-6',
                position === 'left' && 'bg-zinc-200 text-zinc-800 hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-600'
              )}
              onClick={() => setPosition('left')}
            >
              <AlignLeft className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className={cn(
                'h-6 w-6',
                position === 'center' && 'bg-zinc-200 text-zinc-800 hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-600'
              )}
              onClick={() => setPosition('center')}
            >
              <AlignCenter className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className={cn(
                'h-6 w-6',
                position === 'right' && 'bg-zinc-200 text-zinc-800 hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-600'
              )}
              onClick={() => setPosition('right')}
            >
              <AlignRight className="h-4 w-4" />
            </Button>
          </div>
        </DialogHeader>

        {/* Status Content */}
        <div className="space-y-4 pt-4">
          {/* Pipeline Status - with cancel button */}
          <div className="flex flex-wrap items-center justify-between gap-4">
            {/* Left side: Status indicators */}
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2">
                <div className="text-sm font-medium">{t('documentPanel.pipelineStatus.busy')}:</div>
                <div className={`h-2 w-2 rounded-full ${status?.busy ? 'bg-green-500' : 'bg-gray-300'}`} />
              </div>
              <div className="flex items-center gap-2">
                <div className="text-sm font-medium">{t('documentPanel.pipelineStatus.requestPending')}:</div>
                <div className={`h-2 w-2 rounded-full ${status?.request_pending ? 'bg-green-500' : 'bg-gray-300'}`} />
              </div>
              {/* Only show cancellation status when it's requested */}
              {status?.cancellation_requested && (
                <div className="flex items-center gap-2">
                  <div className="text-sm font-medium">{t('documentPanel.pipelineStatus.cancellationRequested')}:</div>
                  <div className="h-2 w-2 rounded-full bg-red-500" />
                </div>
              )}
            </div>

            {/* Right side: Cancel button - only show when pipeline is busy */}
            {status?.busy && (
              <Button
                variant="destructive"
                size="sm"
                disabled={!canCancel}
                onClick={() => setShowCancelConfirm(true)}
                title={
                  status?.cancellation_requested
                    ? t('documentPanel.pipelineStatus.cancelInProgress')
                    : t('documentPanel.pipelineStatus.cancelTooltip')
                }
              >
                {t('documentPanel.pipelineStatus.cancelButton')}
              </Button>
            )}
          </div>

          {/* Job / Progress Information */}
          <div className="rounded-md border p-4 space-y-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="space-y-1">
                <div className="text-sm font-medium">{t('documentPanel.pipelineStatus.jobName')}: {status?.job_name || '-'}</div>
                <div className="text-sm text-zinc-600 dark:text-zinc-400">
                  {t('documentPanel.pipelineStatus.startTime')}: {status?.job_start
                    ? new Date(status.job_start).toLocaleString(undefined, {
                      year: 'numeric',
                      month: 'numeric',
                      day: 'numeric',
                      hour: 'numeric',
                      minute: 'numeric',
                      second: 'numeric'
                    })
                    : '-'}
                </div>
              </div>
              <div className="text-left sm:text-right">
                <div className="text-2xl font-semibold leading-none">
                  {`${Math.round(metrics.progressPercent)}%`}
                </div>
                <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">
                  {progressSummary}
                </div>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-3 text-xs text-zinc-600 dark:text-zinc-400">
                <span>{t('documentPanel.pipelineStatus.progress')}</span>
                <span>{status ? `${metrics.processedBatches}/${metrics.totalBatches || 0} ${t('documentPanel.pipelineStatus.unit')}` : '—'}</span>
              </div>
              <Progress value={metrics.progressPercent} className="h-2" />
            </div>

            <div className="rounded-md border bg-zinc-50/70 p-3 dark:bg-zinc-900/50">
              <div className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                {t('documentPanel.pipelineStatus.currentStep', 'Current step')}
              </div>
              <div className="mt-1 text-sm break-words">
                {currentStep}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-md border p-3">
                <div className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                  {t('documentPanel.pipelineStatus.remaining', 'Remaining')}
                </div>
                <div className="mt-1 text-lg font-semibold">{metrics.remainingBatches}</div>
                <div className="text-xs text-zinc-600 dark:text-zinc-400">
                  {t('documentPanel.pipelineStatus.unit')}
                </div>
              </div>
              <div className="rounded-md border p-3">
                <div className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                  {t('documentPanel.pipelineStatus.elapsed', 'Elapsed')}
                </div>
                <div className="mt-1 text-lg font-semibold">{formatDuration(metrics.elapsedMs)}</div>
              </div>
              <div className="rounded-md border p-3">
                <div className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                  {t('documentPanel.pipelineStatus.eta', 'ETA')}
                </div>
                <div className="mt-1 text-lg font-semibold">
                  {metrics.progressPercent > 0
                    ? formatDuration(metrics.etaMs)
                    : t('documentPanel.pipelineStatus.etaUnavailable', 'Estimating…')}
                </div>
              </div>
              <div className="rounded-md border p-3">
                <div className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                  {t('documentPanel.pipelineStatus.totalDocuments', 'Total documents')}
                </div>
                <div className="mt-1 text-lg font-semibold">{status?.docs ?? 0}</div>
              </div>
            </div>
          </div>

          {/* History Messages */}
          <div className="space-y-2">
            <div className="text-sm font-medium">{t('documentPanel.pipelineStatus.pipelineMessages')}:</div>
            <div
              ref={historyRef}
              onScroll={handleScroll}
              className="font-mono text-xs rounded-md bg-zinc-800 text-zinc-100 p-3 overflow-y-auto overflow-x-hidden min-h-[7.5em] max-h-[40vh]"
            >
              {status?.history_messages?.length ? (
                status.history_messages.map((msg, idx) => (
                  <div key={idx} className="whitespace-pre-wrap break-all">{msg}</div>
                ))
              ) : '-'}
            </div>
          </div>
        </div>
      </DialogContent>

      {/* Cancel Confirmation Dialog */}
      <Dialog open={showCancelConfirm} onOpenChange={setShowCancelConfirm}>
        <DialogContent className="sm:max-w-[425px]">
          <DialogHeader>
            <DialogTitle>{t('documentPanel.pipelineStatus.cancelConfirmTitle')}</DialogTitle>
            <DialogDescription>
              {t('documentPanel.pipelineStatus.cancelConfirmDescription')}
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-3 mt-4">
            <Button
              variant="outline"
              onClick={() => setShowCancelConfirm(false)}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={handleConfirmCancel}
            >
              {t('documentPanel.pipelineStatus.cancelConfirmButton')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Dialog>
  )
}
