import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'motion/react'
import {
  ArrowPathIcon,
  ChartBarIcon,
  Cog6ToothIcon,
  DocumentTextIcon,
  PauseIcon,
  PlayIcon,
  ShareIcon,
  StopIcon,
} from '@heroicons/react/24/outline'
import { api, type Agent, Badge, Button, Card, EmptyState, ErrorState, PageHeader, SkeletonCard, StatusDot, Tooltip, useAuth, useToasts, cn, formatCurrency, formatDuration, formatNumber, formatPercent, relativeTime } from '@finops/shared'
import { ExecuteDialog } from '@/components/agents/ExecuteDialog'

export default function Agents() {
  const queryClient = useQueryClient()
  const { can } = useAuth()
  const push = useToasts((state) => state.push)
  const [filter, setFilter] = useState<'all' | 'implemented' | 'coming_soon'>('all')
  const [search, setSearch] = useState('')
  const [executing, setExecuting] = useState<Agent | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.get<Agent[]>('/agents'),
    refetchInterval: 20_000,
  })

  const lifecycle = useMutation({
    mutationFn: ({ key, action }: { key: string; action: string }) =>
      api.post(`/agents/${key}/lifecycle`, { action }),
    onSuccess: (_result, variables) => {
      push({ title: `Agent ${variables.action}d`, tone: 'ok' })
      void queryClient.invalidateQueries({ queryKey: ['agents'] })
    },
    onError: (mutationError) =>
      push({ title: 'Lifecycle change failed', description: (mutationError as Error).message, tone: 'err' }),
  })

  const agents = useMemo(() => {
    const rows = data ?? []
    return rows
      .filter((agent) => (filter === 'all' ? true : agent.availability === filter))
      .filter((agent) =>
        search
          ? `${agent.name} ${agent.description} ${agent.category} ${agent.tags.join(' ')}`
              .toLowerCase()
              .includes(search.toLowerCase())
          : true,
      )
      .sort((a, b) => {
        if (a.availability !== b.availability) return a.availability === 'implemented' ? -1 : 1
        return a.name.localeCompare(b.name)
      })
  }, [data, filter, search])

  const counts = useMemo(
    () => ({
      all: data?.length ?? 0,
      implemented: data?.filter((agent) => agent.availability === 'implemented').length ?? 0,
      coming_soon: data?.filter((agent) => agent.availability === 'coming_soon').length ?? 0,
    }),
    [data],
  )

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div className="space-y-6">
      <PageHeader
        title="Agents"
        description="Every agent registered on the platform, with live health, cost and execution state."
        actions={
          <div className="flex items-center gap-2">
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Filter agents…"
              className="input w-48 sm:w-60"
              aria-label="Filter agents"
            />
            <Button size="sm" onClick={() => refetch()}>
              <ArrowPathIcon className="h-3.5 w-3.5" />
              Refresh
            </Button>
          </div>
        }
      />

      <div className="flex gap-1 rounded-xl border border-line/70 bg-surface-muted/60 p-1 sm:w-fit">
        {(
          [
            ['all', 'All'],
            ['implemented', 'Implemented'],
            ['coming_soon', 'Coming soon'],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            onClick={() => setFilter(value)}
            className={cn(
              'flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors sm:flex-none',
              filter === value ? 'bg-surface-raised text-ink shadow-glass' : 'text-ink-muted hover:text-ink',
            )}
          >
            {label}
            <span className="rounded-full bg-accent-soft px-1.5 text-2xs tabular-nums">{counts[value]}</span>
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <SkeletonCard key={index} rows={4} />
          ))}
        </div>
      ) : agents.length === 0 ? (
        <Card>
          <EmptyState title="No agents match" description="Adjust the filter or search term." />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {agents.map((agent, index) => {
            const comingSoon = agent.availability === 'coming_soon'
            const metrics = agent.metrics ?? ({} as Agent['metrics'])
            return (
              <motion.div
                key={agent.key}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(index * 0.03, 0.3), duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
              >
                <Card interactive={!comingSoon} className={cn('flex h-full flex-col', comingSoon && 'opacity-75')}>
                  <div className="flex items-start justify-between gap-3 px-5 pt-5">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        {!comingSoon && (
                          <StatusDot
                            status={metrics.health ?? agent.lifecycle_state}
                            pulse={(metrics.running ?? 0) > 0}
                          />
                        )}
                        <h3 className="truncate text-sm font-semibold tracking-tight">{agent.name}</h3>
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-ink-muted">{agent.description}</p>
                    </div>
                    {comingSoon ? (
                      <Badge tone="idle">Coming soon</Badge>
                    ) : (
                      <Badge status={agent.lifecycle_state}>{agent.lifecycle_state}</Badge>
                    )}
                  </div>

                  <div className="mt-3 flex flex-wrap gap-1.5 px-5">
                    <span className="chip">{agent.category}</span>
                    {comingSoon && agent.planned_quarter ? (
                      <span className="chip">Planned {agent.planned_quarter}</span>
                    ) : null}
                    {!comingSoon && agent.model ? <span className="chip font-mono">{agent.model}</span> : null}
                    {!comingSoon ? <span className="chip">{agent.tools.length} tools</span> : null}
                    {agent.requires_approval ? <span className="chip">HITL</span> : null}
                  </div>

                  {comingSoon ? (
                    <div className="mt-auto px-5 pb-5 pt-4">
                      <p className="text-2xs text-ink-subtle">
                        Owned by {agent.owner} · {agent.department}
                      </p>
                    </div>
                  ) : (
                    <>
                      <dl className="mt-4 grid grid-cols-3 gap-y-3 border-t border-line/60 px-5 py-4 text-xs">
                        <div>
                          <dt className="metric-label">Executions</dt>
                          <dd className="mt-0.5 font-semibold tabular-nums">
                            {formatNumber(metrics.executions_total ?? 0)}
                          </dd>
                        </div>
                        <div>
                          <dt className="metric-label">Avg latency</dt>
                          <dd className="mt-0.5 font-semibold tabular-nums">
                            {formatDuration(metrics.avg_latency_ms ?? 0)}
                          </dd>
                        </div>
                        <div>
                          <dt className="metric-label">Cost today</dt>
                          <dd className="mt-0.5 font-semibold tabular-nums">
                            {formatCurrency(metrics.cost_today_usd ?? 0, 3)}
                          </dd>
                        </div>
                        <div>
                          <dt className="metric-label">Error rate</dt>
                          <dd
                            className={cn(
                              'mt-0.5 font-semibold tabular-nums',
                              (metrics.error_rate_pct ?? 0) > 5 && 'text-state-err',
                            )}
                          >
                            {formatPercent(metrics.error_rate_pct ?? 0)}
                          </dd>
                        </div>
                        <div>
                          <dt className="metric-label">Retries</dt>
                          <dd className="mt-0.5 font-semibold tabular-nums">{metrics.retry_count_24h ?? 0}</dd>
                        </div>
                        <div>
                          <dt className="metric-label">Incidents</dt>
                          <dd
                            className={cn(
                              'mt-0.5 font-semibold tabular-nums',
                              (metrics.open_incidents ?? 0) > 0 && 'text-state-err',
                            )}
                          >
                            {metrics.open_incidents ?? 0}
                          </dd>
                        </div>
                      </dl>

                      <div className="px-5 pb-3 text-2xs text-ink-subtle">
                        <p className="truncate">
                          Owner {agent.owner}
                          {metrics.last_execution
                            ? ` · last run ${relativeTime(metrics.last_execution.finished_at)} (${metrics.last_execution.status})`
                            : ' · never executed'}
                        </p>
                        {metrics.current_execution ? (
                          <Link
                            to={`/executions/${metrics.current_execution.id}`}
                            className="mt-1 inline-flex items-center gap-1.5 text-state-info hover:underline"
                          >
                            <StatusDot status="running" pulse />
                            Execution in progress
                          </Link>
                        ) : null}
                      </div>

                      <div className="mt-auto flex flex-wrap items-center gap-1.5 border-t border-line/60 px-4 py-3">
                        {can('agent:execute') ? (
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => setExecuting(agent)}
                            disabled={agent.lifecycle_state !== 'active'}
                          >
                            <PlayIcon className="h-3.5 w-3.5" />
                            Execute
                          </Button>
                        ) : null}
                        {can('agent:lifecycle') ? (
                          <>
                            <Tooltip content={agent.lifecycle_state === 'paused' ? 'Resume' : 'Pause'}>
                              <Button
                                size="sm"
                                variant="ghost"
                                aria-label={agent.lifecycle_state === 'paused' ? 'Resume agent' : 'Pause agent'}
                                onClick={() =>
                                  lifecycle.mutate({
                                    key: agent.key,
                                    action: agent.lifecycle_state === 'paused' ? 'resume' : 'pause',
                                  })
                                }
                              >
                                {agent.lifecycle_state === 'paused' ? (
                                  <PlayIcon className="h-3.5 w-3.5" />
                                ) : (
                                  <PauseIcon className="h-3.5 w-3.5" />
                                )}
                              </Button>
                            </Tooltip>
                            <Tooltip content={agent.lifecycle_state === 'disabled' ? 'Enable' : 'Disable'}>
                              <Button
                                size="sm"
                                variant="ghost"
                                aria-label="Disable agent"
                                onClick={() =>
                                  lifecycle.mutate({
                                    key: agent.key,
                                    action: agent.lifecycle_state === 'disabled' ? 'enable' : 'disable',
                                  })
                                }
                              >
                                <StopIcon className="h-3.5 w-3.5" />
                              </Button>
                            </Tooltip>
                          </>
                        ) : null}
                        <div className="ml-auto flex items-center gap-0.5">
                          <Tooltip content="Settings & configuration">
                            <Link
                              to={`/agents/${agent.key}?tab=config`}
                              className="rounded-lg p-1.5 text-ink-subtle transition-colors hover:bg-accent-soft hover:text-ink"
                              aria-label="Agent settings"
                            >
                              <Cog6ToothIcon className="h-4 w-4" />
                            </Link>
                          </Tooltip>
                          <Tooltip content="Logs">
                            <Link
                              to={`/logs?agent=${agent.key}`}
                              className="rounded-lg p-1.5 text-ink-subtle transition-colors hover:bg-accent-soft hover:text-ink"
                              aria-label="Agent logs"
                            >
                              <DocumentTextIcon className="h-4 w-4" />
                            </Link>
                          </Tooltip>
                          <Tooltip content="Traces">
                            <Link
                              to={`/executions?agent=${agent.key}`}
                              className="rounded-lg p-1.5 text-ink-subtle transition-colors hover:bg-accent-soft hover:text-ink"
                              aria-label="Agent traces"
                            >
                              <ShareIcon className="h-4 w-4" />
                            </Link>
                          </Tooltip>
                          <Tooltip content="Metrics">
                            <Link
                              to={`/agents/${agent.key}?tab=metrics`}
                              className="rounded-lg p-1.5 text-ink-subtle transition-colors hover:bg-accent-soft hover:text-ink"
                              aria-label="Agent metrics"
                            >
                              <ChartBarIcon className="h-4 w-4" />
                            </Link>
                          </Tooltip>
                        </div>
                      </div>
                    </>
                  )}
                </Card>
              </motion.div>
            )
          })}
        </div>
      )}

      {executing ? <ExecuteDialog agent={executing} onClose={() => setExecuting(null)} /> : null}
    </div>
  )
}
