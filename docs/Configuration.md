# Configuration reference

`env.example` ships only the knobs a first-time user must touch. Every
other setting is documented here — copy the key into your `.env` if you
need to override the default.

---

## Contents

- [Server tuning](#server-tuning)
- [Auth & tokens](#auth--tokens)
- [Remote auth providers](#remote-auth-providers)
- [LLM providers](#llm-providers)
- [Embedding providers](#embedding-providers)
- [Reranker providers](#reranker-providers)
- [Query tuning](#query-tuning)
- [Document processing](#document-processing)
- [Concurrency](#concurrency)
- [Storage backends](#storage-backends)
- [Observability](#observability)
- [Evaluation](#evaluation)

---

## Server tuning

```dotenv
# gunicorn workers + per-worker request timeout (also used as default
# LLM request timeout when LLM_TIMEOUT is not set)
WORKERS=2
TIMEOUT=150
CORS_ORIGINS=http://localhost:3000,http://localhost:8080

# Optional SSL (generated docker compose mounts certs at /app/data/certs)
SSL=true
SSL_CERTFILE=/path/to/cert.pem
SSL_KEYFILE=/path/to/key.pem

# Storage locations (default: ./inputs, ./rag_storage)
INPUT_DIR=/abs/path/to/inputs
WORKING_DIR=/abs/path/to/rag_storage
TIKTOKEN_CACHE_DIR=/app/data/tiktoken      # for offline deployment

# Ollama-compatible server name/tag exposed to /api/chat
OLLAMA_EMULATING_MODEL_NAME=lightrag
OLLAMA_EMULATING_MODEL_TAG=latest

# Max nodes returned by graph retrieval
MAX_GRAPH_NODES=1000

# Logging
LOG_LEVEL=INFO
VERBOSE=False
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
LOG_DIR=/path/to/log/directory
LIGHTRAG_PERFORMANCE_TIMING_LOGS=false
```

---

## Auth & tokens

```dotenv
# Legacy env-file accounts (prefer the DB-backed directory instead —
# users you create via /auth/register or the Members page persist in
# sqlite and can be edited from the UI).
AUTH_ACCOUNTS='admin:admin123,user1:{bcrypt}$2b$12$S8Yu...'

# JWT tuning
JWT_ALGORITHM=HS256
TOKEN_EXPIRE_HOURS=48
GUEST_TOKEN_EXPIRE_HOURS=24

# Sliding-window token auto-renewal. When enabled, an access token is
# refreshed when its remaining lifetime drops below
# TOKEN_RENEW_THRESHOLD × original lifetime. The following endpoints
# are intentionally skipped (too hot to rotate on every call):
#   /health, /documents/paginated, /documents/pipeline_status
# Rate limit: minimum 60s between renewals for the same user.
TOKEN_AUTO_RENEW=true
TOKEN_RENEW_THRESHOLD=0.5

# Optional API-key whitelist (paths that do not require X-API-Key).
# Supports exact matches and `/prefix/*`.
WHITELIST_PATHS=/health,/api/*

# Platform V2 switches. The bootstrap script sets these automatically
# — only touch them if you are driving deployment without bootstrap.
USE_DB_AUTH=true
DB_URL=sqlite+aiosqlite:///./lightrag_auth.db
ENABLE_KB_ISOLATION=true

# Identifier tuning (rarely touched). DEFAULT_WORKSPACE_ID falls back
# to WORKSPACE when unset. KB_SEPARATOR must stay filesystem-safe.
DEFAULT_WORKSPACE_ID=default
DEFAULT_KB_ID=default
KB_SEPARATOR=__
```

---

## Remote auth providers

`LIGHTRAG_AUTH_PROVIDER` selects which auth backend verifies
credentials. Default is `local` — the DB-backed user store the
bootstrap script sets up. Set to `ldap` to delegate credential
verification to an LDAP / Active Directory server while keeping every
other part of the stack (workspaces, KB registry, audit log, federated
query, the `no_access` isolation sentinel) unchanged.

All remote providers still require `USE_DB_AUTH=true` — workspace
memberships and refresh tokens live in the local DB regardless of
where the password check happens.

```dotenv
LIGHTRAG_AUTH_PROVIDER=local   # or: ldap
```

### LDAP / Active Directory

Install the optional extra:

```bash
uv sync --extra ldap
```

Enable and configure:

```dotenv
LIGHTRAG_AUTH_PROVIDER=ldap

# Connection
LDAP_SERVER_URL=ldaps://ldap.example.com:636
LDAP_USE_START_TLS=false        # true for ldap://<host> with StartTLS

# Service account used to look up user DNs
LDAP_BIND_DN=cn=lightrag-svc,ou=service,dc=example,dc=com
LDAP_BIND_PASSWORD=your-service-account-password

# User search
LDAP_USER_SEARCH_BASE=ou=people,dc=example,dc=com
LDAP_USER_FILTER=(&(objectClass=inetOrgPerson)(uid={username}))
LDAP_USER_ATTR_USERNAME=uid
# For Active Directory, the common pattern is:
#   LDAP_USER_FILTER=(&(objectClass=user)(sAMAccountName={username}))
#   LDAP_USER_ATTR_USERNAME=sAMAccountName

# Optional group → role mapping. Left unset ⇒ every LDAP user gets
# LDAP_DEFAULT_ROLE (viewer unless overridden).
LDAP_GROUP_SEARCH_BASE=ou=groups,dc=example,dc=com
LDAP_GROUP_FILTER=(&(objectClass=groupOfNames)(member={user_dn}))
LDAP_GROUP_ATTR=cn
LDAP_ROLE_MAPPING=lightrag-admins:admin,lightrag-editors:editor,lightrag-viewers:viewer
LDAP_DEFAULT_ROLE=viewer
```

Runtime behaviour:

- **First login** — the user is shadow-created in the local
  `users` table with `source="ldap"` and gets a personal
  uuid4-hex workspace where they're `owner` (matches the
  self-registration flow so every auth path yields the same
  per-user isolation guarantee).
- **Subsequent logins** — the LDAP bind still happens every time
  (LightRAG never stores the LDAP password). Group lookup re-runs,
  so role bumps in LDAP apply on next sign-in without any sync job.
- **Password changes** — `/auth/change-password` returns 501. Users
  rotate passwords through the upstream directory, not LightRAG.
- **User management** — `/auth/users` CRUD returns 501. Accounts come
  from LDAP; the Members page becomes a read-only view of who has
  logged in so far.
- **Self-registration** — disabled regardless of
  `LIGHTRAG_ALLOW_SELF_REGISTRATION`. The 注册 tab does not render.

Filter injection is blocked at the edge — user-supplied `{username}`
values are RFC-4515-escaped before being spliced into
`LDAP_USER_FILTER`, so the usual `alice)(uid=*)` payloads can't short-
circuit the search.

### Adding another remote provider

The pattern is a single file + one registration line. Subclass
`AuthProvider` in `lightrag/api/auth_provider.py`, implement at least
`get_auth_status` and `authenticate_password`, then register under a
new name in `_select_auth_provider` inside
`lightrag/api/dependencies.py`. `LDAPAuthProvider` at
`lightrag/api/auth_providers/ldap_provider.py` is the canonical
reference — ~300 lines, stateless aside from parsed env config.

OIDC-style redirect flows need an extra callback route but the
`AuthenticatedPrincipal` contract is the same: set `role`,
`user_id`, and optional `memberships` to whatever the upstream
returns and every downstream surface (token minting, audit emits,
workspace permissions, the DB shadow-user trick) keeps working.

---

## LLM providers

`env.example` ships Alibaba DashScope compatible-mode by default
(validated end-to-end with Qwen3.6-plus + text-embedding-v4). Below
are the equivalent blocks for other bindings — replace the LLM_* /
EMBEDDING_* lines in your `.env`.

### OpenAI

```dotenv
LLM_BINDING=openai
LLM_BINDING_HOST=https://api.openai.com/v1
LLM_BINDING_API_KEY=your_api_key
LLM_MODEL=gpt-5-mini

EMBEDDING_BINDING=openai
EMBEDDING_BINDING_HOST=https://api.openai.com/v1
EMBEDDING_BINDING_API_KEY=your_api_key
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_DIM=3072
EMBEDDING_SEND_DIM=false          # OpenAI default; set true only to truncate dim
```

### Alibaba DashScope (compatible mode)

Gotchas baked into the template defaults:

- `text-embedding-v4` caps at **10 rows per embed request** — do not
  raise `EMBEDDING_BATCH_NUM` past 10.
- DashScope returns 1024-d vectors unless you explicitly set
  `EMBEDDING_SEND_DIM=true` + `EMBEDDING_DIM=2048`.
- qwen3.x models are reasoning models. Keep thinking OFF for indexing
  (entity extraction + summarization + keyword extraction) so each
  call does not burn ~1000 reasoning tokens, and turn it ON for query
  answer generation via `QUERY_ENABLE_THINKING=true`.
- qwen3.6-plus tier allows up to 50 concurrent requests; `MAX_ASYNC=16`
  is a safe headroom.
- Multimodal embedding (`tongyi-embedding-vision-*`) must use the
  **native** DashScope API, not compatible-mode — LightRAG's tongyi
  adapter routes through the `dashscope` SDK.

### Azure OpenAI

```dotenv
LLM_BINDING=azure_openai
LLM_BINDING_HOST=https://xxxx.openai.azure.com/
LLM_BINDING_API_KEY=your_api_key
LLM_MODEL=my-gpt-mini-deployment   # or set AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION=2024-08-01-preview
```

### OpenRouter

```dotenv
LLM_BINDING=openai
LLM_BINDING_HOST=https://openrouter.ai/api/v1
LLM_BINDING_API_KEY=your_api_key
LLM_MODEL=google/gemini-2.5-flash
```

### Gemini (AI Studio)

```dotenv
LLM_BINDING=gemini
LLM_BINDING_API_KEY=your_gemini_api_key
LLM_BINDING_HOST=https://generativelanguage.googleapis.com
LLM_MODEL=gemini-flash-latest

# Tuning
GEMINI_LLM_MAX_OUTPUT_TOKENS=9000
GEMINI_LLM_TEMPERATURE=0.7
GEMINI_LLM_THINKING_CONFIG='{"thinking_budget": -1, "include_thoughts": true}'
```

### Vertex AI

```dotenv
LLM_BINDING=gemini
LLM_BINDING_HOST=DEFAULT_GEMINI_ENDPOINT   # auto-pick by project+location
LLM_MODEL=gemini-2.5-flash
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
```

### Ollama

Ollama's default context window is 8k; LightRAG needs 32k+, so
`OLLAMA_LLM_NUM_CTX` is mandatory.

```dotenv
LLM_BINDING=ollama
LLM_BINDING_HOST=http://localhost:11434
LLM_MODEL=qwen3.5:9b
OLLAMA_LLM_NUM_CTX=32768
OLLAMA_LLM_NUM_PREDICT=9000
OLLAMA_LLM_TEMPERATURE=0.85
OLLAMA_LLM_STOP='["</s>", "<|EOT|>"]'
```

### AWS Bedrock

Credentials come from the AWS credential chain, not `LLM_BINDING_API_KEY`.

```dotenv
LLM_BINDING=aws_bedrock
LLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...       # optional
AWS_REGION=us-east-1
BEDROCK_LLM_TEMPERATURE=1.0
```

### OpenAI-compatible tuning

```dotenv
# Bumps temperature to escape pathological loops on some local models.
OPENAI_LLM_TEMPERATURE=0.9

# Cap generation length. Keep below LLM_TIMEOUT × tokens-per-second.
OPENAI_LLM_MAX_TOKENS=9000
OPENAI_LLM_MAX_COMPLETION_TOKENS=9000    # o1-mini + newer use this

# OpenAI-specific: reasoning effort hint.
OPENAI_LLM_REASONING_EFFORT=minimal

# Pass-through body for OpenRouter / vLLM etc.
OPENAI_LLM_EXTRA_BODY='{"reasoning": {"enabled": false}}'
OPENAI_LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'

LLM_TIMEOUT=180
```

---

## Embedding providers

Embedding config is frozen at first ingest — swapping model or dim
later requires wiping vector storage. `env.example` ships OpenAI.

```dotenv
# OpenAI default — dynamic dimension off
EMBEDDING_SEND_DIM=false
EMBEDDING_TOKEN_LIMIT=8192
EMBEDDING_TIMEOUT=30

# Azure OpenAI
EMBEDDING_BINDING=azure_openai
EMBEDDING_BINDING_HOST=https://xxxx.openai.azure.com/
EMBEDDING_API_KEY=your_api_key
EMBEDDING_MODEL=my-text-embedding-3-large-deployment
EMBEDDING_DIM=3072
AZURE_EMBEDDING_API_VERSION=2024-08-01-preview

# Gemini (always set EMBEDDING_SEND_DIM=true)
EMBEDDING_BINDING=gemini
EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIM=1536
EMBEDDING_TOKEN_LIMIT=2048
EMBEDDING_BINDING_HOST=https://generativelanguage.googleapis.com
EMBEDDING_BINDING_API_KEY=your_api_key
EMBEDDING_SEND_DIM=true

# Ollama
EMBEDDING_BINDING=ollama
EMBEDDING_BINDING_HOST=http://localhost:11434
EMBEDDING_BINDING_API_KEY=your_api_key
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_DIM=2560
OLLAMA_EMBEDDING_NUM_CTX=8192

# AWS Bedrock (uses AWS credential chain)
EMBEDDING_BINDING=aws_bedrock
EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
EMBEDDING_DIM=1024

# Jina
EMBEDDING_BINDING=jina
EMBEDDING_BINDING_HOST=https://api.jina.ai/v1/embeddings
EMBEDDING_MODEL=jina-embeddings-v4
EMBEDDING_DIM=2048
EMBEDDING_BINDING_API_KEY=your_api_key
```

---

## Reranker providers

Significantly improves retrieval quality. Query mode `mix` is the
intended pair when a reranker is on.

```dotenv
# Cohere
RERANK_BINDING=cohere
RERANK_MODEL=rerank-v3.5
RERANK_BINDING_HOST=https://api.cohere.com/v2/rerank
RERANK_BINDING_API_KEY=your_rerank_api_key
# ColBERT-style models with token limits benefit from chunking:
RERANK_ENABLE_CHUNKING=true
RERANK_MAX_TOKENS_PER_DOC=480

# Aliyun Dashscope
RERANK_BINDING=aliyun
RERANK_MODEL=gte-rerank-v2
RERANK_BINDING_HOST=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_BINDING_API_KEY=your_rerank_api_key

# Jina
RERANK_BINDING=jina
RERANK_MODEL=jina-reranker-v2-base-multilingual
RERANK_BINDING_HOST=https://api.jina.ai/v1/rerank
RERANK_BINDING_API_KEY=your_rerank_api_key

# Filter chunks under this rerank score (0.0 = keep all)
MIN_RERANK_SCORE=0.0
RERANK_BY_DEFAULT=True
```

### Local vLLM (CPU or GPU)

```dotenv
LIGHTRAG_SETUP_EMBEDDING_PROVIDER=vllm
LIGHTRAG_SETUP_RERANK_PROVIDER=vllm

VLLM_EMBED_MODEL=BAAI/bge-m3
VLLM_EMBED_PORT=8001
VLLM_EMBED_DEVICE=cpu
VLLM_EMBED_API_KEY=           # synced to EMBEDDING_BINDING_API_KEY; auto-gen if blank

VLLM_RERANK_MODEL=BAAI/bge-reranker-v2-m3
VLLM_RERANK_PORT=8000
VLLM_RERANK_DEVICE=cuda
VLLM_RERANK_API_KEY=

# 1 = CPU mode. Unset for GPU.
VLLM_USE_CPU=1
CUDA_VISIBLE_DEVICES=-1
NVIDIA_VISIBLE_DEVICES=0
```

---

## Query tuning

Context-budget math that goes to the LLM:

    MAX_ENTITY_TOKENS + MAX_RELATION_TOKENS < MAX_TOTAL_TOKENS
    Chunk_Tokens = MAX_TOTAL_TOKENS - Actual_Entity_Tokens - Actual_Relation_Tokens

```dotenv
ENABLE_LLM_CACHE=true           # LLM response cache (non-streaming only)
COSINE_THRESHOLD=0.2

TOP_K=40                        # entities/relations retrieved from KG
CHUNK_TOP_K=20                  # chunks for naive vector search

MAX_ENTITY_TOKENS=6000
MAX_RELATION_TOKENS=8000
MAX_TOTAL_TOKENS=30000

# KG chunk picker strategy:
#   VECTOR — pick by vector similarity (closer to naive retrieval)
#   WEIGHT — pick by entity/chunk weight (more KG-specific)
# Less important when a reranker is active.
KG_CHUNK_PICK_METHOD=VECTOR

# Chunks per source entity/relation. Higher = slower rerank.
RELATED_CHUNK_NUMBER=5
```

---

## Document processing

```dotenv
# Cache LLM responses during entity/relation extraction too.
ENABLE_LLM_CACHE_FOR_EXTRACT=true

# Output language for the graph summary (English, Chinese, ...).
SUMMARY_LANGUAGE=English

# 100 MB default. 0 disables the limit. If behind Nginx, also set
# client_max_body_size.
MAX_UPLOAD_SIZE=104857600

# Entity types the extractor is allowed to tag.
ENTITY_TYPES='["Person","Creature","Organization","Location","Event","Concept","Method","Content","Data","Artifact","NaturalObject"]'

# Chunking
CHUNK_SIZE=1200
CHUNK_OVERLAP_SIZE=100

# Summary knobs
FORCE_LLM_SUMMARY_ON_MERGE=8        # trigger summary when merging ≥ N fragments
SUMMARY_MAX_TOKENS=1200             # max description size that triggers a summary
SUMMARY_LENGTH_RECOMMENDED=600
SUMMARY_CONTEXT_SIZE=12000
MAX_EXTRACT_INPUT_TOKENS=20480

# Cap how many source chunk ids stick to one entity/relation.
# FIFO = evict oldest once full. KEEP = keep oldest (cheaper, fewer merges).
MAX_SOURCE_IDS_PER_ENTITY=300
MAX_SOURCE_IDS_PER_RELATION=300
SOURCE_IDS_LIMIT_METHOD=FIFO

# Capped file_path field (display-only).
MAX_FILE_PATHS=100

# For encrypted PDFs.
PDF_DECRYPT_PASSWORD=your_pdf_password
```

---

## Concurrency

```dotenv
MAX_ASYNC=4                      # concurrent LLM requests (query + indexing)
MAX_PARALLEL_INSERT=2            # parallel documents (2–10; MAX_ASYNC/3 is a good start)
EMBEDDING_FUNC_MAX_ASYNC=8       # parallel embedding calls
EMBEDDING_BATCH_NUM=10           # chunks per embedding request
```

---

## Storage backends

LightRAG has four independent storages (KV, vector, graph, doc-status)
that can each be backed by a different system. Default ships file-based
(Json + NanoVectorDB + NetworkX).

### Picking a full stack

```dotenv
# Production Redis + Qdrant + Memgraph
LIGHTRAG_KV_STORAGE=RedisKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=RedisDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=QdrantVectorDBStorage
LIGHTRAG_GRAPH_STORAGE=MemgraphStorage

# All-in-one Postgres (pgvector + AGE)
LIGHTRAG_KV_STORAGE=PGKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
LIGHTRAG_GRAPH_STORAGE=PGGraphStorage
LIGHTRAG_VECTOR_STORAGE=PGVectorStorage

# All-in-one OpenSearch
LIGHTRAG_KV_STORAGE=OpenSearchKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=OpenSearchDocStatusStorage
LIGHTRAG_GRAPH_STORAGE=OpenSearchGraphStorage
LIGHTRAG_VECTOR_STORAGE=OpenSearchVectorDBStorage

# All-in-one MongoDB (vector requires Atlas or Atlas-compatible)
LIGHTRAG_KV_STORAGE=MongoKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=MongoDocStatusStorage
LIGHTRAG_GRAPH_STORAGE=MongoGraphStorage
LIGHTRAG_VECTOR_STORAGE=MongoVectorDBStorage
```

### PostgreSQL

```dotenv
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=your_username
POSTGRES_PASSWORD='your_password'
POSTGRES_DATABASE=rag
POSTGRES_MAX_CONNECTIONS=25

# Vector support (disable when using a separate vector backend)
POSTGRES_ENABLE_VECTOR=true

# Vector index: HNSW, HNSW_HALFVEC (needs pgvector ≥ 0.7.0 for
# embeddings ≥ 2000-dim), IVFFlat, VCHORDRQ.
POSTGRES_VECTOR_INDEX_TYPE=HNSW
POSTGRES_HNSW_M=16
POSTGRES_HNSW_EF=200
POSTGRES_IVFFLAT_LISTS=100
POSTGRES_VCHORDRQ_BUILD_OPTIONS=
POSTGRES_VCHORDRQ_PROBES=
POSTGRES_VCHORDRQ_EPSILON=1.9

# Connection retry (defaults tuned for ~30s HA failover).
POSTGRES_CONNECTION_RETRIES=10
POSTGRES_CONNECTION_RETRY_BACKOFF=3.0
POSTGRES_CONNECTION_RETRY_BACKOFF_MAX=30.0
POSTGRES_POOL_CLOSE_TIMEOUT=5.0

# TLS
POSTGRES_SSL_MODE=require
POSTGRES_SSL_CERT=/path/client-cert.pem
POSTGRES_SSL_KEY=/path/client-key.pem
POSTGRES_SSL_ROOT_CERT=/path/ca-cert.pem
POSTGRES_SSL_CRL=/path/crl.pem

# Supabase / connection-string extras.
POSTGRES_SERVER_SETTINGS='options=reference%3D[project-ref]'
POSTGRES_STATEMENT_CACHE_SIZE=100
```

### Neo4j

```dotenv
NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD='your_password'
NEO4J_DATABASE=neo4j
NEO4J_MAX_CONNECTION_POOL_SIZE=100
NEO4J_CONNECTION_TIMEOUT=30
NEO4J_CONNECTION_ACQUISITION_TIMEOUT=30
NEO4J_MAX_TRANSACTION_RETRY_TIME=30
NEO4J_MAX_CONNECTION_LIFETIME=300
NEO4J_LIVENESS_CHECK_TIMEOUT=30
NEO4J_KEEP_ALIVE=true
```

### MongoDB

```dotenv
# Vector storage needs Atlas Search / Vector Search.
MONGO_URI=mongodb://localhost:27017/
MONGO_DATABASE=LightRAG
```

### OpenSearch

```dotenv
OPENSEARCH_HOSTS=localhost:9200     # comma-separated host:port; no scheme
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD=LightRAG2026_!@
OPENSEARCH_USE_SSL=true
OPENSEARCH_VERIFY_CERTS=false
OPENSEARCH_TIMEOUT=30
OPENSEARCH_MAX_RETRIES=3

# 3-AZ Amazon OpenSearch: bump replicas to 2.
OPENSEARCH_NUMBER_OF_SHARDS=1
OPENSEARCH_NUMBER_OF_REPLICAS=0

# k-NN (HNSW) tuning
OPENSEARCH_KNN_EF_CONSTRUCTION=200
OPENSEARCH_KNN_M=16
OPENSEARCH_KNN_EF_SEARCH=100

# PPL graph traversal on the server side.
OPENSEARCH_USE_PPL_GRAPHLOOKUP=true
```

### Milvus

Detailed guide: [docs/MilvusConfigurationGuide.md](MilvusConfigurationGuide.md).

```dotenv
MILVUS_URI=http://localhost:19530
MILVUS_DB_NAME=lightrag
MILVUS_DEVICE=cpu
MILVUS_USER=root
MILVUS_PASSWORD=your_password
MILVUS_TOKEN=your_token

# Bundled Milvus docker stack needs MinIO credentials.
MINIO_ACCESS_KEY_ID=minioadmin
MINIO_SECRET_ACCESS_KEY=minioadmin

# Index: AUTOINDEX (default), HNSW, HNSW_SQ, HNSW_PQ, IVF_FLAT, IVF_SQ8, DISKANN
MILVUS_INDEX_TYPE=AUTOINDEX
MILVUS_METRIC_TYPE=COSINE            # COSINE | L2 | IP

MILVUS_HNSW_M=16
MILVUS_HNSW_EF_CONSTRUCTION=360
MILVUS_HNSW_EF=200

# HNSW_SQ (Milvus 2.6.8+)
MILVUS_HNSW_SQ_TYPE=SQ8              # SQ4U | SQ6 | SQ8 | BF16 | FP16
MILVUS_HNSW_SQ_REFINE=false
MILVUS_HNSW_SQ_REFINE_TYPE=FP32
MILVUS_HNSW_SQ_REFINE_K=10

# IVF_FLAT / IVF_SQ8
MILVUS_IVF_NLIST=1024
MILVUS_IVF_NPROBE=16
```

### Qdrant

```dotenv
QDRANT_URL=http://localhost:6333
QDRANT_DEVICE=cpu
QDRANT_API_KEY=your-api-key

# Batching for very large upserts. Defaults stay below common 32MB
# gateway limits.
QDRANT_UPSERT_MAX_PAYLOAD_BYTES=16777216
QDRANT_UPSERT_MAX_POINTS_PER_BATCH=128
```

### Redis

```dotenv
REDIS_URI=redis://localhost:6379
REDIS_SOCKET_TIMEOUT=30
REDIS_CONNECT_TIMEOUT=10
REDIS_MAX_CONNECTIONS=100
REDIS_RETRY_ATTEMPTS=3
```

### Memgraph

```dotenv
MEMGRAPH_URI=bolt://localhost:7687
MEMGRAPH_USERNAME=
MEMGRAPH_PASSWORD=
MEMGRAPH_DATABASE=memgraph
```

### Workspace override

Rarely used — forces every storage backend to read/write under a fixed
workspace prefix regardless of the request. Exists for backwards
compatibility with pre-Platform-V2 deployments.

```dotenv
WORKSPACE=legacy_workspace_name
```

---

## Observability

Langfuse tracing for OpenAI-compatible LLM calls. Install extras:
`pip install lightrag-hku[observability]`.

```dotenv
LANGFUSE_SECRET_KEY=''
LANGFUSE_PUBLIC_KEY=''
LANGFUSE_HOST='https://cloud.langfuse.com'
LANGFUSE_ENABLE_TRACE=true
```

---

## Evaluation

RAGAS evaluation harness. **Both endpoints must be OpenAI-compatible.**

```dotenv
# LLM used to grade answers.
EVAL_LLM_MODEL=gpt-4o-mini
EVAL_LLM_BINDING_API_KEY=your_api_key        # falls back to OPENAI_API_KEY
EVAL_LLM_BINDING_HOST=https://api.openai.com/v1

# Embedding used for eval reference alignment.
EVAL_EMBEDDING_MODEL=text-embedding-3-large
EVAL_EMBEDDING_BINDING_API_KEY=your_api_key  # falls back: EVAL_LLM_BINDING_API_KEY → OPENAI_API_KEY
EVAL_EMBEDDING_BINDING_HOST=https://api.openai.com/v1

# Concurrency + retry
EVAL_MAX_CONCURRENT=2
EVAL_QUERY_TOP_K=10
EVAL_LLM_MAX_RETRIES=5
EVAL_LLM_TIMEOUT=180
```
