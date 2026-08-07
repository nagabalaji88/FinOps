/**
 * Execute — the board.
 *
 * Two agents are on the console at a time. Both are fully implemented and run through the
 * same engine, tools, guardrails and approval gates the platform uses everywhere else;
 * nothing here is a demonstration path. Either slot can be swapped for another implemented
 * agent, and the pair is remembered for this browser.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import {
  ArrowsRightLeftIcon,
  CheckIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  HandRaisedIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline'
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  PageHeader,
  Reveal,
  SkeletonCard,
  Stagger,
  StatusDot,
  api,
  cn,
  formatDuration,
  formatNumber,
  formatPercent,
  relativeTime,
  type Agent,
  type Execution,
} from '@finops/shared'
import { RunConsole } from '@/components/execute/RunConsole'
import { resolveBoard, useBoard } from '@/lib/board'

function AgentSummary({
  agent,
  selected,
  onSelect,
  swappable,
  onSwap,
}: {
  agent: Agent
  selected: boolean
  onSelect: () => void
  swappable: Agent[]
  onSwap: (agentKey: string) => void
}) {
  const [picking, setPicking] = useState(false)
  const metrics = agent.metrics
  const paused = agent.lifecycle_state !== 'active'

  return (
    <Reveal>
      <Card
        interactive
        className={cn(
          'relative h-full cursor-pointer p-5 transition-colors',
          selected && 'ring-1 ring-ink/20',
        )}
        onClick={onSelect}
      >
        {selected ? (
          <motion.span
            layoutId="board-selection"
            className="pointer-events-none absolute inset-0 rounded-2xl bg-accent-soft/50"
            transition={{ type: 'spring', stiffness: 380, damping: 34 }}
          />
        ) : null}

        <div className="relative">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <StatusDot status={paused ? 'paused' : 'active'} pulse={metrics.running > 0} />
                <h3 className="truncate text-sm font-semibold tracking-tight">{agent.name}</h3>
              </div>
              <p className="mt-1 line-clamp-2 text-xs text-ink-muted">{agent.description}</p>
            </div>
            {swappable.length ? (
              <div className="relative shrink-0">
                <button
                  type="button"
                  aria-label="Swap agent"
                  onClick={(event) => {
                    event.stopPropagation()
                    setPicking((value) => !value)
                  }}
                  className="rounded-lg p-1.5 text-ink-subtle transition-colors hover:bg-accent-soft hover:text-ink"
                >
                  <ArrowsRightLeftIcon className="h-4 w-4" />
                </button>
                {picking ? (
                  <motion.div
                    initial={{ opacity: 0, y: -4, scale: 0.98 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    className="absolute right-0 top-9 z-20 w-60 overflow-hidden rounded-xl border border-line bg-surface-raised p-1 shadow-glass-lg"
                    onClick={(event) => event.stopPropagation()}
                  >
                    {swappable.map((candidate) => (
                      <button
                        key={candidate.key}
                        type="button"
                        onClick={() => {
                          onSwap(candidate.key)
                          setPicking(false)
                        }}
                        className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs text-ink-muted transition-colors hover:bg-accent-soft hover:text-ink"
                      >
                        {candidate.key === agent.key ? (
                          <CheckIcon className="h-3.5 w-3.5 shrink-0" />
                        ) : (
                          <span className="h-3.5 w-3.5 shrink-0" />
                        )}
                        <span className="truncate">{candidate.name}</span>
                      </button>
                    ))}
                  </motion.div>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-1.5">
            <Badge tone="idle">
              <WrenchScrewdriverIcon className="h-3 w-3" />
              {agent.tools.length} tools
            </Badge>
            {agent.requires_approval ? (
              <Badge tone="warn">
                <HandRaisedIcon className="h-3 w-3" />
                human gate
              </Badge>
            ) : null}
            <Badge tone={metrics.health === 'healthy' ? 'ok' : metrics.health === 'degraded' ? 'warn' : 'err'}>
              {metrics.health}
            </Badge>
            {paused ? <Badge tone="warn">{agent.lifecycle_state}</Badge> : null}
          </div>

          <dl className="mt-4 grid grid-cols-3 gap-3 border-t border-line/60 pt-3">
            <div>
              <dt className="metric-label">Runs 24h</dt>
              <dd className="mt-0.5 text-sm font-semibold tabular-nums">
                {formatNumber(metrics.executions_24h)}
              </dd>
            </div>
            <div>
              <dt className="metric-label">Success</dt>
              <dd
                className={cn(
                  'mt-0.5 text-sm font-semibold tabular-nums',
                  metrics.success_rate_pct < 90 && metrics.executions_24h > 0 && 'text-state-warn',
                )}
              >
                {formatPercent(metrics.success_rate_pct)}
              </dd>
            </div>
            <div>
              <dt className="metric-label">Latency</dt>
              <dd className="mt-0.5 text-sm font-semibold tabular-nums">
                {formatDuration(metrics.avg_latency_ms)}
              </dd>
            </div>
          </dl>
        </div>
      </Card>
    </Reveal>
  )
}

export default function Execute() {
  const [board, setSlot] = useBoard()
  const [activeSlot, setActiveSlot] = useState(0)

  const agents = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.get<Agent[]>('/agents'),
    refetchInterval: 30_000,
  })

  const implemented = useMemo(
    () => (agents.data ?? []).filter((agent) => agent.availability === 'implemented'),
    [agents.data],
  )

  const slots = useMemo(() => resolveBoard(implemented, board), [implemented, board])

  const active = slots[activeSlot] ?? slots[0] ?? null

  const recent = useQuery({
    queryKey: ['recent-runs', active?.key],
    queryFn: () =>
      api.get<{ items: Execution[] }>('/executions', { agent_key: active!.key, limit: 6, since_hours: 168 }),
    enabled: Boolean(active),
    refetchInterval: 20_000,
  })

  useEffect(() => {
    if (activeSlot >= slots.length && slots.length) setActiveSlot(0)
  }, [slots.length, activeSlot])

  if (agents.isError) return <ErrorState error={agents.error} retry={() => agents.refetch()} />

  return (
    <div className="space-y-6">
      <PageHeader
        title="Execute"
        description="Run a production agent against the banking system of record. Live trace, human approval and full cost attribution."
        actions={
          active ? (
            <Badge tone="idle" dot>
              {active.name} · v{active.version}
            </Badge>
          ) : null
        }
      />

      {agents.isLoading ? (
        <div className="grid gap-4 md:grid-cols-2">
          <SkeletonCard rows={3} />
          <SkeletonCard rows={3} />
        </div>
      ) : slots.length === 0 ? (
        <EmptyState
          title="No implemented agents are registered"
          description="Seed the platform, or check that the agent registry bootstrapped."
        />
      ) : (
        <>
          <Stagger className="grid gap-4 md:grid-cols-2">
            {slots.map((agent, index) => (
              <AgentSummary
                key={agent.key}
                agent={agent}
                selected={index === activeSlot}
                onSelect={() => setActiveSlot(index)}
                swappable={implemented}
                onSwap={(agentKey) => {
                  setSlot(index, agentKey)
                  setActiveSlot(index)
                }}
              />
            ))}
          </Stagger>

          {active ? <RunConsole key={active.key} agent={active} /> : null}

          <Card className="overflow-hidden">
            <div className="flex items-center justify-between gap-3 border-b border-line/60 px-5 py-3.5">
              <h3 className="text-sm font-semibold tracking-tight">Recent runs</h3>
              <span className="text-2xs text-ink-subtle">{active?.name} · last 7 days</span>
            </div>
            {recent.data?.items.length ? (
              <ul className="divide-y divide-line/50">
                {recent.data.items.map((execution, index) => (
                  <motion.li
                    key={execution.id}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: index * 0.03, duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
                    className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-2.5 text-xs"
                  >
                    <Badge status={execution.status}>{execution.status.replace('_', ' ')}</Badge>
                    <span className="font-mono text-2xs text-ink-subtle">{execution.id.slice(0, 8)}</span>
                    <span className="flex items-center gap-1 text-ink-muted">
                      <ClockIcon className="h-3.5 w-3.5" />
                      {formatDuration(execution.latency_ms)}
                    </span>
                    <span className="tabular-nums text-ink-muted">
                      {execution.tool_call_count} tools · {formatNumber(execution.tokens.total)} tokens
                    </span>
                    {execution.error ? (
                      <span className="flex min-w-0 items-center gap-1 text-state-err">
                        <ExclamationTriangleIcon className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">{execution.error}</span>
                      </span>
                    ) : null}
                    <span className="ml-auto text-2xs text-ink-subtle">
                      {relativeTime(execution.created_at)}
                    </span>
                  </motion.li>
                ))}
              </ul>
            ) : (
              <p className="px-5 py-8 text-center text-xs text-ink-subtle">
                No runs yet for {active?.name}.
              </p>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
