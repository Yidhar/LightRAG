import { type FormEvent, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Settings2Icon, ShieldCheckIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { appRoutes } from '@/app/routes'
import { resolveKnowledgeBaseId, resolveWorkspaceId } from '@/app/routeHelpers'
import {
  getKnowledgeBase,
  type KnowledgeBaseRecord,
  type KnowledgeBaseUpdateRequest,
  updateKnowledgeBase,
} from '@/api/lightrag'
import { useAuthStore } from '@/stores/state'
import {
  hasPermission,
  resolveEffectiveRole,
  roleDescriptionKeys,
} from '@/lib/permissions'
import { errorMessage } from '@/lib/utils'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/Alert'
import Badge from '@/components/ui/Badge'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import Textarea from '@/components/ui/Textarea'

/**
 * KB settings — stripped to the three user-facing fields that matter:
 * display name, description, category (tag).
 *
 * Dropped entirely (see commit history): KB id input, status input,
 * config-override JSON editor, KB-scoped member management + role
 * table. Those were operator concerns wearing a "settings page"
 * costume; the simplified product is "workspaces hold KBs, KBs hold
 * files" so this page only needs to edit the KB's display metadata.
 *
 * Member management routes still exist on the backend for multi-tenant
 * deployments — they're just off the main sidebar.
 */
interface MetadataEditorProps {
  knowledgeBase: KnowledgeBaseRecord
  submitting: boolean
  onSubmit: (payload: KnowledgeBaseUpdateRequest) => Promise<void>
}

function KnowledgeBaseMetadataEditor({
  knowledgeBase,
  submitting,
  onSubmit,
}: MetadataEditorProps) {
  const { t } = useTranslation()
  const [name, setName] = useState(knowledgeBase.name || '')
  const [description, setDescription] = useState(knowledgeBase.description || '')
  const [category, setCategory] = useState(knowledgeBase.category || '')

  const initialName = knowledgeBase.name || ''
  const initialDescription = knowledgeBase.description || ''
  const initialCategory = knowledgeBase.category || ''

  const handleReset = () => {
    setName(initialName)
    setDescription(initialDescription)
    setCategory(initialCategory)
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    const trimmedName = name.trim()
    const trimmedDescription = description.trim()
    const trimmedCategory = category.trim()

    if (!trimmedName) {
      toast.error(
        t('platformShell.kbSettings.metadataEditor.errors.displayNameEmpty')
      )
      return
    }

    // Only send fields the user actually changed. Backend treats missing
    // fields as "no change", which keeps status / config_override stable.
    const payload: KnowledgeBaseUpdateRequest = {}
    if (trimmedName !== initialName) payload.name = trimmedName
    if (trimmedDescription !== initialDescription) {
      payload.description = trimmedDescription
    }
    if (trimmedCategory !== initialCategory) {
      // "" explicitly clears the tag back to uncategorised.
      payload.category = trimmedCategory
    }

    if (Object.keys(payload).length === 0) {
      toast.message(t('platformShell.kbSettings.metadataEditor.noChanges'))
      return
    }

    await onSubmit(payload)
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-4 rounded-xl border border-border/60 bg-background/70 p-5"
    >
      <div className="space-y-2">
        <label htmlFor="kb-settings-name" className="text-sm font-medium text-foreground">
          {t('platformShell.workspaceDirectory.displayName')}
        </label>
        <Input
          id="kb-settings-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          disabled={submitting}
          required
          autoFocus
          className="h-10 rounded-xl border-border/70"
        />
      </div>

      <div className="space-y-2">
        <label
          htmlFor="kb-settings-description"
          className="text-sm font-medium text-foreground"
        >
          {t('platformShell.workspaceDirectory.descriptionLabel')}
        </label>
        <Textarea
          id="kb-settings-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={t('platformShell.workspaceDirectory.descriptionPlaceholder')}
          disabled={submitting}
          rows={3}
          className="rounded-xl border-border/70"
        />
      </div>

      <div className="space-y-2">
        <label htmlFor="kb-settings-category" className="text-sm font-medium text-foreground">
          {t('platformShell.workspaceDirectory.categoryLabel', {
            defaultValue: '分类',
          })}
        </label>
        <Input
          id="kb-settings-category"
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          placeholder={t('platformShell.workspaceDirectory.categoryPlaceholder', {
            defaultValue: '例如：研究、行业、客户… 留空表示未分类',
          })}
          disabled={submitting}
          autoComplete="off"
          className="h-10 rounded-xl border-border/70"
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-4">
        <p className="text-[11px] text-muted-foreground">
          ID: <span className="font-mono">{knowledgeBase.id}</span>
        </p>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleReset}
            disabled={submitting}
          >
            {t('platformShell.kbSettings.metadataEditor.reset')}
          </Button>
          <Button type="submit" size="sm" disabled={submitting}>
            {submitting
              ? t('common.saving')
              : t('platformShell.kbSettings.metadataEditor.saveChanges')}
          </Button>
        </div>
      </div>
    </form>
  )
}

export default function KnowledgeBaseSettingsPage() {
  const { t } = useTranslation()
  const { workspaceId, kbId } = useParams()
  const { role, memberships } = useAuthStore()
  const currentWorkspaceId = resolveWorkspaceId(workspaceId)
  const currentKnowledgeBaseId = resolveKnowledgeBaseId(kbId)
  const effectiveRole = resolveEffectiveRole(
    { role, memberships },
    { workspaceId: currentWorkspaceId, kbId: currentKnowledgeBaseId }
  )
  const canManageKnowledgeBaseSettings = hasPermission(
    effectiveRole,
    'kb:manage_settings'
  )

  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBaseRecord | null>(
    null
  )
  const [error, setError] = useState<string | null>(null)
  const [metadataSubmitting, setMetadataSubmitting] = useState(false)

  useEffect(() => {
    let cancelled = false
    const loadKnowledgeBase = async () => {
      try {
        setError(null)
        const response = await getKnowledgeBase(
          currentWorkspaceId,
          currentKnowledgeBaseId
        )
        if (!cancelled) setKnowledgeBase(response)
      } catch (loadError) {
        if (!cancelled) {
          const message =
            loadError instanceof Error
              ? loadError.message
              : t('platformShell.kbSettings.loadFailed')
          setError(message)
          setKnowledgeBase(null)
        }
      }
    }
    void loadKnowledgeBase()
    return () => {
      cancelled = true
    }
  }, [currentKnowledgeBaseId, currentWorkspaceId, t])

  const handleMetadataSubmit = async (payload: KnowledgeBaseUpdateRequest) => {
    try {
      setMetadataSubmitting(true)
      const response = await updateKnowledgeBase(
        currentWorkspaceId,
        currentKnowledgeBaseId,
        payload
      )
      setKnowledgeBase(response.kb)
      toast.success(response.message)
      // Notify the KB tabs / list components to refresh so a rename
      // reflects immediately without a full reload.
      window.dispatchEvent(
        new CustomEvent('lightrag:kb-updated', {
          detail: {
            workspaceId: currentWorkspaceId,
            kbId: currentKnowledgeBaseId,
          },
        })
      )
    } catch (submitError) {
      toast.error(errorMessage(submitError))
    } finally {
      setMetadataSubmitting(false)
    }
  }

  // Human-friendly subtitle keys reused from the existing i18n bundle
  // so we don't have to introduce new translations for this trim.
  const metadataEditorKey = knowledgeBase
    ? `${knowledgeBase.id}:${knowledgeBase.name}:${knowledgeBase.description}:${knowledgeBase.category}`
    : 'kb-metadata-form'

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border/60 px-6 py-4">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="rounded-full px-2.5 py-0.5 text-[11px] uppercase tracking-[0.12em]"
            >
              {t('platformShell.common.kbSettings')}
            </Badge>
            <span className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
              {currentWorkspaceId} / {knowledgeBase?.name || currentKnowledgeBaseId}
            </span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            {t('platformShell.kbSettings.title')}
          </h1>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            {t('platformShell.kbSettings.description')}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to={appRoutes.kbDocuments(currentWorkspaceId, currentKnowledgeBaseId)}>
            {t('platformShell.common.openDocuments')}
          </Link>
        </Button>
      </header>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 bg-muted/20 px-6 py-2 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheckIcon
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            aria-hidden="true"
          />
          <span className="truncate text-muted-foreground">
            {t(roleDescriptionKeys[effectiveRole])}
          </span>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-6 py-6">
          {error && (
            <Alert className="border-border/70 bg-muted/20">
              <AlertTitle>
                {t('platformShell.kbSettings.registryUnavailableTitle')}
              </AlertTitle>
              <AlertDescription>
                {t('platformShell.kbSettings.registryUnavailableDescription')}
              </AlertDescription>
            </Alert>
          )}

          <div className="flex items-center gap-2">
            <Settings2Icon
              className="size-4 text-emerald-600 dark:text-emerald-400"
              aria-hidden="true"
            />
            <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
              {t('platformShell.kbSettings.metadataAndOverridesTitle')}
            </h2>
          </div>

          {knowledgeBase ? (
            canManageKnowledgeBaseSettings ? (
              <KnowledgeBaseMetadataEditor
                key={metadataEditorKey}
                knowledgeBase={knowledgeBase}
                submitting={metadataSubmitting}
                onSubmit={handleMetadataSubmit}
              />
            ) : (
              <Alert className="border-border/70 bg-muted/20">
                <AlertTitle>
                  {t('platformShell.kbSettings.metadataReadOnlyTitle')}
                </AlertTitle>
                <AlertDescription>
                  {t('platformShell.kbSettings.metadataReadOnlyDescription')}
                </AlertDescription>
              </Alert>
            )
          ) : (
            <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-foreground">
              {t('platformShell.kbSettings.metadataPlaceholder')}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
