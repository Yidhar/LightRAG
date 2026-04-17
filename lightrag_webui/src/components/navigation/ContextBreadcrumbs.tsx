import { ChevronRightIcon } from 'lucide-react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { appRoutes } from '@/app/routes'
import { resolveWorkspaceId } from '@/app/routeHelpers'

/**
 * Context breadcrumbs rendered in the top bar.
 *
 * Phase A dropped the ``/kb/:kbId`` segment from URLs, so breadcrumbs
 * only ever have two links (Workspaces > <workspaceId>) plus the leaf
 * label describing the current page.
 */
export default function ContextBreadcrumbs() {
  const { t } = useTranslation()
  const location = useLocation()
  const { workspaceId } = useParams()

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const path = location.pathname

  const trailing = path.endsWith('/members')
    ? t('header.members')
    : path.endsWith('/settings')
      ? t('header.workspaceSettings')
      : path.endsWith('/overview')
        ? t('header.overview')
        : path.endsWith('/graph')
          ? t('header.knowledgeGraph')
          : path.endsWith('/retrieval')
            ? t('header.retrieval')
            : path.endsWith('/api')
              ? t('header.api')
              : path.endsWith('/knowledge-bases') || path.includes('/knowledge-bases/')
                ? t('header.kbSettings')
                : t('header.documents')

  const items = [
    {
      label: t('header.workspaces'),
      to: appRoutes.workspaces,
    },
    {
      label: currentWorkspaceId,
      to: appRoutes.workspace(currentWorkspaceId),
    },
  ]

  return (
    <div className="flex min-w-0 items-center gap-1 text-sm text-muted-foreground">
      {items.map((item) => (
        <div key={item.to} className="flex min-w-0 items-center gap-1">
          <Link to={item.to} className="truncate transition-colors hover:text-foreground">
            {item.label}
          </Link>
          <ChevronRightIcon className="size-3 shrink-0 opacity-60" aria-hidden="true" />
        </div>
      ))}
      <span className="truncate font-medium text-foreground">{trailing}</span>
    </div>
  )
}
