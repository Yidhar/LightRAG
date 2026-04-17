import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { useSettingsStore } from '@/stores/settings'
import Button from '@/components/ui/Button'
import Badge from '@/components/ui/Badge'
import { cn } from '@/lib/utils'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/Table'
import EmptyCard from '@/components/ui/EmptyCard'
import Checkbox from '@/components/ui/Checkbox'
import UploadDocumentsDialog from '@/components/documents/UploadDocumentsDialog'
import ClearDocumentsDialog from '@/components/documents/ClearDocumentsDialog'
import DeleteDocumentsDialog from '@/components/documents/DeleteDocumentsDialog'
import PaginationControls from '@/components/ui/PaginationControls'

import {
  scanNewDocuments,
  rebuildDocumentMultimodal,
  cancelPipeline,
  cancelDocument,
  DocActionResponse,
  getDocumentsPaginatedWithTimeout,
  DocsStatusesResponse,
  DocStatus,
  DocStatusResponse,
  DocumentsRequest,
  PaginationInfo
} from '@/api/lightrag'
import { errorMessage } from '@/lib/utils'
import { toast } from 'sonner'
import { useAuthStore, useBackendState } from '@/stores/state'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import { hasPermission, resolveEffectiveRole } from '@/lib/permissions'

import { RefreshCwIcon, ActivityIcon, ArrowUpIcon, ArrowDownIcon, RotateCcwIcon, CheckSquareIcon, XIcon, AlertTriangle, Info, ImageIcon, SparklesIcon, CircleStopIcon } from 'lucide-react'
import PipelineStatusDialog from '@/components/documents/PipelineStatusDialog'

type StatusFilter = DocStatus | 'all';

// Utility functions defined outside component for better performance and to avoid dependency issues
const getCountValue = (counts: Record<string, number>, ...keys: string[]): number => {
  for (const key of keys) {
    const value = counts[key]
    if (typeof value === 'number') {
      return value
    }
  }
  return 0
}

const hasActiveDocumentsStatus = (counts: Record<string, number>): boolean =>
  getCountValue(counts, 'PROCESSING', 'processing') > 0 ||
  getCountValue(counts, 'PENDING', 'pending') > 0 ||
  getCountValue(counts, 'PREPROCESSED', 'preprocessed') > 0

const getDisplayFileName = (doc: DocStatusResponse, maxLength: number = 20): string => {
  // Check if file_path exists and is a non-empty string
  if (!doc.file_path || typeof doc.file_path !== 'string' || doc.file_path.trim() === '') {
    return doc.id;
  }

  // Try to extract filename from path
  const parts = doc.file_path.split('/');
  const fileName = parts[parts.length - 1];

  // Ensure extracted filename is valid
  if (!fileName || fileName.trim() === '') {
    return doc.id;
  }

  // If filename is longer than maxLength, truncate it and add ellipsis
  return fileName.length > maxLength
    ? fileName.slice(0, maxLength) + '...'
    : fileName;
};

const isPdfRebuildCandidate = (doc: DocStatusResponse): boolean => {
  const filePath = (doc.file_path || '').toLowerCase()
  if (!filePath.endsWith('.pdf')) {
    return false
  }
  return doc.status !== 'processing' && doc.status !== 'pending'
}

const getRawFileName = (doc: Pick<DocStatusResponse, 'file_path' | 'id'>): string => {
  if (!doc.file_path || typeof doc.file_path !== 'string' || doc.file_path.trim() === '') {
    return doc.id
  }

  const parts = doc.file_path.split('/')
  const fileName = parts[parts.length - 1]
  return fileName || doc.id
}

const parseDocStatusValue = (value: unknown): DocStatus | undefined => {
  if (typeof value !== 'string') {
    return undefined
  }

  switch (value) {
    case 'pending':
    case 'processing':
    case 'preprocessed':
    case 'processed':
    case 'failed':
      return value
    default:
      return undefined
  }
}

const mergeUniqueDocsById = (
  prioritizedDocs: DocStatusResponse[],
  existingDocs: DocStatusResponse[]
): DocStatusResponse[] => {
  const merged: DocStatusResponse[] = []
  const seenIds = new Set<string>()

  for (const doc of [...prioritizedDocs, ...existingDocs]) {
    if (!doc?.id || seenIds.has(doc.id)) {
      continue
    }
    seenIds.add(doc.id)
    merged.push(doc)
  }

  return merged
}

const formatMetadata = (metadata: Record<string, any>): string => {
  const formattedMetadata = { ...metadata };

  if (formattedMetadata.processing_start_time && typeof formattedMetadata.processing_start_time === 'number') {
    const date = new Date(formattedMetadata.processing_start_time * 1000);
    if (!isNaN(date.getTime())) {
      formattedMetadata.processing_start_time = date.toLocaleString();
    }
  }

  if (formattedMetadata.processing_end_time && typeof formattedMetadata.processing_end_time === 'number') {
    const date = new Date(formattedMetadata.processing_end_time * 1000);
    if (!isNaN(date.getTime())) {
      formattedMetadata.processing_end_time = date.toLocaleString();
    }
  }

  // Format JSON and remove outer braces and indentation
  const jsonStr = JSON.stringify(formattedMetadata, null, 2);
  const lines = jsonStr.split('\n');
  // Remove first line ({) and last line (}), and remove leading indentation (2 spaces)
  return lines.slice(1, -1)
    .map(line => line.replace(/^ {2}/, ''))
    .join('\n');
};

const pulseStyle = `
/* Tooltip styles */
.tooltip-container {
  position: relative;
  overflow: visible !important;
}

.tooltip {
  position: fixed; /* Use fixed positioning to escape overflow constraints */
  z-index: 9999; /* Ensure tooltip appears above all other elements */
  max-width: 600px;
  white-space: normal;
  word-break: break-word;
  overflow-wrap: break-word;
  border-radius: 0.375rem;
  padding: 0.5rem 0.75rem;
  font-size: 0.75rem; /* 12px */
  background-color: rgba(0, 0, 0, 0.95);
  color: white;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  pointer-events: none; /* Prevent tooltip from interfering with mouse events */
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.15s, visibility 0.15s;
}

.tooltip.visible {
  opacity: 1;
  visibility: visible;
}

.dark .tooltip {
  background-color: rgba(255, 255, 255, 0.95);
  color: black;
}

.tooltip pre {
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: break-word;
}

/* Position tooltip helper class */
.tooltip-helper {
  position: absolute;
  visibility: hidden;
  pointer-events: none;
  top: 0;
  left: 0;
  width: 100%;
  height: 0;
}

@keyframes pulse {
  0% {
    background-color: rgb(255 0 0 / 0.1);
    border-color: rgb(255 0 0 / 0.2);
  }
  50% {
    background-color: rgb(255 0 0 / 0.2);
    border-color: rgb(255 0 0 / 0.4);
  }
  100% {
    background-color: rgb(255 0 0 / 0.1);
    border-color: rgb(255 0 0 / 0.2);
  }
}

.dark .pipeline-busy {
  animation: dark-pulse 2s infinite;
}

@keyframes dark-pulse {
  0% {
    background-color: rgb(255 0 0 / 0.2);
    border-color: rgb(255 0 0 / 0.4);
  }
  50% {
    background-color: rgb(255 0 0 / 0.3);
    border-color: rgb(255 0 0 / 0.6);
  }
  100% {
    background-color: rgb(255 0 0 / 0.2);
    border-color: rgb(255 0 0 / 0.4);
  }
}

.pipeline-busy {
  animation: pulse 2s infinite;
  border: 1px solid;
}
`;

// Type definitions for sort field and direction
type SortField = 'created_at' | 'updated_at' | 'id' | 'file_path';
type SortDirection = 'asc' | 'desc';
type QuerySnapshot = {
  statusFilter: StatusFilter
  page: number
  pageSize: number
  sortField: SortField
  sortDirection: SortDirection
}
type RefreshRequest =
  | {
    type: 'intelligent';
    query: QuerySnapshot;
    customTimeout?: number;
    requestVersion: number;
  }
  | {
    type: 'manual';
    query: QuerySnapshot;
    requestVersion: number;
  };

export default function DocumentManager() {
  // Track component mount status
  const isMountedRef = useRef(true);
  const { workspaceId, kbId } = useParams()

  // Set up mount/unmount status tracking
  useEffect(() => {
    isMountedRef.current = true;

    // Handle page reload/unload
    const handleBeforeUnload = () => {
      isMountedRef.current = false;
    };

    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      isMountedRef.current = false;
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, []);

  const [showPipelineStatus, setShowPipelineStatus] = useState(false)
  const { t, i18n } = useTranslation()
  const { role, memberships } = useAuthStore()
  const health = useBackendState.use.health()
  const pipelineBusy = useBackendState.use.pipelineBusy()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )
  const canUploadDocuments = hasPermission(effectiveRole, 'kb:upload_document')
  const canDeleteDocuments = hasPermission(effectiveRole, 'kb:delete_document')
  const canManageSettings = hasPermission(effectiveRole, 'kb:manage_settings')
  // Selection is always available inside the documents surface. Destructive
  // actions (rebuild multimodal, delete, clear) remain independently gated by
  // their own ``disabled={!can...}`` props, so allowing a read-only role to
  // tick rows does not bypass permissions.
  const canUseDocumentSelection = true

  // Legacy state for backward compatibility
  const [docs, setDocs] = useState<DocsStatusesResponse | null>(null)

  const currentTab = useSettingsStore.use.currentTab()
  const showFileName = useSettingsStore.use.showFileName()
  const setShowFileName = useSettingsStore.use.setShowFileName()
  const documentsPageSize = useSettingsStore.use.documentsPageSize()
  const setDocumentsPageSize = useSettingsStore.use.setDocumentsPageSize()

  // New pagination state
  const [currentPageDocs, setCurrentPageDocs] = useState<DocStatusResponse[]>([])
  const [pagination, setPagination] = useState<PaginationInfo>({
    page: 1,
    page_size: documentsPageSize,
    total_count: 0,
    total_pages: 0,
    has_next: false,
    has_prev: false
  })
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({ all: 0 })
  const [isRefreshing, setIsRefreshing] = useState(false)

  // Sort state
  const [sortField, setSortField] = useState<SortField>('updated_at')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')

  // State for document status filter
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  // State to store page number for each status filter
  const [pageByStatus, setPageByStatus] = useState<Record<StatusFilter, number>>({
    all: 1,
    processed: 1,
    preprocessed: 1,
    processing: 1,
    pending: 1,
    failed: 1,
  });

  // State for document selection
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([])
  const activeSelectedDocIds = useMemo(
    () => (canUseDocumentSelection ? selectedDocIds : []),
    [canUseDocumentSelection, selectedDocIds]
  )
  const isSelectionMode = activeSelectedDocIds.length > 0

  // Add refs to track previous pipelineBusy state and current interval
  const prevPipelineBusyRef = useRef<boolean | undefined>(undefined);
  const pollingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRefreshPromiseRef = useRef<Promise<void> | null>(null);
  const pendingRefreshRequestRef = useRef<RefreshRequest | null>(null);
  const latestRefreshRequestVersionRef = useRef(0);

  // Add retry mechanism state
  const [retryState, setRetryState] = useState({
    count: 0,
    lastError: null as Error | null,
    isBackingOff: false
  });

  // Add circuit breaker state
  const [circuitBreakerState, setCircuitBreakerState] = useState({
    isOpen: false,
    failureCount: 0,
    lastFailureTime: null as number | null,
    nextRetryTime: null as number | null
  });

  const seedQueuedRebuildsIntoProcessing = useCallback((
    queuedDocs: Array<{
      doc: DocStatusResponse
      previousStatus?: DocStatus
    }>
  ) => {
    if (queuedDocs.length === 0) {
      return
    }

    const processingPage = 1
    const queuedDocResponses = queuedDocs.map(item => item.doc)

    setCurrentPageDocs(prev => (
      statusFilter === 'processing'
        ? mergeUniqueDocsById(queuedDocResponses, prev)
        : queuedDocResponses
    ))

    setStatusCounts(prev => {
      const next = { ...prev }

      if (typeof next.processing !== 'number') {
        next.processing = 0
      }
      if (typeof next.all !== 'number') {
        next.all = 0
      }

      for (const { previousStatus } of queuedDocs) {
        if (previousStatus && previousStatus !== 'processing') {
          const previousCount = next[previousStatus]
          if (typeof previousCount === 'number') {
            next[previousStatus] = Math.max(0, previousCount - 1)
          }
        }
        next.processing += 1
      }

      return next
    })

    setPageByStatus(prev => ({
      ...prev,
      [statusFilter]: pagination.page,
      processing: processingPage
    }))
    setStatusFilter('processing')
    setPagination(prev => ({ ...prev, page: processingPage }))
  }, [pagination.page, statusFilter])

  const createQueuedRebuildDoc = useCallback((params: {
    docId?: string
    fileName: string
    summary?: string
    trackId?: string
    previousStatus?: DocStatus
    stage?: string
  }): DocStatusResponse => {
    const nowIso = new Date().toISOString()
    const fileName = params.fileName || params.docId || nowIso

    return {
      id: params.docId || params.trackId || `queued-${fileName}-${Date.now()}`,
      content_summary: params.summary || t('documentPanel.documentManager.queuedRebuildSummary', { name: fileName }),
      content_length: 0,
      status: 'processing',
      created_at: nowIso,
      updated_at: nowIso,
      track_id: params.trackId,
      chunks_count: 0,
      metadata: {
        multimodal_rebuild_in_progress: true,
        multimodal_rebuild_stage: params.stage || 'queued_for_rebuild',
        processing_start_time: Math.floor(Date.now() / 1000),
        previous_status: params.previousStatus,
        optimistic: true
      },
      file_path: fileName
    }
  }, [t])

  // Handle checkbox change for individual documents
  const handleDocumentSelect = useCallback((docId: string, checked: boolean) => {
    if (!canUseDocumentSelection) {
      return
    }
    setSelectedDocIds(prev => {
      if (checked) {
        return [...prev, docId]
      } else {
        return prev.filter(id => id !== docId)
      }
    })
  }, [canUseDocumentSelection])

  // Handle deselect all documents
  const handleDeselectAll = useCallback(() => {
    if (!canUseDocumentSelection) {
      return
    }
    setSelectedDocIds([])
  }, [canUseDocumentSelection])

  // Handle sort column click
  const handleSort = (field: SortField) => {
    let actualField = field;

    // When clicking the first column, determine the actual sort field based on showFileName
    if (field === 'id') {
      actualField = showFileName ? 'file_path' : 'id';
    }

    const newDirection = (sortField === actualField && sortDirection === 'desc') ? 'asc' : 'desc';

    setSortField(actualField);
    setSortDirection(newDirection);

    // Reset page to 1 when sorting changes
    setPagination(prev => ({ ...prev, page: 1 }));

    // Reset all status filters' page memory since sorting affects all
    setPageByStatus({
      all: 1,
      processed: 1,
      preprocessed: 1,
      processing: 1,
      pending: 1,
      failed: 1,
    });
  };

  // Sort documents based on current sort field and direction
  const sortDocuments = useCallback((documents: DocStatusResponse[]) => {
    return [...documents].sort((a, b) => {
      let valueA, valueB;

      // Special handling for ID field based on showFileName setting
      if (sortField === 'id' && showFileName) {
        valueA = getDisplayFileName(a);
        valueB = getDisplayFileName(b);
      } else if (sortField === 'id') {
        valueA = a.id;
        valueB = b.id;
      } else {
        // Date fields
        valueA = new Date(a[sortField]).getTime();
        valueB = new Date(b[sortField]).getTime();
      }

      // Apply sort direction
      const sortMultiplier = sortDirection === 'asc' ? 1 : -1;

      // Compare values
      if (typeof valueA === 'string' && typeof valueB === 'string') {
        return sortMultiplier * valueA.localeCompare(valueB);
      } else {
        return sortMultiplier * (valueA > valueB ? 1 : valueA < valueB ? -1 : 0);
      }
    });
  }, [sortField, sortDirection, showFileName]);

  // Define a new type that includes status information
  type DocStatusWithStatus = DocStatusResponse & { status: DocStatus };

  const filteredAndSortedDocs = useMemo(() => {
    // Use currentPageDocs directly if available (from paginated API)
    // This preserves the backend's sort order and prevents status grouping
    if (currentPageDocs && currentPageDocs.length > 0) {
      return currentPageDocs.map(doc => ({
        ...doc,
        status: doc.status as DocStatus
      })) as DocStatusWithStatus[];
    }

    // Fallback to legacy docs structure for backward compatibility
    if (!docs) return null;

    // Create a flat array of documents with status information
    const allDocuments: DocStatusWithStatus[] = [];

    if (statusFilter === 'all') {
      // When filter is 'all', include documents from all statuses
      Object.entries(docs.statuses).forEach(([status, documents]) => {
        documents.forEach(doc => {
          allDocuments.push({
            ...doc,
            status: status as DocStatus
          });
        });
      });
    } else {
      // When filter is specific status, only include documents from that status
      const documents = docs.statuses[statusFilter] || [];
      documents.forEach(doc => {
        allDocuments.push({
          ...doc,
          status: statusFilter
        });
      });
    }

    // Sort all documents together if sort field and direction are specified
    if (sortField && sortDirection) {
      return sortDocuments(allDocuments);
    }

    return allDocuments;
  }, [currentPageDocs, docs, sortField, sortDirection, statusFilter, sortDocuments]);

  // Calculate current page selection state (after filteredAndSortedDocs is defined)
  const currentPageDocIds = useMemo(() => {
    return filteredAndSortedDocs?.map(doc => doc.id) || []
  }, [filteredAndSortedDocs])

  const selectedCurrentPageCount = useMemo(() => {
    return currentPageDocIds.filter(id => activeSelectedDocIds.includes(id)).length
  }, [activeSelectedDocIds, currentPageDocIds])

  const isCurrentPageFullySelected = useMemo(() => {
    return currentPageDocIds.length > 0 && selectedCurrentPageCount === currentPageDocIds.length
  }, [currentPageDocIds, selectedCurrentPageCount])

  const hasCurrentPageSelection = useMemo(() => {
    return selectedCurrentPageCount > 0
  }, [selectedCurrentPageCount])

  const selectedDocs = useMemo(() => {
    if (!filteredAndSortedDocs) {
      return [] as DocStatusResponse[]
    }
    return filteredAndSortedDocs.filter(doc => activeSelectedDocIds.includes(doc.id))
  }, [activeSelectedDocIds, filteredAndSortedDocs])

  const rebuildableSelectedDocs = useMemo(() => {
    return selectedDocs.filter(isPdfRebuildCandidate)
  }, [selectedDocs])

  const canRebuildSelectedDoc = useMemo(() => {
    return (
      canUploadDocuments &&
      selectedDocs.length === 1 &&
      rebuildableSelectedDocs.length === 1 &&
      !pipelineBusy &&
      !isRefreshing
    )
  }, [canUploadDocuments, selectedDocs.length, rebuildableSelectedDocs.length, pipelineBusy, isRefreshing])

  const rebuildMultimodalTooltip = useMemo(() => {
    if (!canUploadDocuments) {
      return t('documentPanel.documentManager.accessMessages.uploadDisabled')
    }
    if (pipelineBusy) {
      return t('documentPanel.documentManager.rebuildMultimodalBusy')
    }
    if (selectedDocs.length === 0) {
      return t('documentPanel.documentManager.rebuildMultimodalTooltip')
    }
    if (selectedDocs.length !== 1) {
      return t('documentPanel.documentManager.rebuildMultimodalSingleOnly')
    }
    if (rebuildableSelectedDocs.length !== 1) {
      return t('documentPanel.documentManager.rebuildMultimodalPdfOnly')
    }
    return t('documentPanel.documentManager.rebuildMultimodalTooltip')
  }, [canUploadDocuments, pipelineBusy, selectedDocs.length, rebuildableSelectedDocs.length, t])

  // Handle select current page
  const handleSelectCurrentPage = useCallback(() => {
    if (!canUseDocumentSelection) {
      return
    }
    setSelectedDocIds(currentPageDocIds)
  }, [canUseDocumentSelection, currentPageDocIds])


  // Get selection button properties
  const getSelectionButtonProps = useCallback(() => {
    if (!hasCurrentPageSelection) {
      return {
        text: t('documentPanel.selectDocuments.selectCurrentPage', { count: currentPageDocIds.length }),
        action: handleSelectCurrentPage,
        icon: CheckSquareIcon
      }
    } else if (isCurrentPageFullySelected) {
      return {
        text: t('documentPanel.selectDocuments.deselectAll', { count: currentPageDocIds.length }),
        action: handleDeselectAll,
        icon: XIcon
      }
    } else {
      return {
        text: t('documentPanel.selectDocuments.selectCurrentPage', { count: currentPageDocIds.length }),
        action: handleSelectCurrentPage,
        icon: CheckSquareIcon
      }
    }
  }, [hasCurrentPageSelection, isCurrentPageFullySelected, currentPageDocIds.length, handleSelectCurrentPage, handleDeselectAll, t])

  // Calculate document counts for each status
  const documentCounts = useMemo(() => {
    if (!docs) return { all: 0 } as Record<string, number>;

    const counts: Record<string, number> = { all: 0 };

    Object.entries(docs.statuses).forEach(([status, documents]) => {
      counts[status as DocStatus] = documents.length;
      counts.all += documents.length;
    });

    return counts;
  }, [docs]);

  const processedCount = getCountValue(statusCounts, 'PROCESSED', 'processed') || documentCounts.processed || 0;
  const preprocessedCount =
    getCountValue(statusCounts, 'PREPROCESSED', 'preprocessed') ||
    documentCounts.preprocessed ||
    0;
  const processingCount = getCountValue(statusCounts, 'PROCESSING', 'processing') || documentCounts.processing || 0;
  const pendingCount = getCountValue(statusCounts, 'PENDING', 'pending') || documentCounts.pending || 0;
  const failedCount = getCountValue(statusCounts, 'FAILED', 'failed') || documentCounts.failed || 0;
  const totalDocumentsCount = statusCounts.all || documentCounts.all
  const inFlightCount = processingCount + pendingCount + preprocessedCount

  const statusFilterItems: Array<{
    key: StatusFilter
    label: string
    count: number
    accentClass: string
    activeClass: string
  }> = [
    {
      key: 'all',
      label: t('documentPanel.documentManager.status.all'),
      count: totalDocumentsCount,
      accentClass: 'text-foreground',
      activeClass: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    },
    {
      key: 'processed',
      label: t('documentPanel.documentManager.status.completed'),
      count: processedCount,
      accentClass: 'text-green-600 dark:text-green-400',
      activeClass: 'border-green-500/30 bg-green-500/10 text-green-700 dark:text-green-300',
    },
    {
      key: 'preprocessed',
      label: t('documentPanel.documentManager.status.preprocessed'),
      count: preprocessedCount,
      accentClass: 'text-purple-600 dark:text-purple-400',
      activeClass: 'border-purple-500/30 bg-purple-500/10 text-purple-700 dark:text-purple-300',
    },
    {
      key: 'processing',
      label: t('documentPanel.documentManager.status.processing'),
      count: processingCount,
      accentClass: 'text-blue-600 dark:text-blue-400',
      activeClass: 'border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300',
    },
    {
      key: 'pending',
      label: t('documentPanel.documentManager.status.pending'),
      count: pendingCount,
      accentClass: 'text-yellow-600 dark:text-yellow-400',
      activeClass: 'border-yellow-500/30 bg-yellow-500/10 text-yellow-700 dark:text-yellow-300',
    },
    {
      key: 'failed',
      label: t('documentPanel.documentManager.status.failed'),
      count: failedCount,
      accentClass: 'text-red-600 dark:text-red-400',
      activeClass: 'border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300',
    },
  ]

  // Store previous status counts
  const prevStatusCounts = useRef({
    processed: 0,
    preprocessed: 0,
    processing: 0,
    pending: 0,
    failed: 0
  })

  // Add pulse style to document
  useEffect(() => {
    const style = document.createElement('style')
    style.textContent = pulseStyle
    document.head.appendChild(style)
    return () => {
      document.head.removeChild(style)
    }
  }, [])

  // Reference to the card content element
  const cardContentRef = useRef<HTMLDivElement>(null);

  // Add tooltip position adjustment for fixed positioning
  useEffect(() => {
    if (!docs) return;

    // Function to position tooltips
    const positionTooltips = () => {
      // Get all tooltip containers
      const containers = document.querySelectorAll<HTMLElement>('.tooltip-container');

      containers.forEach(container => {
        const tooltip = container.querySelector<HTMLElement>('.tooltip');
        if (!tooltip) return;

        // Skip tooltips that aren't visible
        if (!tooltip.classList.contains('visible')) return;

        // Get container position
        const rect = container.getBoundingClientRect();

        // Position tooltip above the container
        tooltip.style.left = `${rect.left}px`;
        tooltip.style.top = `${rect.top - 5}px`;
        tooltip.style.transform = 'translateY(-100%)';
      });
    };

    // Set up event listeners
    const handleMouseOver = (e: MouseEvent) => {
      // Check if target or its parent is a tooltip container
      const target = e.target as HTMLElement;
      const container = target.closest('.tooltip-container');
      if (!container) return;

      // Find tooltip and make it visible
      const tooltip = container.querySelector<HTMLElement>('.tooltip');
      if (tooltip) {
        tooltip.classList.add('visible');
        // Position immediately without delay
        positionTooltips();
      }
    };

    const handleMouseOut = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      const container = target.closest('.tooltip-container');
      if (!container) return;

      const tooltip = container.querySelector<HTMLElement>('.tooltip');
      if (tooltip) {
        tooltip.classList.remove('visible');
      }
    };

    document.addEventListener('mouseover', handleMouseOver);
    document.addEventListener('mouseout', handleMouseOut);

    return () => {
      document.removeEventListener('mouseover', handleMouseOver);
      document.removeEventListener('mouseout', handleMouseOut);
    };
  }, [docs]);

  const buildQuerySnapshot = useCallback((
    overrides: Partial<QuerySnapshot> = {}
  ): QuerySnapshot => ({
    statusFilter: overrides.statusFilter ?? statusFilter,
    page: overrides.page ?? pagination.page,
    pageSize: overrides.pageSize ?? pagination.page_size,
    sortField: overrides.sortField ?? sortField,
    sortDirection: overrides.sortDirection ?? sortDirection
  }), [pagination.page, pagination.page_size, sortField, sortDirection, statusFilter])

  const buildDocumentsRequest = useCallback((
    query: QuerySnapshot,
    page: number = query.page
  ): DocumentsRequest => ({
    status_filter: query.statusFilter === 'all' ? null : query.statusFilter,
    page,
    page_size: query.pageSize,
    sort_field: query.sortField,
    sort_direction: query.sortDirection
  }), [])

  // Utility function to update component state
  const updateComponentState = useCallback((response: any) => {
    setPagination(response.pagination);
    setCurrentPageDocs(response.documents);
    setStatusCounts(response.status_counts);

    // Update legacy docs state for backward compatibility
    const legacyDocs: DocsStatusesResponse = {
      statuses: {
        processed: response.documents.filter((doc: DocStatusResponse) => doc.status === 'processed'),
        preprocessed: response.documents.filter((doc: DocStatusResponse) => doc.status === 'preprocessed'),
        processing: response.documents.filter((doc: DocStatusResponse) => doc.status === 'processing'),
        pending: response.documents.filter((doc: DocStatusResponse) => doc.status === 'pending'),
        failed: response.documents.filter((doc: DocStatusResponse) => doc.status === 'failed')
      }
    };

    setDocs(response.pagination.total_count > 0 ? legacyDocs : null);
  }, []);


  // Enhanced error classification
  const classifyError = useCallback((error: any) => {
    if (error.name === 'AbortError') {
      return { type: 'cancelled', shouldRetry: false, shouldShowToast: false };
    }

    if (error.message === 'Request timeout') {
      return { type: 'timeout', shouldRetry: true, shouldShowToast: true };
    }

    if (error.message?.includes('Network Error') || error.code === 'NETWORK_ERROR') {
      return { type: 'network', shouldRetry: true, shouldShowToast: true };
    }

    if (error.status >= 500) {
      return { type: 'server', shouldRetry: true, shouldShowToast: true };
    }

    if (error.status >= 400 && error.status < 500) {
      return { type: 'client', shouldRetry: false, shouldShowToast: true };
    }

    return { type: 'unknown', shouldRetry: true, shouldShowToast: true };
  }, []);

  // Circuit breaker utility functions
  const isCircuitBreakerOpen = useCallback(() => {
    if (!circuitBreakerState.isOpen) return false;

    const now = Date.now();
    if (circuitBreakerState.nextRetryTime && now >= circuitBreakerState.nextRetryTime) {
      // Reset circuit breaker to half-open state
      setCircuitBreakerState(prev => ({
        ...prev,
        isOpen: false,
        failureCount: Math.max(0, prev.failureCount - 1)
      }));
      return false;
    }

    return true;
  }, [circuitBreakerState]);

  const recordFailure = useCallback((error: Error) => {
    const now = Date.now();
    setCircuitBreakerState(prev => {
      const newFailureCount = prev.failureCount + 1;
      const shouldOpen = newFailureCount >= 3; // Open after 3 failures

      return {
        isOpen: shouldOpen,
        failureCount: newFailureCount,
        lastFailureTime: now,
        nextRetryTime: shouldOpen ? now + (Math.pow(2, newFailureCount) * 1000) : null
      };
    });

    setRetryState(prev => ({
      count: prev.count + 1,
      lastError: error,
      isBackingOff: true
    }));
  }, []);

  const recordSuccess = useCallback(() => {
    setCircuitBreakerState({
      isOpen: false,
      failureCount: 0,
      lastFailureTime: null,
      nextRetryTime: null
    });

    setRetryState({
      count: 0,
      lastError: null,
      isBackingOff: false
    });
  }, []);

  // Handle page size change - update state and save to store
  const handlePageSizeChange = useCallback((newPageSize: number) => {
    if (newPageSize === pagination.page_size) return;

    // Save the new page size to the store
    setDocumentsPageSize(newPageSize);

    // Reset all status filters to page 1 when page size changes
    setPageByStatus({
      all: 1,
      processed: 1,
      preprocessed: 1,
      processing: 1,
      pending: 1,
      failed: 1,
    });

    setPagination(prev => ({ ...prev, page: 1, page_size: newPageSize }));
  }, [pagination.page_size, setDocumentsPageSize]);

  const runRefreshRequest = useCallback(async (refreshRequest: RefreshRequest) => {
    try {
      if (!isMountedRef.current) return;

      setIsRefreshing(true);

      const { query, requestVersion } = refreshRequest
      const isStaleRequest = () => requestVersion !== latestRefreshRequestVersionRef.current

      if (refreshRequest.type === 'manual') {
        const request = buildDocumentsRequest(query, 1)
        const response = await getDocumentsPaginatedWithTimeout(request)

        if (!isMountedRef.current || isStaleRequest()) return;

        if (response.pagination.total_count < query.pageSize && query.pageSize !== 10) {
          handlePageSizeChange(10);
        } else {
          setPagination(response.pagination);
          setCurrentPageDocs(response.documents);
          setStatusCounts(response.status_counts);

          const legacyDocs: DocsStatusesResponse = {
            statuses: {
              processed: response.documents.filter(doc => doc.status === 'processed'),
              preprocessed: response.documents.filter(doc => doc.status === 'preprocessed'),
              processing: response.documents.filter(doc => doc.status === 'processing'),
              pending: response.documents.filter(doc => doc.status === 'pending'),
              failed: response.documents.filter(doc => doc.status === 'failed')
            }
          };

          if (response.pagination.total_count > 0) {
            setDocs(legacyDocs);
          } else {
            setDocs(null);
          }
        }
      } else {
        const { customTimeout } = refreshRequest;
        const pageToFetch = query.page;
        const request = buildDocumentsRequest(query, pageToFetch)
        const response = await getDocumentsPaginatedWithTimeout(request, customTimeout)

        if (!isMountedRef.current || isStaleRequest()) return;

        // Boundary case handling: if target page has no data but total count > 0
        if (response.documents.length === 0 && response.pagination.total_count > 0) {
          const lastPage = Math.max(1, response.pagination.total_pages);

          if (pageToFetch !== lastPage) {
            const lastPageRequest = buildDocumentsRequest(query, lastPage)
            const lastPageResponse = await getDocumentsPaginatedWithTimeout(
              lastPageRequest,
              customTimeout
            )

            if (!isMountedRef.current || isStaleRequest()) return;

            setPageByStatus(prev => ({ ...prev, [query.statusFilter]: lastPage }));
            updateComponentState(lastPageResponse);
            return;
          }
        }

        setPageByStatus(prev => (
          prev[query.statusFilter] === pageToFetch
            ? prev
            : { ...prev, [query.statusFilter]: pageToFetch }
        ));
        updateComponentState(response);
      }

    } catch (err) {
      if (isMountedRef.current) {
        const errorClassification = classifyError(err);

        if (errorClassification.shouldShowToast) {
          toast.error(t('documentPanel.documentManager.errors.loadFailed', { error: errorMessage(err) }));
        }

        if (errorClassification.shouldRetry) {
          recordFailure(err as Error);
        }
      }
    } finally {
      if (isMountedRef.current) {
        setIsRefreshing(false);
      }
    }
  }, [
    t,
    updateComponentState,
    classifyError,
    recordFailure,
    handlePageSizeChange,
    buildDocumentsRequest
  ]);

  const enqueueRefresh = useCallback(async (refreshRequest: RefreshRequest) => {
    if (activeRefreshPromiseRef.current) {
      pendingRefreshRequestRef.current = refreshRequest;
      await activeRefreshPromiseRef.current;
      return;
    }

    const refreshLoopPromise = (async () => {
      let nextRequest: RefreshRequest | null = refreshRequest;

      while (nextRequest) {
        pendingRefreshRequestRef.current = null;
        await runRefreshRequest(nextRequest);
        nextRequest = pendingRefreshRequestRef.current;
      }
    })();

    activeRefreshPromiseRef.current = refreshLoopPromise;

    try {
      await refreshLoopPromise;
    } finally {
      if (activeRefreshPromiseRef.current === refreshLoopPromise) {
        activeRefreshPromiseRef.current = null;
      }
      pendingRefreshRequestRef.current = null;
    }
  }, [runRefreshRequest]);

  // Intelligent refresh function: handles all boundary cases
  const handleIntelligentRefresh = useCallback(async (
    targetPage?: number,
    resetToFirst?: boolean,
    customTimeout?: number
  ) => {
    const page = resetToFirst ? 1 : (targetPage || pagination.page)
    const query = buildQuerySnapshot({ page })
    const requestVersion = latestRefreshRequestVersionRef.current

    await enqueueRefresh({
      type: 'intelligent',
      query,
      customTimeout,
      requestVersion
    });
  }, [buildQuerySnapshot, enqueueRefresh, pagination.page]);

  // New paginated data fetching function
  const fetchPaginatedDocuments = useCallback(async (
    page: number,
    pageSize: number,
    currentStatusFilter: StatusFilter
  ) => {
    // Update pagination state
    setPagination(prev => ({ ...prev, page, page_size: pageSize }));

    // Use intelligent refresh
    await enqueueRefresh({
      type: 'intelligent',
      query: buildQuerySnapshot({
        page,
        pageSize,
        statusFilter: currentStatusFilter
      }),
      requestVersion: latestRefreshRequestVersionRef.current
    });
  }, [buildQuerySnapshot, enqueueRefresh]);

  // Legacy fetchDocuments function for backward compatibility
  const fetchDocuments = useCallback(async () => {
    await fetchPaginatedDocuments(pagination.page, pagination.page_size, statusFilter);
  }, [fetchPaginatedDocuments, pagination.page, pagination.page_size, statusFilter]);

  // Function to clear current polling interval
  const clearPollingInterval = useCallback(() => {
    if (pollingIntervalRef.current) {
      clearInterval(pollingIntervalRef.current);
      pollingIntervalRef.current = null;
    }
  }, []);

  // Function to start polling with given interval
  const startPollingInterval = useCallback((intervalMs: number) => {
    clearPollingInterval();

    pollingIntervalRef.current = setInterval(async () => {
      try {
        // Check circuit breaker before making request
        if (isCircuitBreakerOpen()) {
          return; // Skip this polling cycle
        }

        // Only perform fetch if component is still mounted
        if (isMountedRef.current) {
          await fetchDocuments();
          recordSuccess(); // Record successful operation
        }
      } catch (err) {
        // Only handle error if component is still mounted
        if (isMountedRef.current) {
          const errorClassification = classifyError(err);

          // Always reset isRefreshing state on error
          setIsRefreshing(false);

          if (errorClassification.shouldShowToast) {
            toast.error(t('documentPanel.documentManager.errors.scanProgressFailed', { error: errorMessage(err) }));
          }

          if (errorClassification.shouldRetry) {
            recordFailure(err as Error);

            // Implement exponential backoff for retries
            const backoffDelay = Math.min(Math.pow(2, retryState.count) * 1000, 30000); // Max 30s

            if (retryState.count < 3) { // Max 3 retries
              setTimeout(() => {
                if (isMountedRef.current) {
                  setRetryState(prev => ({ ...prev, isBackingOff: false }));
                }
              }, backoffDelay);
            }
          } else {
            // For non-retryable errors, stop polling
            clearPollingInterval();
          }
        }
      }
    }, intervalMs);
  }, [fetchDocuments, t, clearPollingInterval, isCircuitBreakerOpen, recordSuccess, recordFailure, classifyError, retryState.count]);

  const startFastProcessingPolling = useCallback(() => {
    startPollingInterval(2000)

    setTimeout(() => {
      if (isMountedRef.current && currentTab === 'documents' && health) {
        const hasActiveDocuments = hasActiveDocumentsStatus(statusCounts)
        const normalInterval = hasActiveDocuments ? 5000 : 30000
        startPollingInterval(normalInterval)
      }
    }, 15000)
  }, [currentTab, health, startPollingInterval, statusCounts])

  const handleCancelDocument = useCallback(
    async (docId: string) => {
      if (!canDeleteDocuments) return
      try {
        const result = await cancelDocument(docId)
        if (result.status === 'cancelled') {
          toast.success(
            t('documentPanel.documentManager.cancelSuccess', {
              defaultValue: '已取消文档 {{id}}',
              id: docId,
            })
          )
        } else if (result.status === 'cancel_requested') {
          toast.info(
            t('documentPanel.documentManager.cancelRequested', {
              defaultValue: '已请求取消，流水线将在下一个检查点终止该文档。',
            })
          )
        } else {
          toast.info(result.message)
        }
        // Refresh list shortly after so the user sees the status flip.
        setTimeout(() => {
          void handleIntelligentRefresh(undefined, false)
        }, 500)
      } catch (err: any) {
        const detail = err?.response?.data?.detail || errorMessage(err)
        toast.error(
          t('documentPanel.documentManager.cancelFailed', {
            defaultValue: '取消失败：{{error}}',
            error: detail,
          })
        )
      }
    },
    [canDeleteDocuments, t, handleIntelligentRefresh]
  )

  const handleStopPipeline = useCallback(async () => {
    if (!canManageSettings) {
      return
    }
    try {
      const result = await cancelPipeline()
      if (result.status === 'cancellation_requested') {
        toast.success(t('documentPanel.pipelineStatus.cancelSuccess'))
        // Reset health + refresh shortly after so the busy flag clears in the UI
        useBackendState.getState().resetHealthCheckTimerDelayed(1000)
      } else if (result.status === 'not_busy') {
        toast.info(t('documentPanel.pipelineStatus.cancelNotBusy'))
      }
    } catch (err) {
      toast.error(t('documentPanel.pipelineStatus.cancelFailed', { error: errorMessage(err) }))
    }
  }, [canManageSettings, t])

  const scanDocuments = useCallback(async () => {
    if (!canUploadDocuments) {
      toast.error(t('documentPanel.documentManager.errors.scanDisabled'))
      return
    }
    try {
      // Check if component is still mounted before starting the request
      if (!isMountedRef.current) return;

      const { status, message, track_id: _track_id } = await scanNewDocuments(); // eslint-disable-line @typescript-eslint/no-unused-vars

      // Check again if component is still mounted after the request completes
      if (!isMountedRef.current) return;

      // Note: _track_id is available for future use (e.g., progress tracking)
      toast.message(message || status);

      // Reset health check timer with 1 second delay to avoid race condition
      useBackendState.getState().resetHealthCheckTimerDelayed(1000);

      // Perform immediate refresh with 90s timeout after scan (tolerates PostgreSQL switchover)
      await handleIntelligentRefresh(undefined, false, 90000);

      // Start fast refresh with 2-second interval after initial refresh
      startFastProcessingPolling()
    } catch (err) {
      // Only show error if component is still mounted
      if (isMountedRef.current) {
        toast.error(t('documentPanel.documentManager.errors.scanFailed', { error: errorMessage(err) }));
      }
    }
  }, [canUploadDocuments, t, handleIntelligentRefresh, startFastProcessingPolling])

  const handleUploadedDocuments = useCallback(async (payload: {
    successfulUploads: Array<{
      fileName: string
      result: DocActionResponse
    }>
    takeoverRebuilds: Array<{
      fileName: string
      result: DocActionResponse
    }>
  }) => {
    const takeoverRebuilds = payload.takeoverRebuilds || []

    useBackendState.getState().resetHealthCheckTimerDelayed(1000)

    if (takeoverRebuilds.length > 0) {
      const queuedDocs = takeoverRebuilds.map(upload => {
        const previousStatus = parseDocStatusValue(
          upload.result.operation_metadata?.previous_status
        )

        return {
          doc: createQueuedRebuildDoc({
            docId: upload.result.doc_id,
            fileName: upload.fileName,
            trackId: upload.result.track_id,
            previousStatus,
            stage: typeof upload.result.operation_metadata?.multimodal_rebuild_stage === 'string'
              ? upload.result.operation_metadata.multimodal_rebuild_stage
              : undefined
          }),
          previousStatus
        }
      })

      seedQueuedRebuildsIntoProcessing(queuedDocs)
      startFastProcessingPolling()

      if (statusFilter === 'processing' && pagination.page === 1) {
        await handleIntelligentRefresh(1, false, 120000)
      }
      return
    }

    await handleIntelligentRefresh(undefined, false, 120000)
  }, [
    createQueuedRebuildDoc,
    handleIntelligentRefresh,
    pagination.page,
    seedQueuedRebuildsIntoProcessing,
    startFastProcessingPolling,
    statusFilter
  ])

  const handleRebuildMultimodal = useCallback(async () => {
    if (!canRebuildSelectedDoc || rebuildableSelectedDocs.length !== 1) {
      return
    }

    const targetDoc = rebuildableSelectedDocs[0]
    const processingPage = 1

    try {
      const { track_id: rebuildTrackId } = await rebuildDocumentMultimodal(targetDoc.id, {
        reuse_cache: true
      })

      if (!isMountedRef.current) return

      const targetFileName = getRawFileName(targetDoc)
      const previousStatus = parseDocStatusValue(targetDoc.status)

      seedQueuedRebuildsIntoProcessing([
        {
          doc: createQueuedRebuildDoc({
            docId: targetDoc.id,
            fileName: targetFileName,
            summary: targetDoc.content_summary,
            trackId: rebuildTrackId,
            previousStatus
          }),
          previousStatus
        }
      ])

      toast.success(t('documentPanel.documentManager.rebuildMultimodalStartedWithName', {
        name: targetFileName
      }), {
        duration: 8000
      })
      setSelectedDocIds([])

      useBackendState.getState().resetHealthCheckTimerDelayed(1000)

      startFastProcessingPolling()

      if (statusFilter === 'processing' && pagination.page === processingPage) {
        await handleIntelligentRefresh(processingPage, false, 120000)
      }
    } catch (err) {
      if (isMountedRef.current) {
        toast.error(
          t('documentPanel.documentManager.errors.rebuildMultimodalFailed', {
            error: errorMessage(err)
          })
        )
      }
    }
  }, [
    canRebuildSelectedDoc,
    rebuildableSelectedDocs,
    t,
    createQueuedRebuildDoc,
    pagination.page,
    statusFilter,
    seedQueuedRebuildsIntoProcessing,
    startFastProcessingPolling,
    handleIntelligentRefresh
  ])

  // Handle manual refresh with pagination reset logic
  const handleManualRefresh = useCallback(async () => {
    await enqueueRefresh({
      type: 'manual',
      query: buildQuerySnapshot(),
      requestVersion: latestRefreshRequestVersionRef.current
    });
  }, [buildQuerySnapshot, enqueueRefresh]);

  useEffect(() => {
    latestRefreshRequestVersionRef.current += 1
  }, [pagination.page, pagination.page_size, statusFilter, sortField, sortDirection])

  // Monitor pipelineBusy changes and trigger immediate refresh with timer reset
  useEffect(() => {
    // Skip the first render when prevPipelineBusyRef is undefined
    if (prevPipelineBusyRef.current !== undefined && prevPipelineBusyRef.current !== pipelineBusy) {
      // pipelineBusy state has changed, trigger immediate refresh
      if (currentTab === 'documents' && health && isMountedRef.current) {
        // Use intelligent refresh to preserve current page
        handleIntelligentRefresh();

        // Reset polling timer after intelligent refresh
        const hasActiveDocuments = hasActiveDocumentsStatus(statusCounts);
        const pollingInterval = hasActiveDocuments ? 5000 : 30000;
        startPollingInterval(pollingInterval);
      }
    }
    // Update the previous state
    prevPipelineBusyRef.current = pipelineBusy;
  }, [
    pipelineBusy,
    currentTab,
    health,
    handleIntelligentRefresh,
    statusCounts,
    startPollingInterval
  ]);

  // Set up intelligent polling with dynamic interval based on document status
  useEffect(() => {
    if (currentTab !== 'documents' || !health) {
      clearPollingInterval();
      return
    }

    // Determine polling interval based on document status
    const hasActiveDocuments = hasActiveDocumentsStatus(statusCounts);
    const pollingInterval = hasActiveDocuments ? 5000 : 30000; // 5s if active, 30s if idle

    startPollingInterval(pollingInterval);

    return () => {
      clearPollingInterval();
    }
  }, [health, t, currentTab, statusCounts, startPollingInterval, clearPollingInterval])

  // Monitor docs changes to check status counts and trigger health check if needed
  useEffect(() => {
    if (!docs) return;

    // Get new status counts
    const newStatusCounts = {
      processed: docs?.statuses?.processed?.length || 0,
      preprocessed: docs?.statuses?.preprocessed?.length || 0,
      processing: docs?.statuses?.processing?.length || 0,
      pending: docs?.statuses?.pending?.length || 0,
      failed: docs?.statuses?.failed?.length || 0
    }

    // Check if any status count has changed
    const hasStatusCountChange = (Object.keys(newStatusCounts) as Array<keyof typeof newStatusCounts>).some(
      status => newStatusCounts[status] !== prevStatusCounts.current[status]
    )

    // Trigger health check if changes detected and component is still mounted
    if (hasStatusCountChange && isMountedRef.current) {
      useBackendState.getState().check()
    }

    // Update previous status counts
    prevStatusCounts.current = newStatusCounts
  }, [docs]);

  // Handle page change - only update state
  const handlePageChange = useCallback((newPage: number) => {
    if (newPage === pagination.page) return;

    // Save the new page for current status filter
    setPageByStatus(prev => ({ ...prev, [statusFilter]: newPage }));
    setPagination(prev => ({ ...prev, page: newPage }));
  }, [pagination.page, statusFilter]);

  // Handle status filter change - only update state
  const handleStatusFilterChange = useCallback((newStatusFilter: StatusFilter) => {
    if (newStatusFilter === statusFilter) return;

    // Save current page for the current status filter
    setPageByStatus(prev => ({ ...prev, [statusFilter]: pagination.page }));

    // Get the saved page for the new status filter
    const newPage = pageByStatus[newStatusFilter];

    // Update status filter and restore the saved page
    setStatusFilter(newStatusFilter);
    setPagination(prev => ({ ...prev, page: newPage }));
  }, [statusFilter, pagination.page, pageByStatus]);

  // Handle documents deleted callback
  const handleDocumentsDeleted = useCallback(async () => {
    setSelectedDocIds([])

    // Reset health check timer with 1 second delay to avoid race condition
    useBackendState.getState().resetHealthCheckTimerDelayed(1000)

    // Schedule a health check 2 seconds after successful clear
    startPollingInterval(2000)
  }, [startPollingInterval])

  // Handle documents cleared callback with proper interval reset
  const handleDocumentsCleared = useCallback(async () => {
    // Clear current polling interval
    clearPollingInterval();

    // Reset status counts to ensure proper state
    setStatusCounts({
      all: 0,
      processed: 0,
      processing: 0,
      pending: 0,
      failed: 0
    });

    // Perform one immediate refresh to confirm clear operation
    if (isMountedRef.current) {
      try {
        await fetchDocuments();
      } catch (err) {
        console.error('Error fetching documents after clear:', err);
      }
    }

    // Set appropriate polling interval based on current state
    // Since documents are cleared, use idle interval (30 seconds)
    if (currentTab === 'documents' && health && isMountedRef.current) {
      startPollingInterval(30000); // 30 seconds for idle state
    }
  }, [clearPollingInterval, setStatusCounts, fetchDocuments, currentTab, health, startPollingInterval])


  // Handle showFileName change - switch sort field if currently sorting by first column
  useEffect(() => {
    // Only switch if currently sorting by the first column (id or file_path)
    if (sortField === 'id' || sortField === 'file_path') {
      const newSortField = showFileName ? 'file_path' : 'id';
      if (sortField !== newSortField) {
        setSortField(newSortField);
      }
    }
  }, [showFileName, sortField]);

  // Reset selection state when page, status filter, or sort changes
  useEffect(() => {
    setSelectedDocIds([])
  }, [pagination.page, statusFilter, sortField, sortDirection]);

  // Central effect to handle all data fetching
  useEffect(() => {
    if (currentTab === 'documents') {
      fetchPaginatedDocuments(pagination.page, pagination.page_size, statusFilter);
    }
  }, [
    currentTab,
    pagination.page,
    pagination.page_size,
    statusFilter,
    sortField,
    sortDirection,
    fetchPaginatedDocuments
  ]);

  const selectionButtonProps =
    canUseDocumentSelection && (hasCurrentPageSelection || currentPageDocIds.length > 0)
      ? getSelectionButtonProps()
      : null

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-hidden p-4 sm:p-5">
      {/* Compact toolbar: stat pills + pipeline badge + action cluster */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => handleStatusFilterChange('all')}
          className={cn(
            'flex min-w-[5.25rem] flex-col items-start rounded-xl border border-border/70 px-3 py-1.5 text-left transition-colors hover:border-emerald-500/30 hover:bg-emerald-500/[0.04]',
            statusFilter === 'all' && 'border-emerald-500/40 bg-emerald-500/[0.08]'
          )}
        >
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {t('documentPanel.documentManager.status.all')}
          </span>
          <span className="text-lg font-semibold tabular-nums text-foreground">{totalDocumentsCount}</span>
        </button>

        <button
          type="button"
          onClick={() => handleStatusFilterChange('processed')}
          className={cn(
            'flex min-w-[5.25rem] flex-col items-start rounded-xl border border-border/70 px-3 py-1.5 text-left transition-colors hover:border-green-500/30 hover:bg-green-500/[0.04]',
            statusFilter === 'processed' && 'border-green-500/40 bg-green-500/[0.08]'
          )}
        >
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {t('documentPanel.documentManager.status.completed')}
          </span>
          <span className="text-lg font-semibold tabular-nums text-green-600 dark:text-green-400">
            {processedCount}
          </span>
        </button>

        <button
          type="button"
          onClick={() => handleStatusFilterChange('processing')}
          className={cn(
            'flex min-w-[5.25rem] flex-col items-start rounded-xl border border-border/70 px-3 py-1.5 text-left transition-colors hover:border-blue-500/30 hover:bg-blue-500/[0.04]',
            statusFilter === 'processing' && 'border-blue-500/40 bg-blue-500/[0.08]',
            inFlightCount > 0 && 'border-blue-500/30 bg-blue-500/[0.06]'
          )}
        >
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {t('documentPanel.documentManager.status.processing')}
          </span>
          <span className="flex items-center gap-1.5 text-lg font-semibold tabular-nums text-blue-600 dark:text-blue-400">
            {inFlightCount > 0 && (
              <span className="inline-block size-1.5 animate-pulse rounded-full bg-blue-500" />
            )}
            {inFlightCount}
          </span>
        </button>

        <button
          type="button"
          onClick={() => handleStatusFilterChange('failed')}
          className={cn(
            'flex min-w-[5.25rem] flex-col items-start rounded-xl border border-border/70 px-3 py-1.5 text-left transition-colors hover:border-red-500/30 hover:bg-red-500/[0.04]',
            statusFilter === 'failed' && 'border-red-500/40 bg-red-500/[0.08]'
          )}
        >
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {t('documentPanel.documentManager.status.failed')}
          </span>
          <span className="text-lg font-semibold tabular-nums text-red-600 dark:text-red-400">
            {failedCount}
          </span>
        </button>

        <Badge
          variant="outline"
          className={cn(
            'rounded-full px-2.5 py-0.5 text-[11px]',
            pipelineBusy && 'pipeline-busy border-red-500/30 text-red-600 dark:text-red-300'
          )}
        >
          {pipelineBusy
            ? t('documentPanel.pipelineStatus.busy')
            : t('documentPanel.pipelineStatus.noActiveJob')}
        </Badge>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            onClick={scanDocuments}
            disabled={!canUploadDocuments}
            side="bottom"
            tooltip={canUploadDocuments ? t('documentPanel.documentManager.scanTooltip') : undefined}
            size="sm"
            className="rounded-full"
          >
            <RefreshCwIcon className="h-4 w-4" />
            {t('documentPanel.documentManager.scanButton')}
          </Button>

          <Button
            variant="outline"
            onClick={() => setShowPipelineStatus(true)}
            side="bottom"
            tooltip={t('documentPanel.documentManager.pipelineStatusTooltip')}
            size="sm"
            className={cn('rounded-full', pipelineBusy && 'pipeline-busy')}
          >
            <ActivityIcon className="h-4 w-4" />
            {t('documentPanel.documentManager.pipelineStatusButton')}
          </Button>

          {pipelineBusy && canManageSettings && (
            <Button
              variant="outline"
              onClick={handleStopPipeline}
              side="bottom"
              tooltip={t('documentPanel.pipelineStatus.cancelTooltip', {
                defaultValue: 'Stop the active pipeline so processing documents can be deleted or resumed'
              })}
              size="sm"
              className="rounded-full border-red-500/40 text-red-600 hover:border-red-500/60 hover:bg-red-500/[0.06] dark:text-red-300"
            >
              <CircleStopIcon className="h-4 w-4" />
              {t('documentPanel.pipelineStatus.cancelButton', { defaultValue: '停止流水线' })}
            </Button>
          )}

          {isSelectionMode && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleRebuildMultimodal}
              disabled={!canRebuildSelectedDoc}
              side="bottom"
              tooltip={rebuildMultimodalTooltip}
              className="rounded-full"
            >
              <ImageIcon className="h-4 w-4" />
              {t('documentPanel.documentManager.rebuildMultimodalButton')}
            </Button>
          )}

          {selectionButtonProps && (
            <Button
              variant="outline"
              size="sm"
              onClick={selectionButtonProps.action}
              side="bottom"
              tooltip={selectionButtonProps.text}
              className="rounded-full"
            >
              {(() => {
                const SelectionIcon = selectionButtonProps.icon
                return <SelectionIcon className="h-4 w-4" />
              })()}
              {selectionButtonProps.text}
            </Button>
          )}

          {isSelectionMode ? (
            <DeleteDocumentsDialog
              disabled={!canDeleteDocuments}
              disabledReason={t('documentPanel.documentManager.accessMessages.deleteDisabled')}
              selectedDocIds={activeSelectedDocIds}
              onDocumentsDeleted={handleDocumentsDeleted}
            />
          ) : (
            <ClearDocumentsDialog
              disabled={!canDeleteDocuments}
              disabledReason={t('documentPanel.documentManager.accessMessages.deleteDisabled')}
              canClearCache={canManageSettings}
              onDocumentsCleared={handleDocumentsCleared}
            />
          )}

          <UploadDocumentsDialog
            disabled={!canUploadDocuments}
            disabledReason={t('documentPanel.documentManager.accessMessages.uploadDisabled')}
            onDocumentsUploaded={handleUploadedDocuments}
          />
          <PipelineStatusDialog
            canCancelPipeline={canManageSettings}
            open={showPipelineStatus}
            onOpenChange={setShowPipelineStatus}
          />
        </div>
      </div>

      {/* Uploaded documents list — single bordered container, takes remaining height */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border/60 bg-background/70">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 px-4 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <SparklesIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
            <p className="text-sm font-semibold text-foreground">
              {t('documentPanel.documentManager.uploadedTitle')}
            </p>
            <Button
              id="toggle-filename-btn"
              variant="outline"
              size="sm"
              onClick={() => setShowFileName(!showFileName)}
              className="rounded-full"
            >
              {showFileName
                ? t('documentPanel.documentManager.hideButton')
                : t('documentPanel.documentManager.showButton')}
              {' '}
              {t('documentPanel.documentManager.fileNameLabel')}
            </Button>
          </div>

          <div className="flex flex-wrap items-center gap-2" dir={i18n.dir()}>
            {statusFilterItems.map((item) => (
              <Button
                key={item.key}
                size="sm"
                variant="outline"
                onClick={() => handleStatusFilterChange(item.key)}
                className={cn(
                  'rounded-full border-border/70 bg-background/80',
                  item.accentClass,
                  statusFilter === item.key && item.activeClass
                )}
              >
                {item.label} ({item.count})
              </Button>
            ))}
            <Button
              variant="ghost"
              size="sm"
              onClick={handleManualRefresh}
              side="bottom"
              tooltip={t('documentPanel.documentManager.refreshTooltip')}
              className={cn(
                'rounded-full',
                isRefreshing && '[&_svg]:animate-spin'
              )}
            >
              <RotateCcwIcon className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {pagination.total_pages > 1 && (
          <div className="border-b border-border/60 px-4 py-2">
            <PaginationControls
              currentPage={pagination.page}
              totalPages={pagination.total_pages}
              pageSize={pagination.page_size}
              totalCount={pagination.total_count}
              onPageChange={handlePageChange}
              onPageSizeChange={handlePageSizeChange}
              isLoading={false}
              compact={true}
            />
          </div>
        )}

        <div className="relative min-h-0 flex-1 overflow-hidden" ref={cardContentRef}>
            {!docs && (
              <div className="absolute inset-0 p-0">
                <EmptyCard
                  title={t('documentPanel.documentManager.emptyTitle')}
                  description={t('documentPanel.documentManager.emptyDescription')}
                />
              </div>
            )}
            {docs && (
              <div className="absolute inset-0 flex flex-col p-0">
                <div className="absolute inset-[-1px] flex flex-col overflow-hidden rounded-[24px] border border-gray-200 p-0 dark:border-gray-700">
                  <Table className="w-full">
                    <TableHeader className="sticky top-0 z-10 bg-background shadow-sm">
                      <TableRow className="border-b bg-card/95 shadow-[inset_0_-1px_0_rgba(0,0,0,0.08)] backdrop-blur supports-[backdrop-filter]:bg-card/75">
                        <TableHead
                          onClick={() => handleSort('id')}
                          className="cursor-pointer select-none hover:bg-gray-200 dark:hover:bg-gray-800"
                        >
                          <div className="flex items-center">
                            {showFileName
                              ? t('documentPanel.documentManager.columns.fileName')
                              : t('documentPanel.documentManager.columns.id')}
                            {((sortField === 'id' && !showFileName) || (sortField === 'file_path' && showFileName)) && (
                              <span className="ml-1">
                                {sortDirection === 'asc' ? <ArrowUpIcon size={14} /> : <ArrowDownIcon size={14} />}
                              </span>
                            )}
                          </div>
                        </TableHead>
                        <TableHead>{t('documentPanel.documentManager.columns.summary')}</TableHead>
                        <TableHead>{t('documentPanel.documentManager.columns.status')}</TableHead>
                        <TableHead>{t('documentPanel.documentManager.columns.length')}</TableHead>
                        <TableHead>{t('documentPanel.documentManager.columns.chunks')}</TableHead>
                        <TableHead
                          onClick={() => handleSort('created_at')}
                          className="cursor-pointer select-none hover:bg-gray-200 dark:hover:bg-gray-800"
                        >
                          <div className="flex items-center">
                            {t('documentPanel.documentManager.columns.created')}
                            {sortField === 'created_at' && (
                              <span className="ml-1">
                                {sortDirection === 'asc' ? <ArrowUpIcon size={14} /> : <ArrowDownIcon size={14} />}
                              </span>
                            )}
                          </div>
                        </TableHead>
                        <TableHead
                          onClick={() => handleSort('updated_at')}
                          className="cursor-pointer select-none hover:bg-gray-200 dark:hover:bg-gray-800"
                        >
                          <div className="flex items-center">
                            {t('documentPanel.documentManager.columns.updated')}
                            {sortField === 'updated_at' && (
                              <span className="ml-1">
                                {sortDirection === 'asc' ? <ArrowUpIcon size={14} /> : <ArrowDownIcon size={14} />}
                              </span>
                            )}
                          </div>
                        </TableHead>
                        {canUseDocumentSelection && (
                          <TableHead className="w-16 text-center">
                            {t('documentPanel.documentManager.columns.select')}
                          </TableHead>
                        )}
                      </TableRow>
                    </TableHeader>
                    <TableBody className="overflow-auto text-sm">
                      {filteredAndSortedDocs && filteredAndSortedDocs.map((doc) => (
                        <TableRow key={doc.id}>
                          <TableCell className="max-w-[250px] truncate overflow-visible font-mono">
                            {showFileName ? (
                              <>
                                <div className="group tooltip-container relative overflow-visible">
                                  <div className="truncate">{getDisplayFileName(doc, 30)}</div>
                                  <div className="tooltip invisible group-hover:visible">{doc.file_path}</div>
                                </div>
                                <div className="text-xs text-gray-500">{doc.id}</div>
                              </>
                            ) : (
                              <div className="group tooltip-container relative overflow-visible">
                                <div className="truncate">{doc.id}</div>
                                <div className="tooltip invisible group-hover:visible">{doc.file_path}</div>
                              </div>
                            )}
                          </TableCell>
                          <TableCell className="max-w-xs min-w-45 truncate overflow-visible">
                            <div className="group tooltip-container relative overflow-visible">
                              <div className="truncate">{doc.content_summary}</div>
                              <div className="tooltip invisible group-hover:visible">{doc.content_summary}</div>
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="group tooltip-container relative flex items-center overflow-visible">
                              {doc.metadata?.multimodal_rebuild_in_progress && (
                                <span className="text-blue-600">
                                  {t('documentPanel.documentManager.status.rebuildingMultimodal')}
                                </span>
                              )}
                              {doc.status === 'processed' && (
                                <span className="text-green-600">{t('documentPanel.documentManager.status.completed')}</span>
                              )}
                              {doc.status === 'preprocessed' &&
                                !doc.metadata?.multimodal_rebuild_in_progress && (
                                  <span className="text-purple-600">
                                    {t('documentPanel.documentManager.status.preprocessed')}
                                  </span>
                                )}
                              {doc.status === 'processing' &&
                                !doc.metadata?.multimodal_rebuild_in_progress && (
                                  <span className="text-blue-600">
                                    {t('documentPanel.documentManager.status.processing')}
                                  </span>
                                )}
                              {doc.status === 'pending' &&
                                !doc.metadata?.multimodal_rebuild_in_progress && (
                                  <span className="text-yellow-600">
                                    {t('documentPanel.documentManager.status.pending')}
                                  </span>
                                )}
                              {doc.status === 'failed' && (
                                <span className="text-red-600">{t('documentPanel.documentManager.status.failed')}</span>
                              )}

                              {doc.error_msg ? (
                                <AlertTriangle className="ml-2 h-4 w-4 text-yellow-500" />
                              ) : doc.metadata && Object.keys(doc.metadata).length > 0 ? (
                                <Info className="ml-2 h-4 w-4 text-blue-500" />
                              ) : null}

                              {(doc.error_msg || (doc.metadata && Object.keys(doc.metadata).length > 0)) && (
                                <div className="tooltip invisible group-hover:visible">
                                  {doc.metadata && Object.keys(doc.metadata).length > 0 && (
                                    <pre>{formatMetadata(doc.metadata)}</pre>
                                  )}
                                  {doc.error_msg && <pre>{doc.error_msg}</pre>}
                                </div>
                              )}

                              {canDeleteDocuments &&
                                (doc.status === 'processing' ||
                                  doc.status === 'pending' ||
                                  doc.status === 'preprocessed') && (
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    side="bottom"
                                    tooltip={t(
                                      'documentPanel.documentManager.cancelDocumentTooltip',
                                      {
                                        defaultValue:
                                          doc.status === 'processing'
                                            ? '请求取消此文档（下一个检查点生效）'
                                            : '取消此文档',
                                      }
                                    )}
                                    className="ml-1 h-6 w-6 shrink-0 rounded-full text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      void handleCancelDocument(doc.id)
                                    }}
                                  >
                                    <XIcon className="h-3.5 w-3.5" />
                                  </Button>
                                )}
                            </div>
                          </TableCell>
                          <TableCell>{doc.content_length ?? '-'}</TableCell>
                          <TableCell>{doc.chunks_count ?? '-'}</TableCell>
                          <TableCell className="truncate">{new Date(doc.created_at).toLocaleString()}</TableCell>
                          <TableCell className="truncate">{new Date(doc.updated_at).toLocaleString()}</TableCell>
                          {canUseDocumentSelection && (
                            <TableCell className="text-center">
                              <Checkbox
                                checked={activeSelectedDocIds.includes(doc.id)}
                                onCheckedChange={(checked) => handleDocumentSelect(doc.id, checked === true)}
                                className="mx-auto"
                              />
                            </TableCell>
                          )}
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
            )}
        </div>
      </div>
    </div>
  )
}
