import { ArrowClockwise, Sparkle } from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { ErrorState } from '@/components/ui/States'
import { Skeleton } from '@/components/ui/Skeleton'
import { ticketKeys } from '@/features/tickets/keys'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { toast } from '@/store/toasts'
import type { components } from '@/types/api'
import { cn } from '@/utils/cn'

type AIAnalysis = components['schemas']['AIAnalysisRead']
type Operation = 'analyze' | 'summarize' | 'suggest-response'

function resultText(analysis: AIAnalysis): string[] {
  const result = analysis.result
  if (!result) return []
  const lines: string[] = []
  const read = (key: string) => result[key]

  if (analysis.operation === 'classify') {
    const category = read('category')
    const subcategory = read('subcategory')
    const priority = read('priority')
    if (typeof category === 'string') lines.push(`Category: ${category}${typeof subcategory === 'string' ? ` · ${subcategory}` : ''}`)
    if (typeof priority === 'string') lines.push(`Suggested priority: ${priority}`)
  } else if (analysis.operation === 'sentiment') {
    const sentiment = read('sentiment')
    if (typeof sentiment === 'string') lines.push(`Sentiment: ${sentiment}`)
  } else if (analysis.operation === 'summarize') {
    const summary = read('summary')
    if (typeof summary === 'string') lines.push(summary)
  }

  return lines
}

function operationLabel(operation: AIAnalysis['operation']): string {
  if (operation === 'classify') return 'Classification'
  if (operation === 'sentiment') return 'Sentiment'
  if (operation === 'summarize') return 'Conversation summary'
  if (operation === 'suggest_response') return 'Reply draft'
  if (operation === 'knowledge_answer') return 'Knowledge answer'
  return 'Embedding'
}

export function TicketAssistant({
  ticketId,
  canAnalyze,
  canSuggest,
}: {
  ticketId: string
  canAnalyze: boolean
  canSuggest: boolean
}) {
  const queryClient = useQueryClient()
  const analysesQuery = useQuery({
    queryKey: ticketKeys.aiAnalyses(ticketId),
    queryFn: async () => (await apiClient.get<AIAnalysis[]>(`/tickets/${ticketId}/ai/analyses`)).data,
    enabled: Boolean(ticketId && canAnalyze),
    refetchInterval: (query) =>
      query.state.data?.some((analysis) => analysis.status === 'pending' || analysis.status === 'processing')
        ? 3_000
        : false,
  })

  const requestMutation = useMutation<AIAnalysis | AIAnalysis[], unknown, Operation>({
    mutationFn: async (operation) => {
      const { data } = await apiClient.post<AIAnalysis | AIAnalysis[]>(`/tickets/${ticketId}/ai/${operation}`)
      return data
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ticketKeys.aiAnalyses(ticketId) })
      toast.success('AI request queued', 'The result will appear here when it is ready.')
    },
    onError: (error) => toast.error('AI request failed', toApiError(error).message),
  })

  if (!canAnalyze && !canSuggest) return null

  const busy = requestMutation.isPending
  const analyses = analysesQuery.data ?? []
  const working = analyses.some((analysis) => analysis.status === 'pending' || analysis.status === 'processing')

  return (
    <Card variant="bezel" className="overflow-hidden">
      <div className="flex items-start justify-between gap-3 border-b border-border-subtle px-4 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className="grid size-8 place-items-center rounded-xl bg-ai-subtle text-ai">
            <Sparkle aria-hidden="true" className="size-4" />
          </span>
          <div>
            <h2 className="text-sm font-semibold text-ink">AI workspace</h2>
            <p className="mt-0.5 text-[0.6875rem] text-ink-muted">Review every result before acting on it.</p>
          </div>
        </div>
        {working && <Badge variant="ai" size="sm" dot>Working</Badge>}
      </div>

      <div className="flex flex-col gap-3 p-4">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {canAnalyze && (
            <>
              <Button variant="secondary" size="sm" disabled={busy || working} onClick={() => requestMutation.mutate('analyze')}>
                Analyze ticket
              </Button>
              <Button variant="secondary" size="sm" disabled={busy || working} onClick={() => requestMutation.mutate('summarize')}>
                Summarize thread
              </Button>
            </>
          )}
          {canSuggest && (
            <Button
              variant="primary"
              size="sm"
              className={cn(canAnalyze && 'sm:col-span-2')}
              disabled={busy || working}
              leadingIcon={<Sparkle aria-hidden="true" />}
              onClick={() => requestMutation.mutate('suggest-response')}
            >
              Draft a reply
            </Button>
          )}
        </div>

        {analysesQuery.isPending ? (
          <div className="flex flex-col gap-2" aria-label="Loading AI results">
            <Skeleton className="h-14 rounded-xl" />
            <Skeleton className="h-14 rounded-xl" />
          </div>
        ) : analysesQuery.error ? (
          <ErrorState
            title="AI results couldn’t load"
            description={toApiError(analysesQuery.error).message}
            onRetry={() => { void analysesQuery.refetch() }}
          />
        ) : analyses.length ? (
          <div className="flex flex-col gap-2">
            {analyses.map((analysis) => {
              const lines = resultText(analysis)
              const statusVariant = analysis.status === 'completed' ? 'success' : analysis.status === 'failed' ? 'danger' : 'ai'
              return (
                <article key={analysis.id} className="rounded-xl border border-border-subtle bg-surface-sunken/50 px-3 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs font-semibold text-ink">{operationLabel(analysis.operation)}</p>
                    <Badge variant={statusVariant} size="sm" dot>{analysis.status}</Badge>
                  </div>
                  {lines.map((line) => <p key={line} className="mt-2 text-xs leading-relaxed text-ink-muted">{line}</p>)}
                  {analysis.error_message && <p className="mt-2 text-xs text-danger-text">{analysis.error_message}</p>}
                  {analysis.confidence !== null && (
                    <p className="mt-2 text-[0.625rem] text-ink-subtle">Confidence {Math.round(analysis.confidence * 100)}% · AI suggestion</p>
                  )}
                  <p className="mt-2 text-[0.625rem] text-ink-subtle">{analysis.provider} · {analysis.model}</p>
                </article>
              )
            })}
          </div>
        ) : (
          <p className="rounded-xl border border-dashed border-border-subtle px-3 py-4 text-center text-xs text-ink-muted">
            No AI results yet. Start with an analysis or request a reply draft.
          </p>
        )}

        {analysesQuery.data?.some((analysis) => analysis.status === 'failed') && (
          <Button
            variant="ghost"
            size="sm"
            leadingIcon={<ArrowClockwise aria-hidden="true" />}
            disabled={busy || working}
            onClick={() => requestMutation.mutate('analyze')}
          >
            Try analysis again
          </Button>
        )}
        <p className="text-[0.625rem] leading-relaxed text-ink-subtle">
          AI output can be wrong. Check it before updating the ticket or sending a response.
        </p>
      </div>
    </Card>
  )
}
