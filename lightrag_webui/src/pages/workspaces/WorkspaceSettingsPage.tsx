/**
 * Legacy route shim.
 *
 * WorkspaceSettings used to be its own page. The "rename / describe /
 * delete / see KBs inside this workspace" surface is now merged into
 * the single ``/workspaces`` management screen, so this file just
 * re-exports that page — every inbound link (bookmarks, old sidebar
 * entries, the ``/workspaces/:workspaceId/settings`` URL that the
 * router still registers) keeps working, and the unified page reads
 * ``useParams().workspaceId`` to auto-select the right tab.
 */
export { default } from './WorkspaceListPage'
