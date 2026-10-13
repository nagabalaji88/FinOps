/**
 * Analytics — how the board's agents are actually performing.
 *
 * Every figure is read from the platform's own telemetry: the execution ledger, the cost
 * records, the per-tool health counters and the approval log. Nothing is estimated here.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'motion/react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  BanknotesIcon,
  BoltIcon,
  CheckCircleIcon,
  ClockIcon,
  HandRaisedIcon,
} from '@heroicons/react/24/outline'
import {
  AnimatedNumber,
  Badge,
  Card,
  CardHeader,
  ChartTooltip,
  EmptyState,
  ErrorState,
  Meter,
  PageHeader,
  Reveal,
  SkeletonCard,
  Stagger,
  api,
  cn,
  formatCompact,
  formatCurrency,
  formatDuration,
  formatNumber,
  formatPercent,
  relativeTime,
  type Agent,
  type Overview,
} from '@finops/shared'
import { useBoard } from '@/lib/board'

interface TimeseriesPoint {
  timestamp: string
  total: number
  succeeded: number
  failed: number
  avg_latency_ms: number
  cost_usd: number
  tokens: number
}

interface CostSummary {
  window_days: number
  total_usd: number
  by_agent: { agent_key: string; cost_usd: number; calls: number }[]
  by_model: { model: string; cost_usd: number; calls: number }[]
  by_provider: { provider: string; cost_usd: number; calls: number }[]
  [key: string]: unknown
}

interface ToolMetric {
  name: string
  status: string
  total_calls: number
  failures: number
  timeouts: number
  success_rate_pct: number
  p50_latency_ms: number
  p95_latency_ms: number
  last_called_at: string | null
  last_error: string | null
}

const WINDOWS = [24, 72, 168]
const SERIES = ['#5b7cfa', '#34a382', '#c98b2a', '#b1558f', '#4aa3c7', '#8b7fd4']

const chartAxis = {
  stroke: 'rgb(var(--line))',
  tick: { fill: 'rgb(var(--ink-subtle))', fontSize: 10 },
  tickLine: false,
  axisLine: false,
}

function Headline({
  label,
  value,
  format,
  hint,
  share,
  tone = 'idle',
  icon,
}: {
  label: string
  value: number
  format: (value: number) => string
  hint?: string
  share?: number
  tone?: 'ok' | 'warn' | 'err' | 'info' | 'idle'
  icon: React.ReactNode
}) {
  const toneColor = {
    ok: 'text-state-ok',
    warn: 'text-state-warn',
    err: 'text-state-err',
    info: 'text-state-info',
    idle: 'text-ink',
  }[tone]
  return (
    <Reveal>
      <Card className="h-full" interactive>
        <div className="flex flex-col gap-2 px-5 py-4">
          <div className="flex items-center justify-between gap-2">
            <span className="metric-label">{label}</span>
            <span className="text-ink-subtle">{icon}</span>
          </div>
          <AnimatedNumber value={value} format={format} className={cn('metric-value', toneColor)} />
          {hint ? <span className="text-xs text-ink-muted">{hint}</span> : null}
          {share !== undefined ? (
            <Meter value={share} tone={tone === 'idle' ? 'info' : tone} className="mt-1" />
          ) : null}
        </div>
      </Card>
    </Reveal>
  )
}

export default function Analytics() {
  const [hours, setHours] = useState(24)
  const [board] = useBoard()

  const overview = useQuery({
    queryKey: ['overview'],
    queryFn: () => api.get<Overview>('/dashboard/overview'),
    refetchInterval: 20_000,
  })
  const series = useQuery({
    queryKey: ['timeseries', hours],
    queryFn: () => api.get<{ series: TimeseriesPoint[] }>('/dashboard/timeseries', { hours }),
    refetchInterval: 60_000,
  })
  const agents = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.get<Agent[]>('/agents'),
    refetchInterval: 60_000,
  })
  const costs = useQuery({
    queryKey: ['costs', hours],
    queryFn: () => api.get<CostSummary>('/costs/summary', { days: Math.max(1, Math.round(hours / 24)) }),
    refetchInterval: 120_000,
  })
  const tools = useQuery({
    queryKey: ['tool-metrics'],
    queryFn: () => api.get<ToolMetric[]>('/platform-metrics/tools'),
    refetchInterval: 60_000,
  })

  const boardAgents = useMemo(
    () => (agents.data ?? []).filter((agent) => board.includes(agent.key)),
    [agents.data, board],
  )

  /** Tool health restricted to the tools the board's agents can actually call. */
  const boardTools = useMemo(() => {
    const names = new Set(boardAgents.flatMap((agent) => agent.tools))
    return (tools.data ?? [])
      .filter((tool) => names.has(tool.name) && tool.total_calls > 0)
      .sort((a, b) => b.total_calls - a.total_calls)
  }, [tools.data, boardAgents])

  const chart = useMemo(
    () =>
      (series.data?.series ?? []).map((point) => ({
        ...point,
        label: new Date(point.timestamp).toLocaleTimeString('en-GB', {
          hour: '2-digit',
          minute: '2-digit',
        }),
      })),
    [series.data],
  )

  const modelSplit = useMemo(
    () => (costs.data?.by_model ?? []).filter((row) => row.cost_usd > 0).slice(0, 6),
    [costs.data],
  )

  if (overview.isError) return <ErrorState error={overview.error} retry={() => overview.refetch()} />

  const data = overview.data

  return (
    <div className="space-y-6">
      <PageHeader
        title="Analytics"
        description="Throughput, reliability, latency and spend for the agents on the Execute board."
        actions={
          <>
            <div className="flex items-center gap-1 rounded-xl border border-line/70 bg-surface-muted/60 p-1">
              {WINDOWS.map((window) => (
                <button
                  key={window}
                  type="button"
                  onClick={() => setHours(window)}
                  className="relative rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                >
                  {hours === window ? (
                    <motion.span
                      layoutId="analytics-window"
                      className="absolute inset-0 rounded-lg bg-surface-raised shadow-glass"
                      transition={{ type: 'spring', stiffness: 380, damping: 32 }}
                    />
                  ) : null}
                  <span className={hours === window ? 'relative text-ink' : 'relative text-ink-muted'}>
                    {window < 48 ? `${window}h` : `${Math.round(window / 24)}d`}
                  </span>
                </button>
              ))}
            </div>
            {data ? (
              <Badge tone="idle" dot>
                Updated {relativeTime(data.generated_at)}
              </Badge>
            ) : null}
          </>
        }
      />

      {!data ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <SkeletonCard key={index} rows={2} />
          ))}
        </div>
      ) : (
        <>
          <Stagger className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Headline
              label="Executions (24h)"
              value={data.executions.last_24h ?? 0}
              format={(value) => formatCompact(Math.round(value))}
              hint={`${formatNumber(data.executions.total_all_time ?? 0)} all time`}
              icon={<BoltIcon className="h-4 w-4" />}
            />
            <Headline
              label="Success rate"
              value={data.executions.success_rate_pct ?? 0}
              format={(value) => formatPercent(value)}
              hint={`${data.executions.succeeded_24h ?? 0} succeeded · ${data.executions.failed_24h ?? 0} failed`}
              share={data.executions.success_rate_pct ?? 0}
              tone={
                (data.executions.success_rate_pct ?? 0) >= 95
                  ? 'ok'
                  : (data.executions.success_rate_pct ?? 0) >= 85
                    ? 'warn'
                    : 'err'
              }
              icon={<CheckCircleIcon className="h-4 w-4" />}
            />
            <Headline
              label="Average latency"
              value={data.executions.avg_latency_ms ?? 0}
              format={(value) => formatDuration(value)}
              hint={`${formatNumber(data.tokens.total_24h ?? 0)} tokens in 24h`}
              icon={<ClockIcon className="h-4 w-4" />}
            />
            <Headline
              label="Spend today"
              value={data.cost.today_usd ?? 0}
              format={(value) => formatCurrency(value, 2)}
              hint={`Budget ${formatCurrency(data.cost.daily_budget_usd ?? 0, 0)}`}
              share={data.cost.daily_budget_used_pct ?? 0}
              tone={(data.cost.daily_budget_used_pct ?? 0) > 80 ? 'warn' : 'idle'}
              icon={<BanknotesIcon className="h-4 w-4" />}
            />
          </Stagger>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader
                title="Throughput"
                subtitle={`Succeeded and failed executions, last ${hours < 48 ? `${hours} hours` : `${Math.round(hours / 24)} days`}`}
              />
              <div className="h-[260px] px-2 pb-4 pt-2">
                {chart.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chart} margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
                      <defs>
                        <linearGradient id="ok" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="rgb(var(--state-ok))" stopOpacity={0.36} />
                          <stop offset="100%" stopColor="rgb(var(--state-ok))" stopOpacity={0} />
                        </linearGradient>
                        <linearGradient id="err" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="rgb(var(--state-err))" stopOpacity={0.36} />
                          <stop offset="100%" stopColor="rgb(var(--state-err))" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="rgb(var(--line))" strokeDasharray="2 4" vertical={false} />
                      <XAxis dataKey="label" {...chartAxis} minTickGap={28} />
                      <YAxis {...chartAxis} width={34} allowDecimals={false} />
                      <ReTooltip content={<ChartTooltip />} />
                      <Area
                        type="monotone"
                        dataKey="succeeded"
                        name="Succeeded"
                        stroke="rgb(var(--state-ok))"
                        fill="url(#ok)"
                        strokeWidth={1.6}
                        animationDuration={800}
                      />
                      <Area
                        type="monotone"
                        dataKey="failed"
                        name="Failed"
                        stroke="rgb(var(--state-err))"
                        fill="url(#err)"
                        strokeWidth={1.6}
                        animationDuration={1000}
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <EmptyState title="No executions in this window" />
                )}
              </div>
            </Card>

            <Card>
              <CardHeader title="Spend by model" subtitle="Attributed per model call" />
              <div className="h-[260px] px-2 pb-4 pt-2">
                {modelSplit.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={modelSplit}
                        dataKey="cost_usd"
                        nameKey="model"
                        innerRadius={48}
                        outerRadius={78}
                        paddingAngle={2}
                        animationDuration={900}
                      >
                        {modelSplit.map((entry, index) => (
                          <Cell key={entry.model} fill={SERIES[index % SERIES.length]} />
                        ))}
                      </Pie>
                      <ReTooltip content={<ChartTooltip format={(value) => formatCurrency(Number(value), 4)} />} />
                    </PieChart>
                  </ResponsiveContainer>
                ) : (
                  <EmptyState
                    title="No model spend recorded"
                    description="Spend appears once an agent has completed a run against a configured provider."
                  />
                )}
              </div>
            </Card>
          </div>

          {/* Per-agent breakdown for the board */}
          <Card className="overflow-hidden">
            <CardHeader title="Board agents" subtitle="Reliability and cost for the two agents on the Execute screen" />
            <div className="scroll-x px-5 pb-5 pt-4">
              <table className="w-full min-w-[44rem] text-xs">
                <thead>
                  <tr className="border-b border-line/70 text-left">
                    {['Agent', 'Runs 24h', 'Succeeded', 'Failed', 'Success', 'Avg latency', 'Cost today', 'Pending approvals'].map(
                      (column, index) => (
                        <th
                          key={column}
                          className={cn('metric-label px-3 py-2 font-medium', index > 0 && 'text-right')}
                        >
                          {column}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {boardAgents.map((agent, index) => (
                    <motion.tr
                      key={agent.key}
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: index * 0.06, duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
                      className="table-row"
                    >
                      <td className="px-3 py-2.5 font-medium text-ink">{agent.name}</td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                        {formatNumber(agent.metrics.executions_24h)}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                        {formatNumber(agent.metrics.succeeded_24h)}
                      </td>
                      <td
                        className={cn(
                          'px-3 py-2.5 text-right tabular-nums',
                          agent.metrics.failed_24h > 0 ? 'text-state-err' : 'text-ink-muted',
                        )}
                      >
                        {formatNumber(agent.metrics.failed_24h)}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                        {formatPercent(agent.metrics.success_rate_pct)}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                        {formatDuration(agent.metrics.avg_latency_ms)}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                        {formatCurrency(agent.metrics.cost_today_usd, 4)}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums">
                        {agent.metrics.pending_approvals > 0 ? (
                          <span className="inline-flex items-center gap-1 text-state-warn">
                            <HandRaisedIcon className="h-3.5 w-3.5" />
                            {agent.metrics.pending_approvals}
                          </span>
                        ) : (
                          <span className="text-ink-muted">0</span>
                        )}
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          {/* Tool reliability */}
          <Card>
            <CardHeader
              title="Tool reliability"
              subtitle="Measured from every invocation these agents have made"
            />
            <div className="h-[260px] px-2 pb-4 pt-2">
              {boardTools.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={boardTools.slice(0, 12)}
                    margin={{ top: 4, right: 12, bottom: 0, left: 0 }}
                    layout="vertical"
                  >
                    <CartesianGrid stroke="rgb(var(--line))" strokeDasharray="2 4" horizontal={false} />
                    <XAxis type="number" {...chartAxis} allowDecimals={false} />
                    <YAxis type="category" dataKey="name" {...chartAxis} width={150} />
                    <ReTooltip content={<ChartTooltip />} />
                    <Bar
                      dataKey="total_calls"
                      name="Calls"
                      radius={[0, 4, 4, 0]}
                      animationDuration={900}
                    >
                      {boardTools.slice(0, 12).map((tool) => (
                        <Cell
                          key={tool.name}
                          fill={
                            tool.success_rate_pct >= 99
                              ? 'rgb(var(--state-ok))'
                              : tool.success_rate_pct >= 90
                                ? 'rgb(var(--state-warn))'
                                : 'rgb(var(--state-err))'
                          }
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <EmptyState
                  title="No tool calls recorded yet"
                  description="Run an agent from the Execute screen and its tool invocations appear here."
                />
              )}
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
