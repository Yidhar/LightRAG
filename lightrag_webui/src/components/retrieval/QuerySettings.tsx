import { useCallback, useMemo } from 'react'
import { QueryMode, QueryRequest } from '@/api/lightrag'
import Checkbox from '@/components/ui/Checkbox'
import Input from '@/components/ui/Input'
import UserPromptInputWithHistory from '@/components/ui/UserPromptInputWithHistory'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/Tooltip'
import { useSettingsStore } from '@/stores/settings'
import { useTranslation } from 'react-i18next'
import { RotateCcw } from 'lucide-react'
import { cn } from '@/lib/utils'

const ResetButton = ({ onClick, title }: { onClick: () => void; title: string }) => (
  <TooltipProvider>
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          className="inline-flex size-8 items-center justify-center rounded-xl border border-border/70 bg-background text-muted-foreground transition-colors hover:border-emerald-500/30 hover:text-foreground"
          title={title}
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="left">
        <p>{title}</p>
      </TooltipContent>
    </Tooltip>
  </TooltipProvider>
)

const SectionLabel = ({
  htmlFor,
  label,
  tooltip,
}: {
  htmlFor: string
  label: string
  tooltip: string
}) => (
  <TooltipProvider>
    <Tooltip>
      <TooltipTrigger asChild>
        <label htmlFor={htmlFor} className="ml-1 cursor-help text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
          {label}
        </label>
      </TooltipTrigger>
      <TooltipContent side="left">
        <p className="max-w-xs whitespace-pre-wrap text-xs">{tooltip}</p>
      </TooltipContent>
    </Tooltip>
  </TooltipProvider>
)

type QuerySettingsProps = {
  className?: string
  showHeader?: boolean
}

export default function QuerySettings({
  className,
  showHeader = true,
}: QuerySettingsProps) {
  const { t } = useTranslation()
  const querySettings = useSettingsStore((state) => state.querySettings)
  const userPromptHistory = useSettingsStore((state) => state.userPromptHistory)

  const handleChange = useCallback((key: keyof QueryRequest, value: any) => {
    useSettingsStore.getState().updateQuerySettings({ [key]: value })
  }, [])

  const handleSelectFromHistory = useCallback((prompt: string) => {
    handleChange('user_prompt', prompt)
  }, [handleChange])

  const handleDeleteFromHistory = useCallback((index: number) => {
    const newHistory = [...userPromptHistory]
    newHistory.splice(index, 1)
    useSettingsStore.getState().setUserPromptHistory(newHistory)
  }, [userPromptHistory])

  const defaultValues = useMemo(() => ({
    mode: 'mix' as QueryMode,
    top_k: 40,
    chunk_top_k: 20,
    max_entity_tokens: 6000,
    max_relation_tokens: 8000,
    max_total_tokens: 30000,
    history_turns: 0,
  }), [])

  const handleReset = useCallback((key: keyof typeof defaultValues) => {
    handleChange(key, defaultValues[key])
  }, [handleChange, defaultValues])

  return (
    <Card className={cn('flex min-h-0 flex-col rounded-[28px] border-border/70 bg-background/95', className)}>
      {showHeader && (
        <CardHeader className="border-b border-border/60 px-5 py-4">
          <CardTitle>{t('retrievePanel.querySettings.parametersTitle')}</CardTitle>
          <CardDescription>{t('retrievePanel.querySettings.parametersDescription')}</CardDescription>
        </CardHeader>
      )}

      <CardContent className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <div className="space-y-5">
          <section className="space-y-3 rounded-[24px] border border-border/70 bg-muted/20 p-4">
            <div className="space-y-2">
              <SectionLabel
                htmlFor="user_prompt"
                label={t('retrievePanel.querySettings.userPrompt')}
                tooltip={t('retrievePanel.querySettings.userPromptTooltip')}
              />
              <UserPromptInputWithHistory
                id="user_prompt"
                value={querySettings.user_prompt || ''}
                onChange={(value) => handleChange('user_prompt', value)}
                onSelectFromHistory={handleSelectFromHistory}
                onDeleteFromHistory={handleDeleteFromHistory}
                history={userPromptHistory}
                placeholder={t('retrievePanel.querySettings.userPromptPlaceholder')}
                className="h-10"
              />
            </div>

            <div className="space-y-2">
              <SectionLabel
                htmlFor="query_mode_select"
                label={t('retrievePanel.querySettings.queryMode')}
                tooltip={t('retrievePanel.querySettings.queryModeTooltip')}
              />
              <div className="flex items-center gap-2">
                <Select
                  value={querySettings.mode}
                  onValueChange={(v) => handleChange('mode', v as QueryMode)}
                >
                  <SelectTrigger
                    id="query_mode_select"
                    className="h-10 flex-1 rounded-2xl border-border/70 bg-background text-left [&>span]:break-all [&>span]:line-clamp-1"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="naive">{t('retrievePanel.querySettings.queryModeOptions.naive')}</SelectItem>
                      <SelectItem value="local">{t('retrievePanel.querySettings.queryModeOptions.local')}</SelectItem>
                      <SelectItem value="global">{t('retrievePanel.querySettings.queryModeOptions.global')}</SelectItem>
                      <SelectItem value="hybrid">{t('retrievePanel.querySettings.queryModeOptions.hybrid')}</SelectItem>
                      <SelectItem value="mix">{t('retrievePanel.querySettings.queryModeOptions.mix')}</SelectItem>
                      <SelectItem value="bypass">{t('retrievePanel.querySettings.queryModeOptions.bypass')}</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <ResetButton
                  onClick={() => handleReset('mode')}
                  title={t('retrievePanel.querySettings.resetModeToDefault', {
                    value: t('retrievePanel.querySettings.queryModeOptions.mix'),
                  })}
                />
              </div>
            </div>
          </section>

          <section className="space-y-4 rounded-[24px] border border-border/70 bg-background/80 p-4">
            <div className="grid gap-4">
              {[
                {
                  id: 'top_k',
                  key: 'top_k' as const,
                  label: t('retrievePanel.querySettings.topK'),
                  tooltip: t('retrievePanel.querySettings.topKTooltip'),
                  placeholder: t('retrievePanel.querySettings.topKPlaceholder'),
                },
                {
                  id: 'chunk_top_k',
                  key: 'chunk_top_k' as const,
                  label: t('retrievePanel.querySettings.chunkTopK'),
                  tooltip: t('retrievePanel.querySettings.chunkTopKTooltip'),
                  placeholder: t('retrievePanel.querySettings.chunkTopKPlaceholder'),
                },
                {
                  id: 'max_entity_tokens',
                  key: 'max_entity_tokens' as const,
                  label: t('retrievePanel.querySettings.maxEntityTokens'),
                  tooltip: t('retrievePanel.querySettings.maxEntityTokensTooltip'),
                  placeholder: t('retrievePanel.querySettings.maxEntityTokensPlaceholder'),
                },
                {
                  id: 'max_relation_tokens',
                  key: 'max_relation_tokens' as const,
                  label: t('retrievePanel.querySettings.maxRelationTokens'),
                  tooltip: t('retrievePanel.querySettings.maxRelationTokensTooltip'),
                  placeholder: t('retrievePanel.querySettings.maxRelationTokensPlaceholder'),
                },
                {
                  id: 'max_total_tokens',
                  key: 'max_total_tokens' as const,
                  label: t('retrievePanel.querySettings.maxTotalTokens'),
                  tooltip: t('retrievePanel.querySettings.maxTotalTokensTooltip'),
                  placeholder: t('retrievePanel.querySettings.maxTotalTokensPlaceholder'),
                },
                {
                  id: 'history_turns',
                  key: 'history_turns' as const,
                  label: t('retrievePanel.querySettings.historyTurns'),
                  tooltip: t('retrievePanel.querySettings.historyTurnsTooltip'),
                  placeholder: t('retrievePanel.querySettings.historyTurnsPlaceholder'),
                },
              ].map((field) => (
                <div key={field.id} className="space-y-2">
                  <SectionLabel htmlFor={field.id} label={field.label} tooltip={field.tooltip} />
                  <div className="flex items-center gap-2">
                    <Input
                      id={field.id}
                      type="number"
                      value={querySettings[field.key] ?? ''}
                      onChange={(e) => {
                        const value = e.target.value
                        handleChange(field.key, value === '' ? '' : parseInt(value, 10) || 0)
                      }}
                      onBlur={(e) => {
                        const value = e.target.value
                        if (value === '' || isNaN(parseInt(value, 10))) {
                          handleChange(field.key, defaultValues[field.key])
                        }
                      }}
                      min={field.key === 'history_turns' ? 0 : 1}
                      placeholder={field.placeholder}
                      className="h-10 flex-1 rounded-2xl border-border/70 bg-background pr-2 [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none [-moz-appearance:textfield]"
                    />
                    <ResetButton
                      onClick={() => handleReset(field.key)}
                      title={t('retrievePanel.querySettings.resetToDefault')}
                    />
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="space-y-3 rounded-[24px] border border-border/70 bg-muted/20 p-4">
            {[
              {
                id: 'enable_rerank',
                checked: querySettings.enable_rerank,
                label: t('retrievePanel.querySettings.enableRerank'),
                tooltip: t('retrievePanel.querySettings.enableRerankTooltip'),
                onCheckedChange: (checked: boolean) => handleChange('enable_rerank', checked),
              },
              {
                id: 'only_need_context',
                checked: querySettings.only_need_context,
                label: t('retrievePanel.querySettings.onlyNeedContext'),
                tooltip: t('retrievePanel.querySettings.onlyNeedContextTooltip'),
                onCheckedChange: (checked: boolean) => {
                  handleChange('only_need_context', checked)
                  if (checked) handleChange('only_need_prompt', false)
                },
              },
              {
                id: 'only_need_prompt',
                checked: querySettings.only_need_prompt,
                label: t('retrievePanel.querySettings.onlyNeedPrompt'),
                tooltip: t('retrievePanel.querySettings.onlyNeedPromptTooltip'),
                onCheckedChange: (checked: boolean) => {
                  handleChange('only_need_prompt', checked)
                  if (checked) handleChange('only_need_context', false)
                },
              },
              {
                id: 'stream',
                checked: querySettings.stream,
                label: t('retrievePanel.querySettings.streamResponse'),
                tooltip: t('retrievePanel.querySettings.streamResponseTooltip'),
                onCheckedChange: (checked: boolean) => handleChange('stream', checked),
              },
            ].map((toggle) => (
              <div key={toggle.id} className="flex items-center justify-between gap-3 rounded-2xl border border-border/70 bg-background/80 px-3 py-3">
                <div className="min-w-0">
                  <SectionLabel htmlFor={toggle.id} label={toggle.label} tooltip={toggle.tooltip} />
                </div>
                <Checkbox
                  id={toggle.id}
                  checked={toggle.checked}
                  onCheckedChange={(checked) => toggle.onCheckedChange(checked === true)}
                />
              </div>
            ))}
          </section>
        </div>
      </CardContent>
    </Card>
  )
}
