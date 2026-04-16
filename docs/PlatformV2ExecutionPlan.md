# LightRAG Platform V2 Execution Plan

## Goal

Upgrade LightRAG from a single-instance tooling-style WebUI into a platform product with:

- a route-based application shell
- a stronger brand and design system
- account, membership, and permission management
- workspace and knowledge-base level isolation
- a migration path from the current single-workspace runtime

## Non-Goals for Phase 1

- no full enterprise SSO in the first phase
- no hard physical database-per-tenant split in the first phase
- no destructive replacement of the current runtime without migration compatibility

## Architectural Direction

### Product Model

The target platform model is:

- User
- Workspace
- KnowledgeBase
- Membership
- Role / Permission
- API Key / Service Account
- Audit Log

### Runtime Model

The current backend binds most routes to a startup-time `LightRAG(workspace=args.workspace)` instance.
V2 should move toward request-scoped context resolution:

1. authenticate actor
2. resolve workspace
3. resolve knowledge base
4. check permission
5. fetch/build current RAG runtime handle
6. execute documents/query/graph action against the selected knowledge base

### Storage Isolation Model

Phase 1 should use logical isolation with namespaced storage identifiers:

- `{workspace_id}:{kb_id}:{namespace}`

This preserves compatibility with the current LightRAG storage abstractions while enabling multiple knowledge bases per workspace.

## Workstreams

### WS0 — Platform Architecture and Governance

Owned by main rollout.

Deliverables:

- target architecture
- workstream boundaries
- dependency order
- migration strategy
- orchestration rules for delegated agents

### WS1 — Frontend App Shell and Information Architecture

Target outcome:

- route-based application shell
- workspace switcher
- knowledge-base switcher
- page-level navigation instead of one tab-only shell

Suggested routes:

- `/login`
- `/app/workspaces`
- `/app/workspaces/:workspaceId`
- `/app/workspaces/:workspaceId/kb/:kbId/overview`
- `/app/workspaces/:workspaceId/kb/:kbId/documents`
- `/app/workspaces/:workspaceId/kb/:kbId/retrieval`
- `/app/workspaces/:workspaceId/kb/:kbId/graph`
- `/app/workspaces/:workspaceId/kb/:kbId/settings`
- `/app/workspaces/:workspaceId/members`

### WS2 — Brand and Design System

Target outcome:

- semantic design tokens
- stronger visual hierarchy
- redesigned login experience
- improved top navigation
- consistent empty/loading/error states

Constraint:

- visual work only
- no backend architecture or data-model ownership in this stream

### WS3 — Account, Membership, and RBAC

Target outcome:

- DB-backed users instead of env-only account strings
- workspace / knowledge-base scoped membership
- role-based authorization
- centralized permission checks
- safer token lifecycle

Suggested minimum role set:

- owner
- admin
- editor
- viewer

Suggested minimum action set:

- `workspace:view`
- `workspace:update`
- `workspace:invite_member`
- `kb:view`
- `kb:query`
- `kb:upload_document`
- `kb:delete_document`
- `kb:edit_graph`
- `kb:manage_settings`
- `kb:manage_permissions`

### WS4 — Knowledge Base Resource Model and Runtime Isolation

Target outcome:

- knowledge base as first-class resource
- metadata layer for KB configuration
- request-scoped runtime resolution
- namespace upgrade from workspace-only to workspace-plus-kb

Recommended new backend abstractions:

- `KnowledgeBaseRegistry`
- `RagFactory`
- `RagContextResolver`
- `get_current_workspace()`
- `get_current_kb()`
- `get_current_rag()`

### WS5 — Migration and Validation

Target outcome:

- migration of current data to default workspace + default knowledge base
- compatibility strategy for existing deployments
- validation plan for frontend, auth, and runtime isolation

## Suggested Phase Plan

### Phase A — Planning and Shell

- freeze architecture and workstream boundaries
- land new frontend shell structure
- land brand token system

### Phase B — Identity and Authorization

- DB-backed user/membership model
- JWT/refresh strategy
- permission dependency layer

### Phase C — KB Runtime Isolation

- KB metadata model
- request-scoped runtime resolution
- workspace + KB aware APIs

### Phase D — Business Screen Refactor

- documents
- retrieval
- graph
- members / settings

### Phase E — Migration and Hardening

- migration tooling
- regression coverage
- rollout notes
- operator runbook: `docs/platform-v2/ws5-migration-validation-rollout.md`

## Delegation Rules for This Initiative

### Codex Delegates

Use Codex delegates for:

- task decomposition
- execution sequencing
- work-package definitions
- migration/checklist breakdown

Do not assign Codex delegates the top-level product architecture ownership for this initiative; that stays in the main rollout.

### Claude Delegates

Use Claude delegates for:

- concrete work units with clear scope
- implementation-ready specs
- page/module level execution artifacts
- detailed checklists or code units with isolated ownership

### Gemini Delegates

Use Gemini only for:

- frontend beautification
- visual polish
- design token suggestions
- login/header/page aesthetic improvements

Do not assign Gemini any architecture, data-model, tenancy, RBAC, or storage-isolation ownership.

## Initial Task Board

1. finalize architecture master plan
2. split workstreams into execution packages
3. generate concrete unit tasks for frontend shell, RBAC, KB runtime, migration
4. start visual improvement work on login/header/design tokens
5. consolidate outputs into a single implementation roadmap
