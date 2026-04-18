import { useEffect, useMemo } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { BriefcaseBusinessIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { appRoutes, defaultWorkspaceId } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/state'
import { useWorkspaceDirectoryStore } from '@/stores/workspaceDirectory'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/Select'

interface WorkspaceSwitcherProps {
  className?: string
}

/**
 * Top-bar workspace switcher — dropdown, not a link.
 *
 * Shows the current workspace's **name** (falling back to the id while
 * the directory is still loading) and lists every workspace the user is
 * a member of. Picking one navigates in-place to the same sub-page
 * (documents / retrieval / graph / …) under the new workspace so the
 * user stays on task instead of getting dumped on the management page.
 */
export default function WorkspaceSwitcher({ className }: WorkspaceSwitcherProps) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { workspaceId } = useParams()
  const { memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)

  const { byId, ensureLoaded, nameFor } = useWorkspaceDirectoryStore((s) => ({
    byId: s.byId,
    ensureLoaded: s.ensureLoaded,
    nameFor: s.nameFor,
  }))

  useEffect(() => {
    void ensureLoaded()
  }, [ensureLoaded])

  // Union of: default workspace, every workspace the user holds a claim
  // in, and whatever directory record we already cached. Deduped by id
  // and sorted so the default sticks to the top.
  const options = useMemo(() => {
    const seen = new Set<string>()
    const ids: string[] = []
    const push = (id: string) => {
      if (!id || seen.has(id)) return
      seen.add(id)
      ids.push(id)
    }
    push(defaultWorkspaceId)
    for (const claim of memberships) {
      if (claim.workspace_id) push(claim.workspace_id)
    }
    for (const id of Object.keys(byId)) push(id)
    if (currentWorkspaceId) push(currentWorkspaceId)
    return ids
  }, [memberships, byId, currentWorkspaceId])

  // Map /app/workspaces/<current>/<rest> → /app/workspaces/<next>/<rest>
  // so clicking in the switcher keeps the operator on the same surface.
  // Falls back to the workspace root when we can't detect a sub-path
  // (e.g. mounted at /app or /app/workspaces itself).
  const handleChange = (nextWorkspaceId: string) => {
    if (nextWorkspaceId === currentWorkspaceId) return
    const match = pathname.match(/^\/app\/workspaces\/[^/]+(\/.*)?$/)
    const rest = match?.[1] ?? ''
    if (rest) {
      navigate(`/app/workspaces/${nextWorkspaceId}${rest}`)
    } else {
      navigate(appRoutes.workspace(nextWorkspaceId))
    }
  }

  return (
    <Select value={currentWorkspaceId} onValueChange={handleChange}>
      <SelectTrigger
        className={cn(
          'motion-standard group flex h-auto min-w-0 items-center gap-2 rounded-full border border-border/60 bg-background/40 px-3 py-1.5 text-sm shadow-none transition-colors hover:border-emerald-500/40 hover:bg-emerald-500/[0.06] focus:ring-2 focus:ring-emerald-500/20',
          className
        )}
      >
        <BriefcaseBusinessIcon
          className="size-4 shrink-0 text-emerald-600 dark:text-emerald-300"
          aria-hidden="true"
        />
        <span className="flex min-w-0 items-baseline gap-1.5">
          <span className="truncate text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
            {t('header.workspace')}
          </span>
          <span className="truncate font-medium text-foreground">
            {nameFor(currentWorkspaceId)}
          </span>
        </span>
      </SelectTrigger>
      <SelectContent>
        {options.map((id) => {
          const record = byId[id]
          const name = record?.name || id
          return (
            <SelectItem key={id} value={id}>
              <div className="flex min-w-0 flex-col">
                <span className="truncate font-medium">{name}</span>
                {name !== id && (
                  <span className="truncate text-[11px] text-muted-foreground">
                    {id}
                  </span>
                )}
              </div>
            </SelectItem>
          )
        })}
      </SelectContent>
    </Select>
  )
}
