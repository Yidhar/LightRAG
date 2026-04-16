import { memo, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import rehypeReact from 'rehype-react'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark, oneLight } from 'react-syntax-highlighter/dist/cjs/styles/prism'

import { fetchImageBlobUrl } from '@/api/lightrag'
import useTheme from '@/hooks/useTheme'
import { cn } from '@/lib/utils'
import { remarkFootnotes } from '@/utils/remarkFootnotes'

interface KaTeXOptions {
  errorColor?: string
  throwOnError?: boolean
  displayMode?: boolean
  strict?: boolean
  trust?: boolean
  errorCallback?: (error: string, latex: string) => void
}

type MarkdownMessageMode = 'main' | 'thinking'

type MarkdownMessageContentProps = {
  content: string
  messageRole: 'user' | 'assistant'
  renderAsDiagram?: boolean
  latexRendered?: boolean
  mode?: MarkdownMessageMode
}

function InlineBlobImage({ blobId, alt }: { blobId: string; alt: string }) {
  const { t } = useTranslation()
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false
    let objectUrl: string | null = null

    fetchImageBlobUrl(blobId)
      .then((u) => {
        if (cancelled) {
          URL.revokeObjectURL(u)
          return
        }
        objectUrl = u
        setUrl(u)
      })
      .catch(() => {
        if (!cancelled) setError(true)
      })

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [blobId])

  if (error) {
    return (
      <span className="text-muted-foreground inline-flex items-center gap-1 text-xs">
        {t('retrievalRichContent.imageFailed', { blobId })}
      </span>
    )
  }

  if (!url) {
    return (
      <span className="bg-muted my-2 inline-flex h-32 w-48 animate-pulse items-center justify-center rounded text-xs">
        {t('retrievalRichContent.loading')}
      </span>
    )
  }

  return (
    <img
      src={url}
      alt={alt}
      className="my-2 max-w-full rounded border shadow-sm"
      loading="lazy"
    />
  )
}

export default function MarkdownMessageContent({
  content,
  messageRole,
  renderAsDiagram = false,
  latexRendered = true,
  mode = 'main',
}: MarkdownMessageContentProps) {
  const { theme } = useTheme()
  const [katexPlugin, setKatexPlugin] = useState<((options?: KaTeXOptions) => any) | null>(null)

  useEffect(() => {
    const loadKaTeX = async () => {
      try {
        const { default: rehypeKatex } = await import('rehype-katex')
        setKatexPlugin(() => rehypeKatex)
      } catch (error) {
        console.error('Failed to load KaTeX plugin:', error)
        setKatexPlugin(null)
      }
    }

    void loadKaTeX()
  }, [])

  const mainMarkdownComponents = useMemo(
    () => ({
      code: (props: any) => {
        const { inline, className, children, ...restProps } = props
        const match = /language-(\w+)/.exec(className || '')
        const language = match ? match[1] : undefined

        if (language === 'math' && !inline) {
          return (
            <div className="katex-display-wrapper my-4 overflow-x-auto">
              <div className="text-current">{children}</div>
            </div>
          )
        }

        if (language === 'math' && inline) {
          return (
            <span className="katex-inline-wrapper">
              <span className="text-current">{children}</span>
            </span>
          )
        }

        return (
          <CodeHighlight
            inline={inline}
            className={className}
            {...restProps}
            renderAsDiagram={renderAsDiagram}
            messageRole={messageRole}
          >
            {children}
          </CodeHighlight>
        )
      },
      p: ({ children }: { children?: ReactNode }) => <div className="my-2">{children}</div>,
      h1: ({ children }: { children?: ReactNode }) => (
        <h1 className="mt-4 mb-2 text-xl font-bold">{children}</h1>
      ),
      h2: ({ children }: { children?: ReactNode }) => (
        <h2 className="mt-4 mb-2 text-lg font-bold">{children}</h2>
      ),
      h3: ({ children }: { children?: ReactNode }) => (
        <h3 className="mt-3 mb-2 text-base font-bold">{children}</h3>
      ),
      h4: ({ children }: { children?: ReactNode }) => (
        <h4 className="mt-3 mb-2 text-base font-semibold">{children}</h4>
      ),
      ul: ({ children }: { children?: ReactNode }) => <ul className="my-2 list-disc pl-5">{children}</ul>,
      ol: ({ children }: { children?: ReactNode }) => <ol className="my-2 list-decimal pl-5">{children}</ol>,
      li: ({ children }: { children?: ReactNode }) => <li className="my-1">{children}</li>,
      img: (props: any) => {
        const { src, alt } = props
        if (src && /\/images\/img-/.test(src)) {
          const blobId = src.split('/images/')[1]?.split(/[?#]/)[0]
          if (blobId) {
            return <InlineBlobImage blobId={blobId} alt={alt || ''} />
          }
        }

        return <img src={src} alt={alt} className="my-2 max-w-full rounded" loading="lazy" />
      },
    }),
    [messageRole, renderAsDiagram]
  )

  const thinkingMarkdownComponents = useMemo(
    () => ({
      code: (props: any) => (
        <CodeHighlight {...props} renderAsDiagram={renderAsDiagram} messageRole={messageRole} />
      ),
    }),
    [messageRole, renderAsDiagram]
  )

  const sourceLabel = mode === 'thinking' ? 'thinking content' : 'main content'
  let katexRehypePlugin: any = null
  if (katexPlugin && latexRendered) {
    katexRehypePlugin = [
      katexPlugin,
      {
        errorColor: theme === 'dark' ? '#ef4444' : '#dc2626',
        throwOnError: false,
        displayMode: false,
        strict: false,
        trust: true,
        errorCallback: (error: string, latex: string) => {
          if (import.meta.env.DEV) {
            console.warn(`KaTeX rendering error in ${sourceLabel}:`, error, 'for LaTeX:', latex)
          }
        },
      },
    ] as any
  }
  const rehypePlugins = [
    rehypeRaw,
    ...(katexRehypePlugin ? [katexRehypePlugin] : []),
    rehypeReact,
  ]

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkFootnotes, remarkMath]}
      rehypePlugins={rehypePlugins}
      skipHtml={false}
      components={mode === 'thinking' ? thinkingMarkdownComponents : mainMarkdownComponents}
    >
      {content}
    </ReactMarkdown>
  )
}

interface CodeHighlightProps {
  inline?: boolean
  className?: string
  children?: ReactNode
  renderAsDiagram?: boolean
  messageRole?: 'user' | 'assistant'
}

const isLargeJson = (language: string | undefined, content: string | undefined): boolean => {
  if (!content || language !== 'json') return false
  return content.length > 5000
}

const CodeHighlight = memo(
  ({ inline, className, children, renderAsDiagram = false, messageRole, ...props }: CodeHighlightProps) => {
    const { t } = useTranslation()
    const { theme } = useTheme()
    const [hasRendered, setHasRendered] = useState(false)
    const match = className?.match(/language-(\w+)/)
    const language = match ? match[1] : undefined
    const mermaidRef = useRef<HTMLDivElement>(null)
    const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

    const contentStr = String(children || '').replace(/\n$/, '')
    const isLargeJsonBlock = isLargeJson(language, contentStr)

    useEffect(() => {
      if (!(renderAsDiagram && !hasRendered && language === 'mermaid' && mermaidRef.current)) {
        return
      }

      const container = mermaidRef.current
      let cancelled = false

      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current)
      }

      debounceTimerRef.current = setTimeout(() => {
        const renderMermaid = async () => {
          if (!container || cancelled || hasRendered) return

          try {
            const { default: mermaid } = await import('mermaid')

            if (cancelled || mermaidRef.current !== container || hasRendered) {
              return
            }

            mermaid.initialize({
              startOnLoad: false,
              theme: theme === 'dark' ? 'dark' : 'default',
              securityLevel: 'loose',
              suppressErrorRendering: true,
            })

            container.innerHTML =
              '<div class="flex items-center justify-center p-4"><svg class="h-5 w-5 animate-spin text-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg></div>'

            const rawContent = String(children).replace(/\n$/, '').trim()
            const looksPotentiallyComplete =
              rawContent.length > 10 &&
              (
                rawContent.startsWith('graph') ||
                rawContent.startsWith('sequenceDiagram') ||
                rawContent.startsWith('classDiagram') ||
                rawContent.startsWith('stateDiagram') ||
                rawContent.startsWith('gantt') ||
                rawContent.startsWith('pie') ||
                rawContent.startsWith('flowchart') ||
                rawContent.startsWith('erDiagram')
              )

            if (!looksPotentiallyComplete) {
              return
            }

            const processedContent = rawContent
              .split('\n')
              .map((line) => {
                const trimmedLine = line.trim()
                if (trimmedLine.startsWith('subgraph')) {
                  const parts = trimmedLine.split(' ')
                  if (parts.length > 1) {
                    const title = parts.slice(1).join(' ').replace(/["']/g, '')
                    return `subgraph "${title}"`
                  }
                }
                return trimmedLine
              })
              .filter((line) => !line.trim().startsWith('linkStyle'))
              .join('\n')

            const mermaidId = `mermaid-${Date.now()}`
            const { svg, bindFunctions } = await mermaid.render(mermaidId, processedContent)

            if (cancelled || mermaidRef.current !== container || hasRendered) {
              return
            }

            container.innerHTML = svg
            setHasRendered(true)

            if (bindFunctions) {
              try {
                bindFunctions(container)
              } catch (bindError) {
                console.error('Mermaid bindFunctions error:', bindError)
                container.innerHTML +=
                  `<p class="text-orange-500 text-xs">${t('retrievalRichContent.diagramInteractionsLimited')}</p>`
              }
            }
          } catch (error) {
            console.error('Mermaid rendering error:', error)
            console.error('Failed content:', String(children))
            if (mermaidRef.current === container) {
              const errorMessage = error instanceof Error ? error.message : String(error)
              const errorPre = document.createElement('pre')
              errorPre.className = 'text-red-500 text-xs whitespace-pre-wrap break-words'
              errorPre.textContent = t('retrievalRichContent.mermaidError', { error: errorMessage })
              container.innerHTML = ''
              container.appendChild(errorPre)
            }
          }
        }

        void renderMermaid()
      }, 300)

      return () => {
        cancelled = true
        if (debounceTimerRef.current) {
          clearTimeout(debounceTimerRef.current)
        }
      }
    }, [renderAsDiagram, hasRendered, language, children, theme, t])

    if (isLargeJsonBlock) {
      return (
        <pre className="bg-muted overflow-x-auto rounded-md p-4 font-mono text-sm whitespace-pre-wrap break-words">
          {contentStr}
        </pre>
      )
    }

    if (language === 'mermaid' && !renderAsDiagram) {
      return (
        <SyntaxHighlighter
          style={theme === 'dark' ? oneDark : oneLight}
          PreTag="div"
          language="text"
          {...props}
        >
          {contentStr}
        </SyntaxHighlighter>
      )
    }

    if (language === 'mermaid') {
      return <div className="mermaid-diagram-container my-4 overflow-x-auto" ref={mermaidRef}></div>
    }

    const isInline = inline ?? !className?.startsWith('language-')

    const getInlineCodeStyles = () => {
      if (messageRole === 'user') {
        return 'border border-primary-foreground/30 bg-primary-foreground/20 text-primary-foreground'
      }

      return theme === 'dark'
        ? 'border border-muted-foreground/30 bg-muted-foreground/20 text-muted-foreground'
        : 'border border-slate-300 bg-slate-200 text-slate-800'
    }

    return !isInline ? (
      <SyntaxHighlighter
        style={theme === 'dark' ? oneDark : oneLight}
        PreTag="div"
        language={language}
        {...props}
      >
        {contentStr}
      </SyntaxHighlighter>
    ) : (
      <code
        className={cn(className, 'mx-1 rounded-sm px-1 py-0.5 font-mono text-sm', getInlineCodeStyles())}
        {...props}
      >
        {children}
      </code>
    )
  }
)

CodeHighlight.displayName = 'CodeHighlight'
