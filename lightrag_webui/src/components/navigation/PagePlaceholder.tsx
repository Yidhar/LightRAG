import Button from '@/components/ui/Button'
import Badge from '@/components/ui/Badge'
import { Link } from 'react-router-dom'

interface PagePlaceholderProps {
  eyebrow: string
  title: string
  description: string
  context?: Array<{ label: string; value: string }>
  primaryAction?: {
    label: string
    to: string
  }
}

export default function PagePlaceholder({
  eyebrow,
  title,
  description,
  context = [],
  primaryAction,
}: PagePlaceholderProps) {
  return (
    <div className="flex h-full w-full overflow-auto p-6">
      <div className="mx-auto flex h-full w-full max-w-5xl items-center justify-center">
        <div className="surface-panel relative w-full max-w-2xl overflow-hidden rounded-[28px] border border-border/60 bg-gradient-to-br from-emerald-500/6 via-card/95 to-card px-8 py-10 shadow-sm">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-40 bg-gradient-to-b from-emerald-500/12 via-transparent to-transparent" />
          <div className="relative space-y-6">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className="rounded-full px-3 py-1 uppercase tracking-[0.12em]">
                {eyebrow}
              </Badge>
              {context.map((item) => (
                <Badge key={`${item.label}-${item.value}`} variant="outline" className="rounded-full px-3 py-1">
                  <span className="mr-2 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                    {item.label}
                  </span>
                  <span className="font-medium text-foreground">{item.value}</span>
                </Badge>
              ))}
            </div>

            <div className="space-y-3">
              <h1 className="text-3xl font-semibold tracking-tight text-foreground">{title}</h1>
              <p className="max-w-xl text-sm leading-7 text-muted-foreground">{description}</p>
            </div>

            {primaryAction && (
              <div className="flex flex-wrap items-center gap-3">
                <Button asChild>
                  <Link to={primaryAction.to}>{primaryAction.label}</Link>
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
