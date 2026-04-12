import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { backendBaseUrl, popularLabelsDefaultLimit, searchLabelsDefaultLimit } from '@/lib/constants'
import { errorMessage } from '@/lib/utils'
import { useSettingsStore } from '@/stores/settings'
import { useAuthStore } from '@/stores/state'
import { navigationService } from '@/services/navigation'

// Types
export type LightragNodeType = {
  id: string
  labels: string[]
  properties: Record<string, any>
}

export type LightragEdgeType = {
  id: string
  source: string
  target: string
  type: string
  properties: Record<string, any>
}

export type LightragGraphType = {
  nodes: LightragNodeType[]
  edges: LightragEdgeType[]
}

export type LightragStatus = {
  status: 'healthy'
  working_directory: string
  input_directory: string
  configuration: {
    llm_binding: string
    llm_binding_host: string
    llm_model: string
    embedding_binding: string
    embedding_binding_host: string
    embedding_model: string
    kv_storage: string
    doc_status_storage: string
    graph_storage: string
    vector_storage: string
    workspace?: string
    max_graph_nodes?: string
    enable_rerank?: boolean
    rerank_binding?: string | null
    rerank_model?: string | null
    rerank_binding_host?: string | null
    summary_language: string
    force_llm_summary_on_merge: boolean
    max_parallel_insert: number
    max_async: number
    embedding_func_max_async: number
    embedding_batch_num: number
    cosine_threshold: number
    min_rerank_score: number
    related_chunk_number: number
  }
  update_status?: Record<string, any>
  core_version?: string
  api_version?: string
  auth_mode?: 'enabled' | 'disabled'
  pipeline_busy: boolean
  keyed_locks?: {
    process_id: number
    cleanup_performed: {
      mp_cleaned: number
      async_cleaned: number
    }
    current_status: {
      total_mp_locks: number
      pending_mp_cleanup: number
      total_async_locks: number
      pending_async_cleanup: number
    }
  }
  webui_title?: string
  webui_description?: string
}

export type LightragDocumentsScanProgress = {
  is_scanning: boolean
  current_file: string
  indexed_count: number
  total_files: number
  progress: number
}

/**
 * Specifies the retrieval mode:
 * - "naive": Performs a basic search without advanced techniques.
 * - "local": Focuses on context-dependent information.
 * - "global": Utilizes global knowledge.
 * - "hybrid": Combines local and global retrieval methods.
 * - "mix": Integrates knowledge graph and vector retrieval.
 * - "bypass": Bypasses knowledge retrieval and directly uses the LLM.
 */
export type QueryMode = 'naive' | 'local' | 'global' | 'hybrid' | 'mix' | 'bypass'

export type Message = {
  role: 'user' | 'assistant' | 'system'
  content: string
  thinkingContent?: string
  displayContent?: string
  thinkingTime?: number | null
}

export type QueryRequest = {
  query: string
  /** Specifies the retrieval mode. */
  mode: QueryMode
  /** If True, only returns the retrieved context without generating a response. */
  only_need_context?: boolean
  /** If True, only returns the generated prompt without producing a response. */
  only_need_prompt?: boolean
  /** Defines the response format. Examples: 'Multiple Paragraphs', 'Single Paragraph', 'Bullet Points'. */
  response_type?: string
  /** If True, enables streaming output for real-time responses. */
  stream?: boolean
  /** Number of top items to retrieve. Represents entities in 'local' mode and relationships in 'global' mode. */
  top_k?: number
  /** Maximum number of text chunks to retrieve and keep after reranking. */
  chunk_top_k?: number
  /** Maximum number of tokens allocated for entity context in unified token control system. */
  max_entity_tokens?: number
  /** Maximum number of tokens allocated for relationship context in unified token control system. */
  max_relation_tokens?: number
  /** Maximum total tokens budget for the entire query context (entities + relations + chunks + system prompt). */
  max_total_tokens?: number
  /**
   * Stores past conversation history to maintain context.
   * Format: [{"role": "user/assistant", "content": "message"}].
   */
  conversation_history?: Message[]
  /** Number of complete conversation turns (user-assistant pairs) to consider in the response context. */
  history_turns?: number
  /** User-provided prompt for the query. If provided, this will be used instead of the default value from prompt template. */
  user_prompt?: string
  /** Enable reranking for retrieved text chunks. If True but no rerank model is configured, a warning will be issued. Default is True. */
  enable_rerank?: boolean
}

export type QueryResponse = {
  response: string
}

/**
 * A single retrieved chunk as returned by `/query/data`.
 *
 * Text chunks (the common case) only have `chunk_id`, `content`, `file_path`
 * and `reference_id`. Image chunks produced by the multimodal pipeline
 * additionally carry `source_type === 'image_vector'`, `image_blob_id`
 * (the content-hashed blob id, starts with "img-"), and `blob_ref` (the
 * absolute path or URL the backend uses internally).
 *
 * Image bytes for an image chunk can be fetched via `GET /images/{blob_id}`
 * and their full sidecar (caption JSON, source_doc_id backlink, etc.) via
 * `GET /images/{blob_id}/metadata`.
 */
export type RetrievedChunk = {
  chunk_id?: string
  content?: string
  file_path?: string
  reference_id?: string
  source_type?: 'image_vector' | string
  image_blob_id?: string
  blob_ref?: string
  source_doc_id?: string
  source_page?: number
  source_printed_page?: number
  source_page_label?: string | null
  source_bbox?: Record<string, unknown> | null
  picture_index?: number
  page_picture_index?: number
  context_text?: string
  context_chunk_ids?: string[]
  context_chunks?: ImageContextChunk[] | null
  extraction_mode?: string | null
  native_xref?: number | null
  merged_extraction_modes?: string[] | null
  extra?: Record<string, unknown> | null
}

export type ImageContextChunk = {
  chunk_id?: string
  chunk_order_index?: number
  content?: string
}

export type QueryReference = {
  reference_id: string
  file_path: string
}

/**
 * Structured response from `POST /query/data`.
 *
 * `data` is deliberately loose on the backend (`Dict[str, Any]`), so we
 * type it as optional unknown buckets to avoid coupling the WebUI to
 * every backend change.
 */
export type QueryDataResponse = {
  status: 'success' | 'failure' | string
  message: string
  data?: {
    entities?: unknown[]
    relationships?: unknown[]
    chunks?: RetrievedChunk[]
    references?: QueryReference[]
  }
  metadata?: Record<string, unknown>
}

/**
 * Sidecar metadata returned by `GET /images/{blob_id}/metadata`.
 *
 * Not every field is guaranteed — the backend falls back to a minimal
 * shape when the image_metadata KV record is missing but the blob
 * sidecar exists, so consumers should treat every field as optional.
 */
export type ImageMetadata = {
  blob_id: string
  blob_ref?: string | null
  content_type?: string | null
  source_doc_id?: string | null
  source_file_path?: string | null
  source_page?: number | null
  source_printed_page?: number | null
  source_page_label?: string | null
  source_bbox?: Record<string, unknown> | null
  picture_index?: number | null
  page_picture_index?: number | null
  context_text?: string | null
  context_chunk_ids?: string[] | null
  context_chunks?: ImageContextChunk[] | null
  extraction_mode?: string | null
  native_xref?: number | null
  merged_extraction_modes?: string[] | null
  annotation_text?: string | null
  caption_json?: {
    image_category?: string
    sub_type?: string
    caption?: string
    detailed_description?: string
    detected_entities?: string[]
    key_attributes?: Record<string, unknown>
  } | null
  extra?: Record<string, unknown> | null
}

export type EntityUpdateResponse = {
  status: string
  message: string
  data: Record<string, any>
  operation_summary?: {
    merged: boolean
    merge_status: 'success' | 'failed' | 'not_attempted'
    merge_error: string | null
    operation_status: 'success' | 'partial_success' | 'failure'
    target_entity: string | null
    final_entity?: string | null
    renamed?: boolean
  }
}

export type DocActionResponse = {
  status: 'success' | 'partial_success' | 'failure' | 'duplicated'
  message: string
  track_id?: string
  doc_id?: string
  operation_metadata?: Record<string, any>
}

export type ScanResponse = {
  status: 'scanning_started'
  message: string
  track_id: string
}

export type ReprocessFailedResponse = {
  status: 'reprocessing_started'
  message: string
  track_id: string
}

export type RebuildMultimodalResponse = {
  status: 'rebuild_started'
  message: string
  track_id: string
  doc_id: string
}

export type DeleteDocResponse = {
  status: 'deletion_started' | 'busy' | 'not_allowed'
  message: string
  doc_id: string
}

export type DocStatus = 'pending' | 'processing' | 'preprocessed' | 'processed' | 'failed'

export type DocStatusResponse = {
  id: string
  content_summary: string
  content_length: number
  status: DocStatus
  created_at: string
  updated_at: string
  track_id?: string
  chunks_count?: number
  error_msg?: string
  metadata?: Record<string, any>
  file_path: string
}

export type DocsStatusesResponse = {
  statuses: Record<DocStatus, DocStatusResponse[]>
}

export type TrackStatusResponse = {
  track_id: string
  documents: DocStatusResponse[]
  total_count: number
  status_summary: Record<string, number>
}

export type DocumentsRequest = {
  status_filter?: DocStatus | null
  page: number
  page_size: number
  sort_field: 'created_at' | 'updated_at' | 'id' | 'file_path'
  sort_direction: 'asc' | 'desc'
}

export type PaginationInfo = {
  page: number
  page_size: number
  total_count: number
  total_pages: number
  has_next: boolean
  has_prev: boolean
}

export type PaginatedDocsResponse = {
  documents: DocStatusResponse[]
  pagination: PaginationInfo
  status_counts: Record<string, number>
}

export type StatusCountsResponse = {
  status_counts: Record<string, number>
}

export type AuthStatusResponse = {
  auth_configured: boolean
  access_token?: string
  token_type?: string
  auth_mode?: 'enabled' | 'disabled'
  message?: string
  core_version?: string
  api_version?: string
  webui_title?: string
  webui_description?: string
}

export type PipelineStatusResponse = {
  autoscanned: boolean
  busy: boolean
  job_name: string
  job_start?: string
  docs: number
  batchs: number
  cur_batch: number
  request_pending: boolean
  cancellation_requested?: boolean
  latest_message: string
  history_messages?: string[]
  update_status?: Record<string, any>
}

export type LoginResponse = {
  access_token: string
  token_type: string
  auth_mode?: 'enabled' | 'disabled'  // Authentication mode identifier
  message?: string                    // Optional message
  core_version?: string
  api_version?: string
  webui_title?: string
  webui_description?: string
}

export const InvalidApiKeyError = 'Invalid API Key'
export const RequireApiKeError = 'API Key required'

// Axios instance
const axiosInstance = axios.create({
  baseURL: backendBaseUrl,
  headers: {
    'Content-Type': 'application/json'
  }
})

// ========== Token Management ==========
// Prevent multiple requests from triggering token refresh simultaneously
let isRefreshingGuestToken = false;
let refreshTokenPromise: Promise<string> | null = null;

// Silent refresh for guest token
const silentRefreshGuestToken = async (): Promise<string> => {
  // If already refreshing, return the same Promise
  if (isRefreshingGuestToken && refreshTokenPromise) {
    return refreshTokenPromise;
  }

  isRefreshingGuestToken = true;
  refreshTokenPromise = (async () => {
    try {
      // Call /auth-status to get new guest token
      const response = await axios.get('/auth-status', {
        baseURL: backendBaseUrl,
        // This request must skip the interceptor to avoid adding expired token
        headers: { 'X-Skip-Interceptor': 'true' }
      });

      if (response.data.access_token && !response.data.auth_configured) {
        const newToken = response.data.access_token;
        // Update localStorage
        localStorage.setItem('LIGHTRAG-API-TOKEN', newToken);
        // Update auth state
        useAuthStore.getState().login(
          newToken,
          true,
          response.data.core_version,
          response.data.api_version,
          response.data.webui_title || null,
          response.data.webui_description || null
        );
        return newToken;
      } else {
        throw new Error('Failed to get guest token');
      }
    } finally {
      isRefreshingGuestToken = false;
      refreshTokenPromise = null;
    }
  })();

  return refreshTokenPromise;
};

// Interceptor: add api key and check authentication
axiosInstance.interceptors.request.use((config) => {
  // Skip interceptor for token refresh requests
  if (config.headers['X-Skip-Interceptor']) {
    delete config.headers['X-Skip-Interceptor'];
    return config;
  }

  const apiKey = useSettingsStore.getState().apiKey
  const token = localStorage.getItem('LIGHTRAG-API-TOKEN');

  // Always include token if it exists, regardless of path
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  if (apiKey) {
    config.headers['X-API-Key'] = apiKey
  }
  return config
})

// Interceptor：handle token renewal and authentication errors
axiosInstance.interceptors.response.use(
  (response) => {
    // ========== Check for new token from backend ==========
    const newToken = response.headers['x-new-token'];
    if (newToken) {
      localStorage.setItem('LIGHTRAG-API-TOKEN', newToken);

      // Optional: log in development mode
      if (import.meta.env.DEV) {
        console.log('[Auth] Token auto-renewed by backend');
      }

      // Update auth state with renewal tracking
      try {
        const payload = JSON.parse(atob(newToken.split('.')[1]));
        const authStore = useAuthStore.getState();
        if (authStore.isAuthenticated) {
          // Track token renewal time and expiration
          const renewalTime = Date.now();
          const expiresAt = payload.exp ? payload.exp * 1000 : 0;
          authStore.setTokenRenewal(renewalTime, expiresAt);

          // Update username (usually unchanged, but just in case)
          const newUsername = payload.sub;
          if (newUsername && newUsername !== authStore.username) {
            // Need to add setUsername method or just update via login
            // For now, we'll skip username update as it's rare
          }
        }
      } catch (error) {
        console.warn('[Auth] Failed to parse renewed token:', error);
      }
    }
    // ========== End of token renewal check ==========

    return response;
  },
  async (error: AxiosError) => {
    if (error.response) {
      if (error.response?.status === 401) {
        const originalRequest = error.config;

        // 1. For login API, throw error directly
        if (originalRequest?.url?.includes('/login')) {
          throw error;
        }

        // 2. Prevent infinite retry
        if (originalRequest && (originalRequest as any)._retry) {
          navigationService.navigateToLogin();
          return Promise.reject(new Error('Authentication required'));
        }

        // 3. Check if in guest mode
        const authStore = useAuthStore.getState();
        const currentToken = localStorage.getItem('LIGHTRAG-API-TOKEN');
        const isGuest = currentToken && authStore.isGuestMode;

        // 4. Guest mode: silent refresh and retry
        if (isGuest && originalRequest) {
          try {
            const newToken = await silentRefreshGuestToken();

            // Mark as retried to prevent infinite loop
            (originalRequest as any)._retry = true;

            // Update token in request headers
            originalRequest.headers['Authorization'] = `Bearer ${newToken}`;

            // Retry original request
            return axiosInstance(originalRequest);
          } catch (refreshError) {
            console.error('Failed to refresh guest token:', refreshError);
            // Refresh failed, navigate to login
            navigationService.navigateToLogin();
            return Promise.reject(new Error('Failed to refresh authentication'));
          }
        }

        // 5. Non-guest mode: navigate to login page
        navigationService.navigateToLogin();
        return Promise.reject(new Error('Authentication required'));
      }
      throw new Error(
        `${error.response.status} ${error.response.statusText}\n${JSON.stringify(
          error.response.data
        )}\n${error.config?.url}`
      )
    }
    throw error
  }
)

// API methods
export const queryGraphs = async (
  label: string,
  maxDepth: number,
  maxNodes: number
): Promise<LightragGraphType> => {
  const response = await axiosInstance.get(`/graphs?label=${encodeURIComponent(label)}&max_depth=${maxDepth}&max_nodes=${maxNodes}`)
  return response.data
}

export const getGraphLabels = async (): Promise<string[]> => {
  const response = await axiosInstance.get('/graph/label/list')
  return response.data
}

export const getPopularLabels = async (limit: number = popularLabelsDefaultLimit): Promise<string[]> => {
  const response = await axiosInstance.get(`/graph/label/popular?limit=${limit}`)
  return response.data
}

export const searchLabels = async (query: string, limit: number = searchLabelsDefaultLimit): Promise<string[]> => {
  const response = await axiosInstance.get(`/graph/label/search?q=${encodeURIComponent(query)}&limit=${limit}`)
  return response.data
}

export const checkHealth = async (): Promise<
  LightragStatus | { status: 'error'; message: string }
> => {
  try {
    const response = await axiosInstance.get('/health')
    return response.data
  } catch (error) {
    return {
      status: 'error',
      message: errorMessage(error)
    }
  }
}

export const getDocuments = async (): Promise<DocsStatusesResponse> => {
  const response = await axiosInstance.get('/documents')
  return response.data
}

export const scanNewDocuments = async (): Promise<ScanResponse> => {
  const response = await axiosInstance.post('/documents/scan')
  return response.data
}

export const reprocessFailedDocuments = async (): Promise<ReprocessFailedResponse> => {
  const response = await axiosInstance.post('/documents/reprocess_failed')
  return response.data
}

export const rebuildDocumentMultimodal = async (
  docId: string,
  request: { reuse_cache?: boolean } = {}
): Promise<RebuildMultimodalResponse> => {
  const response = await axiosInstance.post(
    `/documents/${encodeURIComponent(docId)}/rebuild_multimodal`,
    request
  )
  return response.data
}

export const getDocumentsScanProgress = async (): Promise<LightragDocumentsScanProgress> => {
  const response = await axiosInstance.get('/documents/scan-progress')
  return response.data
}

export const queryText = async (request: QueryRequest): Promise<QueryResponse> => {
  const response = await axiosInstance.post('/query', request)
  return response.data
}

/**
 * Fetch the structured retrieval data for a query (entities, relationships,
 * chunks, references) without any LLM generation. Used by the WebUI to
 * render image chunks returned by the multimodal pipeline alongside the
 * streamed LLM answer.
 *
 * This runs a real retrieval pass on the backend — callers should only
 * invoke it when they actually need the structured data (e.g. after the
 * streamed answer completes and the user has multimodal enabled).
 */
export const queryData = async (request: QueryRequest): Promise<QueryDataResponse> => {
  const response = await axiosInstance.post('/query/data', request)
  return response.data
}

const viteBackendBaseUrl = (() => {
  const viteEnv =
    typeof import.meta !== 'undefined' && import.meta.env ? import.meta.env : undefined
  const processEnv =
    typeof process !== 'undefined' && process.env ? process.env : undefined
  const rawValue = (viteEnv?.VITE_BACKEND_URL || processEnv?.VITE_BACKEND_URL || '').trim()
  return rawValue ? rawValue.replace(/\/+$/, '') : ''
})()

let fallbackBackendBaseUrl = viteBackendBaseUrl

const buildRequestTargets = (path: string): string[] => {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  const requestTargets = [normalizedPath]

  if (fallbackBackendBaseUrl) {
    requestTargets.push(`${fallbackBackendBaseUrl}${normalizedPath}`)
  }

  return Array.from(new Set(requestTargets))
}

const defaultApiRequestExecutor = <T = unknown>(
  config: AxiosRequestConfig
): Promise<AxiosResponse<T>> => axiosInstance.request<T>(config)

let apiRequestExecutor = defaultApiRequestExecutor

const requestWithBackendFallback = async <T = unknown>(
  config: AxiosRequestConfig,
  validateResponse?: (response: AxiosResponse<T>) => void
): Promise<AxiosResponse<T>> => {
  const requestTargets = buildRequestTargets(String(config.url ?? ''))
  let lastError: unknown = null

  for (const url of requestTargets) {
    try {
      const response = await apiRequestExecutor<T>({
        ...config,
        url
      })
      validateResponse?.(response)
      return response
    } catch (error) {
      lastError = error
    }
  }

  if (lastError instanceof Error) {
    throw lastError
  }

  throw new Error(`Request failed for ${requestTargets.join(', ')}`)
}

/**
 * Build the URL for fetching the raw bytes of an image blob. The WebUI's
 * axios instance is configured with `baseURL` + auth interceptors, but
 * `<img src>` cannot go through axios — so we inline the base URL and
 * rely on the Authorization header being forwarded by the browser via
 * the api-key query / same-origin cookie. When neither is in play,
 * callers can use `fetchImageBlobUrl` below to get a signed object URL.
 */
export const buildImageBlobUrl = (blobId: string): string =>
  `${backendBaseUrl}/images/${encodeURIComponent(blobId)}`

/**
 * Fetch an image blob through the authenticated axios instance and
 * return an object URL suitable for `<img src>`. The caller MUST call
 * `URL.revokeObjectURL(url)` when the component unmounts to avoid leaks.
 */
export const fetchImageBlobUrl = async (blobId: string): Promise<string> => {
  const response = await requestWithBackendFallback<Blob>(
    {
      method: 'get',
      url: `/images/${encodeURIComponent(blobId)}`,
      responseType: 'blob'
    },
    (result) => {
      const headerContentType = String(
        result.headers?.['content-type'] ?? ''
      ).toLowerCase()
      const blobContentType =
        typeof Blob !== 'undefined' && result.data instanceof Blob
          ? result.data.type.toLowerCase()
          : ''
      const effectiveContentType = headerContentType || blobContentType

      if (effectiveContentType && !effectiveContentType.startsWith('image/')) {
        throw new Error(
          `Unexpected image content type: ${effectiveContentType}`
        )
      }
    }
  )
  return URL.createObjectURL(response.data as Blob)
}

/**
 * Fetch the sidecar metadata (caption JSON, source-doc backlink, etc.)
 * for an image blob. Returns `null` on 404 so callers can show the
 * image without a caption instead of throwing.
 */
export const fetchImageMetadata = async (
  blobId: string
): Promise<ImageMetadata | null> => {
  try {
    const response = await requestWithBackendFallback<ImageMetadata>({
      method: 'get',
      url: `/images/${encodeURIComponent(blobId)}/metadata`
    })
    return response.data as ImageMetadata
  } catch (err: any) {
    if (err?.response?.status === 404) {
      return null
    }
    throw err
  }
}

export const __resetApiRequestExecutorForTests = (): void => {
  apiRequestExecutor = defaultApiRequestExecutor
  fallbackBackendBaseUrl = viteBackendBaseUrl
}

export const __setApiRequestExecutorForTests = (
  executor: typeof defaultApiRequestExecutor
): void => {
  apiRequestExecutor = executor
}

export const __setFallbackBackendBaseUrlForTests = (url: string): void => {
  fallbackBackendBaseUrl = url.trim().replace(/\/+$/, '')
}

export const queryTextStream = async (
  request: QueryRequest,
  onChunk: (chunk: string) => void,
  onError?: (error: string) => void
) => {
  const apiKey = useSettingsStore.getState().apiKey;
  const token = localStorage.getItem('LIGHTRAG-API-TOKEN');
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    'Accept': 'application/x-ndjson',
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  if (apiKey) {
    headers['X-API-Key'] = apiKey;
  }

  try {
    const response = await fetch(`${backendBaseUrl}/query/stream`, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      // Handle 401 Unauthorized error specifically
      if (response.status === 401) {
        // Check if in guest mode
        const authStore = useAuthStore.getState();
        const currentToken = localStorage.getItem('LIGHTRAG-API-TOKEN');
        const isGuest = currentToken && authStore.isGuestMode;

        if (isGuest) {
          try {
            // Silent refresh token for guest mode
            const newToken = await silentRefreshGuestToken();

            // Retry stream request with new token
            const retryHeaders = { ...headers };
            retryHeaders['Authorization'] = `Bearer ${newToken}`;

            const retryResponse = await fetch(`${backendBaseUrl}/query/stream`, {
              method: 'POST',
              headers: retryHeaders,
              body: JSON.stringify(request),
            });

            if (!retryResponse.ok) {
              throw new Error(`HTTP error! status: ${retryResponse.status}`);
            }

            // Retry successful, process stream response
            // Re-execute the stream processing logic with retryResponse
            if (!retryResponse.body) {
              throw new Error('Response body is null');
            }

            const reader = retryResponse.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
              const { done, value } = await reader.read();
              if (done) break;

              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() || '';

              for (const line of lines) {
                if (line.trim()) {
                  try {
                    const parsed = JSON.parse(line);
                    if (parsed.response) {
                      onChunk(parsed.response);
                    } else if (parsed.error) {
                      onError?.(parsed.error);
                    }
                  } catch (parseError) {
                    console.error('Failed to parse JSON:', parseError, 'Line:', line);
                    onError?.(`JSON parse error: ${parseError}`);
                  }
                }
              }
            }

            // Process any remaining data in buffer
            if (buffer.trim()) {
              try {
                const parsed = JSON.parse(buffer);
                if (parsed.response) {
                  onChunk(parsed.response);
                } else if (parsed.error) {
                  onError?.(parsed.error);
                }
              } catch (parseError) {
                console.error('Failed to parse final buffer:', parseError);
              }
            }

            return; // Successfully completed retry
          } catch (refreshError) {
            console.error('Failed to refresh guest token for streaming:', refreshError);
            navigationService.navigateToLogin();
            throw new Error('Failed to refresh authentication', { cause: refreshError });
          }
        }

        // Non-guest mode: navigate to login page
        navigationService.navigateToLogin();

        // Create a specific authentication error
        const authError = new Error('Authentication required');
        throw authError;
      }

      // Handle other common HTTP errors with specific messages
      let errorBody = 'Unknown error';
      try {
        errorBody = await response.text(); // Try to get error details from body
      } catch { /* ignore */ }

      // Format error message similar to axios interceptor for consistency
      const url = `${backendBaseUrl}/query/stream`;
      throw new Error(
        `${response.status} ${response.statusText}\n${JSON.stringify(
          { error: errorBody }
        )}\n${url}`
      );
    }

    if (!response.body) {
      throw new Error('Response body is null');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break; // Stream finished
      }

      // Decode the chunk and add to buffer
      buffer += decoder.decode(value, { stream: true }); // stream: true handles multi-byte chars split across chunks

      // Process complete lines (NDJSON)
      const lines = buffer.split('\n');
      buffer = lines.pop() || ''; // Keep potentially incomplete line in buffer

      for (const line of lines) {
        if (line.trim()) {
          try {
            const parsed = JSON.parse(line);
            if (parsed.response) {
              onChunk(parsed.response);
            } else if (parsed.error && onError) {
              onError(parsed.error);
            }
          } catch (error) {
            console.error('Error parsing stream chunk:', line, error);
            if (onError) onError(`Error parsing server response: ${line}`);
          }
        }
      }
    }

    // Process any remaining data in the buffer after the stream ends
    if (buffer.trim()) {
      try {
        const parsed = JSON.parse(buffer);
        if (parsed.response) {
          onChunk(parsed.response);
        } else if (parsed.error && onError) {
          onError(parsed.error);
        }
      } catch (error) {
        console.error('Error parsing final chunk:', buffer, error);
        if (onError) onError(`Error parsing final server response: ${buffer}`);
      }
    }

  } catch (error) {
    const message = errorMessage(error);

    // Check if this is an authentication error
    if (message === 'Authentication required') {
      // Already navigated to login page in the response.status === 401 block
      console.error('Authentication required for stream request');
      if (onError) {
        onError('Authentication required');
      }
      return; // Exit early, no need for further error handling
    }

    // Check for specific HTTP error status codes in the error message
    const statusCodeMatch = message.match(/^(\d{3})\s/);
    if (statusCodeMatch) {
      const statusCode = parseInt(statusCodeMatch[1], 10);

      // Handle specific status codes with user-friendly messages
      let userMessage = message;

      switch (statusCode) {
        case 403:
          userMessage = 'You do not have permission to access this resource (403 Forbidden)';
          console.error('Permission denied for stream request:', message);
          break;
        case 404:
          userMessage = 'The requested resource does not exist (404 Not Found)';
          console.error('Resource not found for stream request:', message);
          break;
        case 429:
          userMessage = 'Too many requests, please try again later (429 Too Many Requests)';
          console.error('Rate limited for stream request:', message);
          break;
        case 500:
        case 502:
        case 503:
        case 504:
          userMessage = `Server error, please try again later (${statusCode})`;
          console.error('Server error for stream request:', message);
          break;
        default:
          console.error('Stream request failed with status code:', statusCode, message);
      }

      if (onError) {
        onError(userMessage);
      }
      return;
    }

    // Handle network errors (like connection refused, timeout, etc.)
    if (message.includes('NetworkError') ||
        message.includes('Failed to fetch') ||
        message.includes('Network request failed')) {
      console.error('Network error for stream request:', message);
      if (onError) {
        onError('Network connection error, please check your internet connection');
      }
      return;
    }

    // Handle JSON parsing errors during stream processing
    if (message.includes('Error parsing') || message.includes('SyntaxError')) {
      console.error('JSON parsing error in stream:', message);
      if (onError) {
        onError('Error processing response data');
      }
      return;
    }

    // Handle other errors
    console.error('Unhandled stream error:', message);
    if (onError) {
      onError(message);
    } else {
      console.error('No error handler provided for stream error:', message);
    }
  }
};

export const insertText = async (text: string): Promise<DocActionResponse> => {
  const response = await axiosInstance.post('/documents/text', { text })
  return response.data
}

export const insertTexts = async (texts: string[]): Promise<DocActionResponse> => {
  const response = await axiosInstance.post('/documents/texts', { texts })
  return response.data
}

export const uploadDocument = async (
  file: File,
  onUploadProgress?: (percentCompleted: number) => void
): Promise<DocActionResponse> => {
  const formData = new FormData()
  formData.append('file', file)

  const response = await axiosInstance.post('/documents/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    },
    // prettier-ignore
    onUploadProgress:
      onUploadProgress !== undefined
        ? (progressEvent) => {
          const percentCompleted = Math.round((progressEvent.loaded * 100) / progressEvent.total!)
          onUploadProgress(percentCompleted)
        }
        : undefined
  })
  return response.data
}

export const batchUploadDocuments = async (
  files: File[],
  onUploadProgress?: (fileName: string, percentCompleted: number) => void
): Promise<DocActionResponse[]> => {
  return await Promise.all(
    files.map(async (file) => {
      return await uploadDocument(file, (percentCompleted) => {
        onUploadProgress?.(file.name, percentCompleted)
      })
    })
  )
}

export const clearDocuments = async (): Promise<DocActionResponse> => {
  const response = await axiosInstance.delete('/documents')
  return response.data
}

export const clearCache = async (): Promise<{
  status: 'success' | 'fail'
  message: string
}> => {
  const response = await axiosInstance.post('/documents/clear_cache', {})
  return response.data
}

export const deleteDocuments = async (
  docIds: string[],
  deleteFile: boolean = false,
  deleteLLMCache: boolean = false
): Promise<DeleteDocResponse> => {
  const response = await axiosInstance.delete('/documents/delete_document', {
    data: { doc_ids: docIds, delete_file: deleteFile, delete_llm_cache: deleteLLMCache }
  })
  return response.data
}

export const getAuthStatus = async (): Promise<AuthStatusResponse> => {
  try {
    // Add a timeout to the request to prevent hanging
    const response = await axiosInstance.get('/auth-status', {
      timeout: 5000, // 5 second timeout
      headers: {
        'Accept': 'application/json' // Explicitly request JSON
      }
    });

    // Check if response is HTML (which indicates a redirect or wrong endpoint)
    const contentType = response.headers['content-type'] || '';
    if (contentType.includes('text/html')) {
      console.warn('Received HTML response instead of JSON for auth-status endpoint');
      return {
        auth_configured: true,
        auth_mode: 'enabled'
      };
    }

    // Strict validation of the response data
    if (response.data &&
        typeof response.data === 'object' &&
        'auth_configured' in response.data &&
        typeof response.data.auth_configured === 'boolean') {

      // For unconfigured auth, ensure we have an access token
      if (!response.data.auth_configured) {
        if (response.data.access_token && typeof response.data.access_token === 'string') {
          return response.data;
        } else {
          console.warn('Auth not configured but no valid access token provided');
        }
      } else {
        // For configured auth, just return the data
        return response.data;
      }
    }

    // If response data is invalid but we got a response, log it
    console.warn('Received invalid auth status response:', response.data);

    // Default to auth configured if response is invalid
    return {
      auth_configured: true,
      auth_mode: 'enabled'
    };
  } catch (error) {
    // If the request fails, assume authentication is configured
    console.error('Failed to get auth status:', errorMessage(error));
    return {
      auth_configured: true,
      auth_mode: 'enabled'
    };
  }
}

export const getPipelineStatus = async (): Promise<PipelineStatusResponse> => {
  const response = await axiosInstance.get('/documents/pipeline_status')
  return response.data
}

export const cancelPipeline = async (): Promise<{
  status: 'cancellation_requested' | 'not_busy'
  message: string
}> => {
  const response = await axiosInstance.post('/documents/cancel_pipeline')
  return response.data
}

export const loginToServer = async (username: string, password: string): Promise<LoginResponse> => {
  const formData = new URLSearchParams();
  formData.append('username', username);
  formData.append('password', password);
  formData.append('grant_type', 'password');

  const response = await axiosInstance.post('/login', formData, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
  });

  return response.data;
}

/**
 * Updates an entity's properties in the knowledge graph
 * @param entityName The name of the entity to update
 * @param updatedData Dictionary containing updated attributes
 * @param allowRename Whether to allow renaming the entity (default: false)
 * @param allowMerge Whether to merge into an existing entity when renaming to a duplicate name
 * @returns Promise with the updated entity information
 */
export const updateEntity = async (
  entityName: string,
  updatedData: Record<string, any>,
  allowRename: boolean = false,
  allowMerge: boolean = false
): Promise<EntityUpdateResponse> => {
  const response = await axiosInstance.post('/graph/entity/edit', {
    entity_name: entityName,
    updated_data: updatedData,
    allow_rename: allowRename,
    allow_merge: allowMerge
  })
  return response.data
}

/**
 * Updates a relation's properties in the knowledge graph
 * @param sourceEntity The source entity name
 * @param targetEntity The target entity name
 * @param updatedData Dictionary containing updated attributes
 * @returns Promise with the updated relation information
 */
export const updateRelation = async (
  sourceEntity: string,
  targetEntity: string,
  updatedData: Record<string, any>
): Promise<DocActionResponse> => {
  const response = await axiosInstance.post('/graph/relation/edit', {
    source_id: sourceEntity,
    target_id: targetEntity,
    updated_data: updatedData
  })
  return response.data
}

/**
 * Checks if an entity name already exists in the knowledge graph
 * @param entityName The entity name to check
 * @returns Promise with boolean indicating if the entity exists
 */
export const checkEntityNameExists = async (entityName: string): Promise<boolean> => {
  try {
    const response = await axiosInstance.get(`/graph/entity/exists?name=${encodeURIComponent(entityName)}`)
    return response.data.exists
  } catch (error) {
    console.error('Error checking entity name:', error)
    return false
  }
}

/**
 * Get the processing status of documents by tracking ID
 * @param trackId The tracking ID returned from upload, text, or texts endpoints
 * @returns Promise with the track status response containing documents and summary
 */
export const getTrackStatus = async (trackId: string): Promise<TrackStatusResponse> => {
  const response = await axiosInstance.get(`/documents/track_status/${encodeURIComponent(trackId)}`)
  return response.data
}

type InFlightPaginatedDocumentRequest = {
  controller: AbortController
  promise: Promise<PaginatedDocsResponse>
  subscriberCount: number
}

const getPaginatedDocumentsRequestKey = (request: DocumentsRequest): string =>
  JSON.stringify(request)

// Deduplicate in-flight paginated document requests with identical parameters.
// This prevents duplicate backend calls caused by overlapping timers/effects or
// React StrictMode double-mount behavior in development.
const inFlightPaginatedDocumentRequests = new Map<
  string,
  InFlightPaginatedDocumentRequest
>()

const releasePaginatedDocumentSubscriber = (
  requestKey: string,
  requestEntry: InFlightPaginatedDocumentRequest,
  abortIfLastSubscriber: boolean
): void => {
  requestEntry.subscriberCount = Math.max(0, requestEntry.subscriberCount - 1)

  if (requestEntry.subscriberCount !== 0) {
    return
  }

  if (inFlightPaginatedDocumentRequests.get(requestKey) === requestEntry) {
    inFlightPaginatedDocumentRequests.delete(requestKey)
  }

  if (abortIfLastSubscriber) {
    requestEntry.controller.abort()
  }
}

const subscribeToPaginatedDocumentsRequest = (
  request: DocumentsRequest
): {
  requestKey: string
  requestEntry: InFlightPaginatedDocumentRequest
  release: (abortIfLastSubscriber: boolean) => void
} => {
  const requestKey = getPaginatedDocumentsRequestKey(request)
  let requestEntry = inFlightPaginatedDocumentRequests.get(requestKey)

  if (!requestEntry) {
    const controller = new AbortController()
    requestEntry = {
      controller,
      subscriberCount: 0,
      promise: paginatedDocumentsPost(request, controller)
        .finally(() => {
          if (inFlightPaginatedDocumentRequests.get(requestKey) === requestEntry) {
            inFlightPaginatedDocumentRequests.delete(requestKey)
          }
        })
    }
    inFlightPaginatedDocumentRequests.set(requestKey, requestEntry)
  }

  requestEntry.subscriberCount += 1

  let released = false
  const release = (abortIfLastSubscriber: boolean): void => {
    if (released) {
      return
    }
    released = true
    releasePaginatedDocumentSubscriber(
      requestKey,
      requestEntry,
      abortIfLastSubscriber
    )
  }

  return {
    requestKey,
    requestEntry,
    release
  }
}

const defaultPaginatedDocumentsPost = async (
  request: DocumentsRequest,
  controller: AbortController
): Promise<PaginatedDocsResponse> => {
  const response = await axiosInstance.post('/documents/paginated', request, {
    signal: controller.signal
  })
  return response.data
}

let paginatedDocumentsPost = defaultPaginatedDocumentsPost

export const abortDocumentsPaginated = (request: DocumentsRequest): void => {
  const requestKey = getPaginatedDocumentsRequestKey(request)
  const inFlightRequest = inFlightPaginatedDocumentRequests.get(requestKey)

  if (!inFlightRequest) {
    return
  }

  inFlightPaginatedDocumentRequests.delete(requestKey)
  inFlightRequest.controller.abort()
}

export const __resetPaginatedDocumentRequestsForTests = (): void => {
  for (const { controller } of inFlightPaginatedDocumentRequests.values()) {
    controller.abort()
  }
  inFlightPaginatedDocumentRequests.clear()
  paginatedDocumentsPost = defaultPaginatedDocumentsPost
}

export const __setPaginatedDocumentsPostForTests = (
  post: typeof defaultPaginatedDocumentsPost
): void => {
  paginatedDocumentsPost = post
}

/**
 * Get documents with pagination support
 * @param request The pagination request parameters
 * @returns Promise with paginated documents response
 */
export const getDocumentsPaginated = async (request: DocumentsRequest): Promise<PaginatedDocsResponse> => {
  const { requestEntry, release } = subscribeToPaginatedDocumentsRequest(request)

  try {
    return await requestEntry.promise
  } finally {
    release(false)
  }
}

export const getDocumentsPaginatedWithTimeout = (
  request: DocumentsRequest,
  timeoutMs: number = 30000,
  errorMsg: string = 'Document fetch timeout'
): Promise<PaginatedDocsResponse> => {
  const { requestEntry, release } = subscribeToPaginatedDocumentsRequest(request)

  return new Promise<PaginatedDocsResponse>((resolve, reject) => {
    let timedOut = false
    const timeoutId = setTimeout(() => {
      timedOut = true
      release(true)
      reject(new Error(errorMsg))
    }, timeoutMs)

    requestEntry.promise
      .then(response => {
        if (timedOut) {
          return
        }
        clearTimeout(timeoutId)
        release(false)
        resolve(response)
      })
      .catch(error => {
        if (timedOut) {
          return
        }
        clearTimeout(timeoutId)
        release(false)
        reject(error)
      })
  })
}

/**
 * Get counts of documents by status
 * @returns Promise with status counts response
 */
export const getDocumentStatusCounts = async (): Promise<StatusCountsResponse> => {
  const response = await axiosInstance.get('/documents/status_counts')
  return response.data
}
