import { ChevronRightIcon } from 'lucide-react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'

export default function ContextBreadcrumbs() {
  const { t } = useTranslation()
  const location = useLocation()
  const { workspaceId, kbId } = useParams()

  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const path = location.pathname

  const trailing =
    path.includes('/members')
      ? t('header.members')
      : path.includes('/settings') && !path.includes('/kb/')
        ? t('header.workspaceSettings')
        : path.includes('/overview')
          ? t('header.overview')
          : path.includes('/graph')
            ? t('header.knowledgeGraph')
            : path.includes('/retrieval')
              ? t('header.retrieval')
              : path.includes('/api')
                ? t('header.api')
                : path.includes('/settings')
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

  if (path.includes('/kb/')) {
    items.push({
      label: currentKnowledgeBaseId,
      to: appRoutes.kbOverview(currentWorkspaceId, currentKnowledgeBaseId),
    })
  }

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
