import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { backendBaseUrl, popularLabelsDefaultLimit, searchLabelsDefaultLimit } from '@/lib/constants'
import { useKBStore } from '@/stores/kb'
import { errorMessage } from '@/lib/utils'
import { useSettingsStore } from '@/stores/settings'
import { useAuthStore } from '@/stores/state'
import { navigationService } from '@/services/navigation'
import { toast } from 'sonner'
import { normalizeMembershipClaims, type MembershipClaim } from '@/lib/permissions'

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
  auth_mode?: 'local' | 'setup_required'
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
  // Unix epoch (ms) when the message was created — used to label when a
  // question was asked / answered. Optional so history persisted before
  // this field existed still hydrates cleanly.
  timestamp?: number | null
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
  knowledge_base_id?: string | null
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
  all_kbs?: boolean
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
  auth_mode?: 'local' | 'setup_required'
  available_providers?: string[]
  supports_password_login?: boolean
  supports_refresh_tokens?: boolean
  supports_user_management?: boolean
  supports_self_registration?: boolean
  self_registration_role?: string | null
  message?: string
  core_version?: string
  api_version?: string
  webui_title?: string
  webui_description?: string
}

export type RegisterAccountResponse = {
  access_token: string
  token_type: string
  refresh_token?: string
  auth_mode?: string
  self_registered?: boolean
  personal_workspace_id?: string
}

export const registerAccount = async (
  username: string,
  password: string
): Promise<RegisterAccountResponse> => {
  const response = await axiosInstance.post('/auth/register', {
    username,
    password,
  })
  return response.data
}

export type PipelineStatusResponse = {
  autoscanned: boolean
  busy: boolean
  job_name: string
  job_start?: string
  docs: number
  batchs: number
  cur_batch: number
  total_chunks?: number
  processed_chunks?: number
  current_stage?: string
  current_stage_label?: string
  stage_unit?: string
  stage_total?: number
  stage_processed?: number
  stage_remaining?: number
  stage_elapsed_seconds?: number
  stage_eta_seconds?: number | null
  request_pending: boolean
  cancellation_requested?: boolean
  latest_message: string
  history_messages?: string[]
  update_status?: Record<string, any>
}

export type LoginResponse = {
  access_token: string
  token_type: string
  auth_mode?: 'local' | 'setup_required'
  message?: string                    // Optional message
  core_version?: string
  api_version?: string
  webui_title?: string
  webui_description?: string
}

export type MembershipEntry = {
  membership_id: string
  user_id: string
  username: string
  workspace_id: string
  kb_id?: string | null
  role: 'owner' | 'admin' | 'editor' | 'viewer'
  source: string
  created_at: string
  updated_at: string
}

export type MembershipListResponse = {
  members: MembershipEntry[]
  total_count: number
}

export type MembershipMutationResponse = {
  status: 'created' | 'updated'
  message: string
  member: MembershipEntry
}

export type MembershipDeleteResponse = {
  status: 'deleted'
  message: string
  user_id: string
  workspace_id: string
  kb_id?: string | null
}

export type KnowledgeBaseRecord = {
  id: string
  workspace_id: string
  name: string
  description: string
  created_at: string
  config_override: Record<string, any>
  status: string
  category: string
}

export type KnowledgeBaseListResponse = {
  items: KnowledgeBaseRecord[]
  total_count: number
}

export type KnowledgeBaseCategoriesResponse = {
  workspace_id: string
  categories: string[]
}

export type KnowledgeBaseCreateRequest = {
  kb_id?: string
  name?: string
  description?: string
  config_override?: Record<string, any>
  status?: string
  category?: string
}

export type KnowledgeBaseUpdateRequest = {
  name?: string
  description?: string
  config_override?: Record<string, any>
  status?: string
  // Omit = unchanged. "" = clear back to uncategorised. Any other value = set/rename.
  category?: string
}

export type KnowledgeBaseMutationResponse = {
  status: 'created' | 'updated'
  message: string
  kb: KnowledgeBaseRecord
}

export type KnowledgeBaseDeleteResponse = {
  status: 'deleted'
  message: string
  workspace_id: string
  kb_id: string
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

const decodeTokenClaims = (
  token: string
): {
  sub?: string
  role?: string
  exp?: number
  memberships?: MembershipClaim[]
} => {
  try {
    const payload = JSON.parse(atob(token.split('.')[1] || ''))
    return {
      ...payload,
      memberships: normalizeMembershipClaims(payload.memberships),
    }
  } catch (error) {
    console.warn('[Auth] Failed to decode token claims:', error)
    return {}
  }
}

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

  // Inject the current workspace / KB scope on root-level endpoints
  // that can't read the scope from path params. The backend dependency
  // resolver accepts ``X-Workspace-Id`` and ``X-KB-Id`` as a fallback
  // when path segments don't supply them.
  try {
    const { activeWorkspaceId, activeKbId } = useKBStore.getState()
    if (activeWorkspaceId && !config.headers['X-Workspace-Id']) {
      config.headers['X-Workspace-Id'] = activeWorkspaceId
    }
    if (activeKbId && !config.headers['X-KB-Id']) {
      config.headers['X-KB-Id'] = activeKbId
    }
  } catch {
    // Scope headers are best-effort; a failure here must never block
    // the request.
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
        const payload = decodeTokenClaims(newToken)
        const authStore = useAuthStore.getState();
        if (authStore.isAuthenticated) {
          // Track token renewal time and expiration
          const renewalTime = Date.now();
          const expiresAt = payload.exp ? payload.exp * 1000 : 0;
          authStore.setTokenRenewal(renewalTime, expiresAt);
          useAuthStore.setState({
            username: payload.sub || authStore.username,
            role: payload.role || authStore.role,
            memberships: payload.memberships || authStore.memberships,
            tokenExpiresAt: expiresAt || authStore.tokenExpiresAt,
          })
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

        // 3. Any 401 means the current session is no longer valid and the user must sign in again.
        navigationService.navigateToLogin();
        return Promise.reject(new Error('Authentication required'));
      }
      if (error.response?.status === 403) {
        const detail =
          typeof error.response.data === 'object' && error.response.data && 'detail' in error.response.data
            ? String((error.response.data as Record<string, unknown>).detail)
            : 'You do not have permission to perform this action.'
        toast.error(detail)
      }
      // Rewrap into a plain Error for uniform message formatting, but keep
      // the status code + response payload attached so downstream callers
      // can branch on specific HTTP conditions (e.g. DocumentManager
      // swallowing 404 "KB not linked" into an empty state instead of a
      // blocking toast).
      const composed = new Error(
        `${error.response.status} ${error.response.statusText}\n${JSON.stringify(
          error.response.data
        )}\n${error.config?.url}`
      ) as Error & { status?: number; response?: AxiosError['response'] }
      composed.status = error.response.status
      composed.response = error.response
      throw composed
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

export const listWorkspaceMembers = async (
  workspaceId: string
): Promise<MembershipListResponse> => {
  const response = await axiosInstance.get(`/workspaces/${encodeURIComponent(workspaceId)}/members`)
  return response.data
}

export const createWorkspaceMember = async (
  workspaceId: string,
  payload: { username: string; role: MembershipEntry['role'] }
): Promise<MembershipMutationResponse> => {
  const response = await axiosInstance.post(
    `/workspaces/${encodeURIComponent(workspaceId)}/members`,
    payload
  )
  return response.data
}

export const updateWorkspaceMemberRole = async (
  workspaceId: string,
  userId: string,
  role: MembershipEntry['role']
): Promise<MembershipMutationResponse> => {
  const response = await axiosInstance.put(
    `/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
    { role }
  )
  return response.data
}

export const deleteWorkspaceMember = async (
  workspaceId: string,
  userId: string
): Promise<MembershipDeleteResponse> => {
  const response = await axiosInstance.delete(
    `/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`
  )
  return response.data
}

export const listKnowledgeBaseMembers = async (
  workspaceId: string,
  kbId: string
): Promise<MembershipListResponse> => {
  const response = await axiosInstance.get(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}/members`
  )
  return response.data
}

export const createKnowledgeBaseMember = async (
  workspaceId: string,
  kbId: string,
  payload: { username: string; role: MembershipEntry['role'] }
): Promise<MembershipMutationResponse> => {
  const response = await axiosInstance.post(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}/members`,
    payload
  )
  return response.data
}

export const updateKnowledgeBaseMemberRole = async (
  workspaceId: string,
  kbId: string,
  userId: string,
  role: MembershipEntry['role']
): Promise<MembershipMutationResponse> => {
  const response = await axiosInstance.put(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}/members/${encodeURIComponent(userId)}`,
    { role }
  )
  return response.data
}

export const deleteKnowledgeBaseMember = async (
  workspaceId: string,
  kbId: string,
  userId: string
): Promise<MembershipDeleteResponse> => {
  const response = await axiosInstance.delete(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}/members/${encodeURIComponent(userId)}`
  )
  return response.data
}

// ---------------------------------------------------------------------------
// Workspace metadata CRUD (Phase W1)
// ---------------------------------------------------------------------------

export type WorkspaceRecord = {
  id: string
  name: string
  description: string | null
  owner_user_id: string | null
  created_at: string
  updated_at: string
}

export type WorkspaceListResponse = {
  items: WorkspaceRecord[]
  total_count: number
}

export type WorkspaceCreateRequest = {
  id?: string
  name: string
  description?: string | null
}

export type WorkspaceUpdateRequest = {
  name?: string
  description?: string | null
}

export type WorkspaceDeleteResponse = {
  status: 'deleted'
  message: string
  id: string
}

export const listWorkspaces = async (): Promise<WorkspaceListResponse> => {
  const response = await axiosInstance.get('/workspaces')
  return response.data
}

export const getWorkspace = async (
  workspaceId: string
): Promise<WorkspaceRecord> => {
  const response = await axiosInstance.get(
    `/workspaces/${encodeURIComponent(workspaceId)}`
  )
  return response.data
}

export const createWorkspace = async (
  payload: WorkspaceCreateRequest
): Promise<WorkspaceRecord> => {
  const response = await axiosInstance.post('/workspaces', payload)
  return response.data
}

export const updateWorkspace = async (
  workspaceId: string,
  payload: WorkspaceUpdateRequest
): Promise<WorkspaceRecord> => {
  const response = await axiosInstance.patch(
    `/workspaces/${encodeURIComponent(workspaceId)}`,
    payload
  )
  return response.data
}

export const deleteWorkspace = async (
  workspaceId: string
): Promise<WorkspaceDeleteResponse> => {
  const response = await axiosInstance.delete(
    `/workspaces/${encodeURIComponent(workspaceId)}`
  )
  return response.data
}

export const listKnowledgeBases = async (
  workspaceId: string
): Promise<KnowledgeBaseListResponse> => {
  const response = await axiosInstance.get(`/workspaces/${encodeURIComponent(workspaceId)}/kb`)
  return response.data
}

export const listAllKnowledgeBases = async (): Promise<KnowledgeBaseListResponse> => {
  // Global KB pool — every KB registered anywhere. Used by the
  // "link existing KB" picker on the workspace management page.
  const response = await axiosInstance.get('/kb')
  return response.data
}

export const listKnowledgeBaseCategories = async (
  workspaceId: string
): Promise<KnowledgeBaseCategoriesResponse> => {
  const response = await axiosInstance.get(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/categories`
  )
  return response.data
}

export type LinkKbResponse = {
  status: 'linked' | 'already_linked'
  message: string
  workspace_id: string
  kb_id: string
}

export const linkKnowledgeBaseToWorkspace = async (
  workspaceId: string,
  kbId: string
): Promise<LinkKbResponse> => {
  const response = await axiosInstance.post(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/link`,
    { kb_id: kbId }
  )
  return response.data
}

export type UnlinkKbResponse = {
  status: 'unlinked'
  message: string
  workspace_id: string
  kb_id: string
  remaining_links: number
}

export const unlinkKnowledgeBaseFromWorkspace = async (
  workspaceId: string,
  kbId: string
): Promise<UnlinkKbResponse> => {
  const response = await axiosInstance.delete(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}/link`
  )
  return response.data
}

export type BootstrapAdminResponse = {
  access_token: string
  token_type: string
  refresh_token?: string
  auth_mode?: string
  bootstrap?: boolean
}

export const bootstrapAdmin = async (
  username: string,
  password: string
): Promise<BootstrapAdminResponse> => {
  const response = await axiosInstance.post('/auth/bootstrap-admin', {
    username,
    password,
  })
  return response.data
}

// ---------------------------------------------------------------------------
// Audit log API (PR-AUDIT-3)
// ---------------------------------------------------------------------------

export type AuditOutcome = 'success' | 'denied' | 'error'

export type AuditEventResponse = {
  id: string
  occurred_at: string
  workspace_id: string | null
  kb_id: string | null
  action: string
  outcome: AuditOutcome
  actor: {
    user_id: string | null
    username: string | null
    role: string | null
  }
  resource: {
    type: string
    id: string | null
  }
  http: {
    method: string | null
    path: string | null
    status: number | null
  }
  client: {
    ip: string | null
    user_agent: string | null
  }
  metadata: Record<string, any> | null
}

export type AuditEventListResponse = {
  events: AuditEventResponse[]
  total_count: number
  next_offset: number | null
}

export type AuditListFilters = {
  actor_user_id?: string
  action?: string
  outcome?: AuditOutcome
  since?: string
  until?: string
  limit?: number
  offset?: number
}

export const listAuditEvents = async (
  workspaceId: string,
  filters: AuditListFilters = {}
): Promise<AuditEventListResponse> => {
  const response = await axiosInstance.get(
    `/workspaces/${encodeURIComponent(workspaceId)}/audit`,
    { params: filters }
  )
  return response.data
}

export const getAuditEvent = async (
  workspaceId: string,
  eventId: string
): Promise<AuditEventResponse> => {
  const response = await axiosInstance.get(
    `/workspaces/${encodeURIComponent(workspaceId)}/audit/${encodeURIComponent(eventId)}`
  )
  return response.data
}

export const deleteAuditEventsBefore = async (
  workspaceId: string,
  before: string
): Promise<{ status: 'deleted'; deleted_count: number; before: string }> => {
  const response = await axiosInstance.delete(
    `/workspaces/${encodeURIComponent(workspaceId)}/audit`,
    { params: { before } }
  )
  return response.data
}

export const getKnowledgeBase = async (
  workspaceId: string,
  kbId: string
): Promise<KnowledgeBaseRecord> => {
  const response = await axiosInstance.get(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}`
  )
  return response.data
}

export const createKnowledgeBase = async (
  workspaceId: string,
  payload: KnowledgeBaseCreateRequest
): Promise<KnowledgeBaseMutationResponse> => {
  const response = await axiosInstance.post(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb`,
    payload
  )
  return response.data
}

export const updateKnowledgeBase = async (
  workspaceId: string,
  kbId: string,
  payload: KnowledgeBaseUpdateRequest
): Promise<KnowledgeBaseMutationResponse> => {
  const response = await axiosInstance.patch(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}`,
    payload
  )
  return response.data
}

export const deleteKnowledgeBase = async (
  workspaceId: string,
  kbId: string
): Promise<KnowledgeBaseDeleteResponse> => {
  const response = await axiosInstance.delete(
    `/workspaces/${encodeURIComponent(workspaceId)}/kb/${encodeURIComponent(kbId)}`
  )
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
  // Mirror the axios request-interceptor: streaming uses raw fetch(),
  // so we have to inject the workspace / KB scope by hand. Without
  // this the backend falls back to the default workspace and a non-
  // default-workspace query silently hits the wrong data ("no context"
  // from the default KB's documents instead of the user's own KB).
  try {
    const { activeWorkspaceId, activeKbId } = useKBStore.getState();
    if (activeWorkspaceId) {
      (headers as Record<string, string>)['X-Workspace-Id'] = activeWorkspaceId;
    }
    if (activeKbId) {
      (headers as Record<string, string>)['X-KB-Id'] = activeKbId;
    }
  } catch {
    // Scope injection is best-effort; never let a store read crash the
    // streaming call.
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
  onUploadProgress?: (percentCompleted: number) => void,
  // Optional override — when set, the upload targets this KB instead
  // of whatever ``useKBStore.activeKbId`` points at. Lets the upload
  // dialog offer its own KB picker without having to mutate the
  // global store for a single-shot operation.
  targetKbId?: string
): Promise<DocActionResponse> => {
  const formData = new FormData()
  formData.append('file', file)

  const headers: Record<string, string> = {
    'Content-Type': 'multipart/form-data',
  }
  if (targetKbId) {
    headers['X-KB-Id'] = targetKbId
  }

  const response = await axiosInstance.post('/documents/upload', formData, {
    headers,
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

/**
 * Parse an RFC 5987 ``Content-Disposition`` header and return the
 * filename the server asked us to save under, or ``null`` if the
 * header is absent / malformed. Prefers the UTF-8 ``filename*=``
 * form — the ASCII ``filename="..."`` fallback typically drops
 * non-Latin characters so a Chinese-named file would arrive as a
 * bare extension otherwise.
 */
const parseContentDispositionFilename = (
  disposition: string | undefined | null
): string | null => {
  if (!disposition) return null
  // RFC 5987 extended parameter: filename*=UTF-8''<url-encoded>
  const utf8Match = disposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1].trim())
    } catch {
      // fall through to ASCII fallback
    }
  }
  const asciiMatch = disposition.match(/filename\s*=\s*"?([^";]+)"?/i)
  return asciiMatch ? asciiMatch[1].trim() : null
}

/**
 * Download a source document from the current workspace's input
 * directory and trigger a browser save dialog.
 *
 * Used by the retrieval citation UI so an operator can click a
 * cited filename to pull the original PDF/markdown/txt. Axios
 * handles the bearer token + ``X-Workspace-Id`` / ``X-KB-Id``
 * header injection; a plain ``<a href>`` would not carry the JWT.
 * The blob is held only long enough to drive the click, then the
 * object URL is revoked so the blob can be GC'd.
 *
 * The server may return the file under a different name than the
 * one requested — specifically, for programmatically-ingested
 * docs with no on-disk source the server reconstructs the text
 * and tags it with a ``.txt`` suffix. Respecting
 * ``Content-Disposition`` keeps that suffix visible to the
 * operator instead of mis-saving text content under a ``.pdf``
 * extension that would confuse downstream viewers.
 */
export const downloadSourceFile = async (name: string): Promise<void> => {
  const response = await axiosInstance.get('/documents/file', {
    params: { name },
    responseType: 'blob',
  })
  const disposition =
    (response.headers?.['content-disposition'] as string | undefined) ??
    (response.headers?.['Content-Disposition'] as string | undefined) ??
    null
  const effectiveName = parseContentDispositionFilename(disposition) || name
  const blobUrl = URL.createObjectURL(response.data as Blob)
  try {
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = effectiveName
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    // Defer revoke a tick so the browser has actually started the
    // download — some engines race the revoke against the click
    // dispatch and cancel the save.
    setTimeout(() => URL.revokeObjectURL(blobUrl), 0)
  }
}

export type MoveDocumentResponse = {
  status: 'moved'
  message: string
  doc_id: string
  source_kb_id: string
  target_kb_id: string
}

export type CurrentUserResponse = {
  user_id: string | null
  username: string | null
  source: string
  is_active: boolean
  role: string
  memberships: Array<{
    workspace_id: string
    kb_id?: string | null
    role: string
  }>
  provider: string
}

export const getCurrentUser = async (): Promise<CurrentUserResponse> => {
  const response = await axiosInstance.get('/auth/me')
  return response.data
}

export type RetrievalHistoryResponse = {
  workspace_id: string
  kb_id: string
  history: unknown[]
  updated_at: string | null
}

export const getRetrievalHistory = async (
  workspaceId: string,
  kbId: string
): Promise<RetrievalHistoryResponse> => {
  const response = await axiosInstance.get<RetrievalHistoryResponse>(
    '/auth/me/retrieval-history',
    { params: { workspace_id: workspaceId, kb_id: kbId } }
  )
  return response.data
}

export const putRetrievalHistory = async (
  workspaceId: string,
  kbId: string,
  history: unknown[]
): Promise<RetrievalHistoryResponse> => {
  const response = await axiosInstance.put<RetrievalHistoryResponse>(
    '/auth/me/retrieval-history',
    { history },
    { params: { workspace_id: workspaceId, kb_id: kbId } }
  )
  return response.data
}

export const clearRetrievalHistory = async (
  workspaceId: string,
  kbId: string
): Promise<{ status: string }> => {
  const response = await axiosInstance.delete<{ status: string }>(
    '/auth/me/retrieval-history',
    { params: { workspace_id: workspaceId, kb_id: kbId } }
  )
  return response.data
}

export const moveDocument = async (
  docId: string,
  targetKbId: string
): Promise<MoveDocumentResponse> => {
  const response = await axiosInstance.post(
    `/documents/${encodeURIComponent(docId)}/move`,
    { target_kb_id: targetKbId }
  )
  return response.data
}

export type CopyDocumentResponse = {
  status: 'copied'
  message: string
  doc_id: string
  source_kb_id: string
  target_kb_id: string
}

// Copy = re-ingest the document into the target KB while leaving the source
// KB copy intact (unlike move, which schedules a background delete from the
// source). Backend gates this on KB_UPLOAD_DOCUMENT.
export const copyDocument = async (
  docId: string,
  targetKbId: string
): Promise<CopyDocumentResponse> => {
  const response = await axiosInstance.post(
    `/documents/${encodeURIComponent(docId)}/copy`,
    { target_kb_id: targetKbId }
  )
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

export type CancelDocumentResponse = {
  status: 'cancelled' | 'cancel_requested' | 'already_final' | 'not_found'
  message: string
  doc_id: string
  previous_status: string | null
}

/**
 * Per-document cancel. Backend picks between synchronous FAILED flip
 * (for pending docs) and a deferred cancel flag the pipeline checks at
 * its next processing checkpoint (for docs currently processing).
 */
export const cancelDocument = async (
  docId: string
): Promise<CancelDocumentResponse> => {
  const response = await axiosInstance.post(
    `/documents/${encodeURIComponent(docId)}/cancel`
  )
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
        auth_mode: 'local'
      };
    }

    // Strict validation of the response data
    if (response.data &&
        typeof response.data === 'object' &&
        'auth_configured' in response.data &&
        typeof response.data.auth_configured === 'boolean') {
      return response.data;
    }

    // If response data is invalid but we got a response, log it
    console.warn('Received invalid auth status response:', response.data);

    // Default to auth configured if response is invalid
    return {
      auth_configured: true,
      auth_mode: 'local'
    };
  } catch (error) {
    // If the request fails, assume authentication is configured
    console.error('Failed to get auth status:', errorMessage(error));
    return {
      auth_configured: true,
      auth_mode: 'local'
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

const getPaginatedDocumentsRequestKey = (request: DocumentsRequest): string => {
  // CRITICAL: the dedup key MUST include the active workspace/KB scope.
  // That scope lives in the X-Workspace-Id / X-KB-Id headers (injected by
  // the request interceptor from useKBStore), NOT in the request body. Keying
  // on the body alone meant a paginated for (workspace B, kb B) shared a key
  // with a still-in-flight request for (workspace A, kb A) that had the same
  // page/filter — so on a workspace/KB switch the new request DEDUP-subscribed
  // to the old, wrong-scope request and blocked until it finished (observed as
  // a ~20s+ idle gap before the documents load). Scoping the key makes the
  // switch fire a fresh request immediately.
  const { activeWorkspaceId, activeKbId } = useKBStore.getState()
  return JSON.stringify({
    request,
    ws: activeWorkspaceId ?? null,
    kb: activeKbId ?? null,
  })
}

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
 * Get counts of documents by status.
 *
 * By default the axios interceptor injects the *active* workspace/KB
 * headers from the Zustand store. Pages that need to read counts for a
 * KB other than the active one (e.g. the KB overview card) can pass an
 * explicit ``kbId`` override — we stamp ``X-KB-Id`` on that single
 * request without mutating global state.
 */
export const getDocumentStatusCounts = async (
  kbId?: string,
  allKbs?: boolean
): Promise<StatusCountsResponse> => {
  const headers: Record<string, string> = {}
  // ``all_kbs`` fans the count out across every KB in the current
  // workspace — the explicit ``X-KB-Id`` override would pin it back to
  // a single shard, so the two modes are mutually exclusive.
  if (allKbs) {
    const response = await axiosInstance.get('/documents/status_counts', {
      params: { all_kbs: true },
    })
    return response.data
  }
  if (kbId) {
    headers['X-KB-Id'] = kbId
  }
  const response = await axiosInstance.get('/documents/status_counts', { headers })
  return response.data
}
