import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type Execution, Badge, Card, EmptyState, ErrorState, PageHeader, Skeleton, StatusDot, formatCurrency, formatDateTime, formatDuration, formatNumber, titleCase } from '@finops/shared'

const STATUSES = ['all', 'running', 'queued', 'awaiting_approval', 'succeeded', 'failed', 'cancelled']

export default function Executions() {
  const [searchParams, setSearchParams] = useSearchParams()
  const agent = searchParams.get('agent') ?? ''
  const status = searchParams.get('status') ?? 'all'
  const [page, setPage] = useState(0)
  const limit = 50

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['executions', agent, status, page],
    queryFn: () =>
      api.get<{ total: number; items: Execution[] }>('/executions', {
        agent_key: agent || undefined,
        status: status === 'all' ? undefined : status,
        limit,
        offset: page * limit,
      }),
    refetchInterval: 10_000,
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div className="space-y-5">
      <PageHeader
        title="Executions"
        description="Every agent run with its trace, cost and outcome."
        actions={
          <div className="flex flex-wrap gap-2">
            <input
              className="input w-44"
              placeholder="Agent key"
              value={agent}
              onChange={(event) => {
                setPage(0)
                setSearchParams({ agent: event.target.value, status })
              }}
              aria-label="Filter by agent"
            />
            <select
              className="input w-44"
              value={status}
              onChange={(event) => {
                setPage(0)
                setSearchParams({ agent, status: event.target.value })
              }}
              aria-label="Filter by status"
            >
              {STATUSES.map((value) => (
                <option key={value} value={value}>
                  {titleCase(value)}
                </option>
              ))}
            </select>
          </div>
        }
      />

      <Card className="overflow-hidden">
        <div className="scroll-x">
          <table className="w-full min-w-[980px] text-xs">
            <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
              <tr>
                {['Agent', 'Status', 'Started', 'Latency', 'Tokens', 'Cost', 'Tools', 'Model', 'User'].map((header) => (
                  <th key={header} className="px-4 py-2.5 text-left font-medium">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {isLoading
                ? Array.from({ length: 8 }).map((_, index) => (
                    <tr key={index}>
                      <td colSpan={9} className="px-4 py-2">
                        <Skeleton className="h-5 w-full" />
                      </td>
                    </tr>
                  ))
                : data?.items.map((execution) => (
                    <tr key={execution.id} className="table-row">
                      <td className="px-4 py-2.5">
                        <Link to={`/executions/${execution.id}`} className="flex items-center gap-2 hover:underline">
                          <StatusDot status={execution.status} pulse={execution.status === 'running'} />
                          <span className="font-medium">{titleCase(execution.agent_key)}</span>
                        </Link>
                        <span className="ml-4 font-mono text-2xs text-ink-subtle">{execution.id.slice(0, 8)}</span>
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge status={execution.status}>{execution.status.replace('_', ' ')}</Badge>
                      </td>
                      <td className="px-4 py-2.5 text-ink-muted">{formatDateTime(execution.created_at)}</td>
                      <td className="px-4 py-2.5 tabular-nums">{formatDuration(execution.latency_ms)}</td>
                      <td className="px-4 py-2.5 tabular-nums">{formatNumber(execution.tokens.total)}</td>
                      <td className="px-4 py-2.5 tabular-nums">{formatCurrency(execution.cost_usd, 4)}</td>
                      <td className="px-4 py-2.5 tabular-nums">{execution.tool_call_count}</td>
                      <td className="px-4 py-2.5 font-mono text-2xs text-ink-muted">{execution.model ?? '—'}</td>
                      <td className="px-4 py-2.5 text-ink-muted">{execution.user_email ?? 'system'}</td>
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>
        {!isLoading && data?.items.length === 0 ? (
          <EmptyState title="No executions" description="Run an agent to see its execution here." />
        ) : null}
        {data && data.total > limit ? (
          <div className="flex items-center justify-between border-t border-line/60 px-4 py-3 text-2xs text-ink-muted">
            <span>
              {page * limit + 1}–{Math.min((page + 1) * limit, data.total)} of {formatNumber(data.total)}
            </span>
            <div className="flex gap-2">
              <button
                className="btn-ghost px-2 py-1 text-2xs"
                disabled={page === 0}
                onClick={() => setPage((current) => Math.max(current - 1, 0))}
              >
                Previous
              </button>
              <button
                className="btn-ghost px-2 py-1 text-2xs"
                disabled={(page + 1) * limit >= data.total}
                onClick={() => setPage((current) => current + 1)}
              >
                Next
              </button>
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  )
}
