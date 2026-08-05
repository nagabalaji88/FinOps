import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
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
  ArrowTrendingUpIcon,
  BoltIcon,
  CheckCircleIcon,
  ClockIcon,
  CpuChipIcon,
  CurrencyDollarIcon,
  ExclamationTriangleIcon,
  QueueListIcon,
} from '@heroicons/react/24/outline'
import { api, type Overview as OverviewData } from '@/lib/api'
import {
  AnimatedNumber,
  Badge,
  Card,
  CardHeader,
  ErrorState,
  Meter,
  PageHeader,
  SkeletonCard,
  Stat,
} from '@/components/ui'
import {
  formatBytes,
  formatCompact,
  formatCurrency,
  formatDuration,
  formatNumber,
  formatPercent,
  relativeTime,
} from '@/lib/utils'

interface AiUsage {
  window_days: number
  totals: Record<string, number>
  models: {
    model: string
    display_name: string
    provider: string
    category: string
    calls: number
    tokens_input: number
    tokens_output: number
    tokens_cached: number
    cost_usd: number
    avg_latency_ms: number
  }[]
  distribution: { model: string; share_pct: number }[]
  cache: Record<string, unknown>
  vector_search: { queries: number; avg_latency_ms: number }
}

interface TimeseriesPoint {
  timestamp: string
  total: number
  succeeded: number
  failed: number
  avg_latency_ms: number
  cost_usd: number
  tokens: number
}

const SERIES_COLORS = ['#5b7cfa', '#34a382', '#c98b2a', '#b1558f', '#4aa3c7', '#8b7fd4']

const chartAxis = {
  stroke: 'rgb(var(--line))',
  tick: { fill: 'rgb(var(--ink-subtle))', fontSize: 10 },
  tickLine: false,
  axisLine: false,
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-line bg-surface-raised px-2.5 py-2 text-2xs shadow-glass-lg">
      <p className="mb-1 font-medium text-ink">{label}</p>
      {payload.map((entry: any) => (
        <p key={entry.dataKey} className="flex items-center gap-2 text-ink-muted">
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: entry.color }} />
          {entry.name}: <span className="tabular-nums text-ink">{entry.value}</span>
        </p>
      ))}
    </div>
  )
}

export default function Overview() {
  const overview = useQuery({
    queryKey: ['overview'],
    queryFn: () => api.get<OverviewData>('/dashboard/overview'),
    refetchInterval: 15_000,
  })
  const usage = useQuery({
    queryKey: ['ai-usage'],
    queryFn: () => api.get<AiUsage>('/dashboard/ai-usage', { days: 7 }),
    refetchInterval: 60_000,
  })
  const series = useQuery({
    queryKey: ['timeseries'],
    queryFn: () => api.get<{ series: TimeseriesPoint[] }>('/dashboard/timeseries', { hours: 24 }),
    refetchInterval: 60_000,
  })

  if (overview.isError) return <ErrorState error={overview.error} retry={() => overview.refetch()} />

  const data = overview.data
  const chartData =
    series.data?.series.map((point) => ({
      ...point,
      label: new Date(point.timestamp).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }),
    })) ?? []

  return (
    <div className="space-y-6">
      <PageHeader
        title="Executive overview"
        description="Live operating picture across every agent, execution and cost centre."
        actions={
          data ? (
            <Badge tone="idle" dot>
              Updated {relativeTime(data.generated_at)}
            </Badge>
          ) : null
        }
      />

      {/* Primary metrics */}
      {overview.isLoading || !data ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, index) => (
            <SkeletonCard key={index} rows={1} />
          ))}
        </div>
      ) : (
        <>
          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Key metrics">
            <Card interactive>
              <Stat
                label="Total agents"
                value={<AnimatedNumber value={data.agents.total} format={(v) => formatNumber(Math.round(v))} />}
                hint={`${data.agents.implemented} implemented · ${data.agents.coming_soon} on roadmap`}
                icon={<CpuChipIcon className="h-4 w-4" />}
              />
            </Card>
            <Card interactive>
              <Stat
                label="Executions (24h)"
                value={<AnimatedNumber value={data.executions.last_24h} format={(v) => formatCompact(Math.round(v))} />}
                hint={`${formatNumber(data.executions.total_all_time)} all time`}
                icon={<BoltIcon className="h-4 w-4" />}
              />
            </Card>
            <Card interactive>
              <Stat
                label="Success rate"
                value={<AnimatedNumber value={data.executions.success_rate_pct} format={(v) => formatPercent(v)} />}
                tone={data.executions.success_rate_pct >= 95 ? 'ok' : data.executions.success_rate_pct >= 85 ? 'warn' : 'err'}
                hint={`${data.executions.succeeded_24h} succeeded · ${data.executions.failed_24h} failed`}
                icon={<CheckCircleIcon className="h-4 w-4" />}
              />
            </Card>
            <Card interactive>
              <Stat
                label="Average latency"
                value={<AnimatedNumber value={data.executions.avg_latency_ms} format={(v) => formatDuration(v)} />}
                hint={`Avg cost ${formatCurrency(data.executions.avg_cost_usd, 4)} per run`}
                icon={<ClockIcon className="h-4 w-4" />}
              />
            </Card>
          </section>

          {/* Lifecycle strip */}
          <section className="grid gap-4 sm:grid-cols-3 xl:grid-cols-6" aria-label="Agent lifecycle">
            {[
              { label: 'Running', value: data.agents.running, tone: 'info' as const },
              { label: 'Idle', value: Math.max(data.agents.idle ?? 0, 0), tone: 'idle' as const },
              { label: 'Queued', value: data.agents.queued, tone: 'warn' as const },
              { label: 'Paused', value: data.agents.paused, tone: 'warn' as const },
              { label: 'Failed 24h', value: data.agents.failed_24h, tone: 'err' as const },
              { label: 'Approvals', value: data.approvals.pending, tone: 'warn' as const },
            ].map((item, index) => (
              <Card
                key={item.label}
                className="px-4 py-3.5"
                transition={{ delay: index * 0.05, duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
              >
                <p className="metric-label">{item.label}</p>
                <p
                  className={`mt-1.5 text-xl font-semibold tabular-nums ${
                    item.value > 0 && item.tone === 'err'
                      ? 'text-state-err'
                      : item.value > 0 && item.tone === 'warn'
                        ? 'text-state-warn'
                        : 'text-ink'
                  }`}
                >
                  <AnimatedNumber value={item.value} format={(v) => formatNumber(Math.round(v))} />
                </p>
              </Card>
            ))}
          </section>

          <div className="grid gap-4 lg:grid-cols-3">
            {/* Throughput */}
            <Card className="lg:col-span-2">
              <CardHeader
                title="Execution throughput"
                subtitle="Succeeded and failed executions per hour, last 24 hours"
              />
              <div className="h-64 px-2 pb-4 pt-4">
                {chartData.length === 0 ? (
                  <div className="flex h-full items-center justify-center text-xs text-ink-subtle">
                    No executions in this window
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chartData} margin={{ top: 4, right: 12, bottom: 0, left: -18 }}>
                      <defs>
                        <linearGradient id="ok" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#34a382" stopOpacity={0.28} />
                          <stop offset="100%" stopColor="#34a382" stopOpacity={0} />
                        </linearGradient>
                        <linearGradient id="err" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#c05353" stopOpacity={0.28} />
                          <stop offset="100%" stopColor="#c05353" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="rgb(var(--line))" strokeOpacity={0.5} vertical={false} />
                      <XAxis dataKey="label" {...chartAxis} minTickGap={28} />
                      <YAxis {...chartAxis} width={44} allowDecimals={false} />
                      <ReTooltip content={<ChartTooltip />} />
                      <Area
                        type="monotone"
                        dataKey="succeeded"
                        name="Succeeded"
                        stroke="#34a382"
                        strokeWidth={1.8}
                        fill="url(#ok)"
                      />
                      <Area
                        type="monotone"
                        dataKey="failed"
                        name="Failed"
                        stroke="#c05353"
                        strokeWidth={1.8}
                        fill="url(#err)"
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                )}
              </div>
            </Card>

            {/* Budget */}
            <Card>
              <CardHeader title="Spend against budget" subtitle="Daily and month-to-date" />
              <div className="space-y-5 px-5 py-5">
                <div>
                  <div className="mb-2 flex items-baseline justify-between">
                    <span className="metric-label">Today</span>
                    <span className="text-sm font-semibold tabular-nums">
                      {formatCurrency(data.cost.today_usd, 2)}
                      <span className="ml-1 text-2xs font-normal text-ink-subtle">
                        / {formatCurrency(data.cost.daily_budget_usd, 0)}
                      </span>
                    </span>
                  </div>
                  <Meter
                    value={data.cost.daily_budget_used_pct}
                    tone={data.cost.daily_budget_used_pct > 90 ? 'err' : data.cost.daily_budget_used_pct > 70 ? 'warn' : 'ok'}
                  />
                  <p className="mt-1.5 text-2xs text-ink-subtle">
                    {formatPercent(data.cost.daily_budget_used_pct)} of the daily budget consumed
                  </p>
                </div>
                <div>
                  <div className="mb-2 flex items-baseline justify-between">
                    <span className="metric-label">Month to date</span>
                    <span className="text-sm font-semibold tabular-nums">
                      {formatCurrency(data.cost.month_to_date_usd, 2)}
                      <span className="ml-1 text-2xs font-normal text-ink-subtle">
                        / {formatCurrency(data.cost.monthly_budget_usd, 0)}
                      </span>
                    </span>
                  </div>
                  <Meter
                    value={data.cost.monthly_budget_used_pct}
                    tone={data.cost.monthly_budget_used_pct > 90 ? 'err' : data.cost.monthly_budget_used_pct > 70 ? 'warn' : 'ok'}
                  />
                </div>
                <Link
                  to="/costs"
                  className="block rounded-xl border border-line/70 px-3 py-2 text-center text-xs text-ink-muted transition-colors hover:bg-accent-soft hover:text-ink"
                >
                  Open cost dashboard
                </Link>
              </div>
            </Card>
          </div>

          {/* AI usage */}
          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader
                title="AI usage"
                subtitle={`Tokens and spend by model, last ${usage.data?.window_days ?? 7} days`}
                action={
                  usage.data ? (
                    <div className="flex gap-4 text-right">
                      <div>
                        <p className="metric-label">Tokens</p>
                        <p className="text-sm font-semibold tabular-nums">
                          {formatCompact(usage.data.totals.total_tokens)}
                        </p>
                      </div>
                      <div>
                        <p className="metric-label">Spend</p>
                        <p className="text-sm font-semibold tabular-nums">
                          {formatCurrency(usage.data.totals.cost_usd, 2)}
                        </p>
                      </div>
                    </div>
                  ) : null
                }
              />
              <div className="h-56 px-2 pb-4 pt-4">
                {!usage.data?.models.length ? (
                  <div className="flex h-full items-center justify-center px-6 text-center text-xs text-ink-subtle">
                    No model calls recorded yet. Configure a provider API key and execute an agent to
                    populate this view.
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={usage.data.models.slice(0, 8)}
                      margin={{ top: 4, right: 12, bottom: 0, left: -18 }}
                    >
                      <CartesianGrid stroke="rgb(var(--line))" strokeOpacity={0.5} vertical={false} />
                      <XAxis dataKey="display_name" {...chartAxis} interval={0} angle={-12} height={44} textAnchor="end" />
                      <YAxis {...chartAxis} width={52} />
                      <ReTooltip content={<ChartTooltip />} cursor={{ fill: 'rgb(var(--ink) / 0.04)' }} />
                      <Bar dataKey="tokens_input" name="Input tokens" stackId="t" fill="#5b7cfa" radius={[0, 0, 0, 0]} />
                      <Bar dataKey="tokens_output" name="Output tokens" stackId="t" fill="#34a382" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader title="Model distribution" subtitle="Share of calls" />
              <div className="h-56 px-2 py-4">
                {!usage.data?.distribution.length ? (
                  <div className="flex h-full items-center justify-center text-xs text-ink-subtle">No data</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={usage.data.distribution}
                        dataKey="share_pct"
                        nameKey="model"
                        innerRadius="58%"
                        outerRadius="82%"
                        paddingAngle={2}
                        stroke="none"
                      >
                        {usage.data.distribution.map((entry, index) => (
                          <Cell key={entry.model} fill={SERIES_COLORS[index % SERIES_COLORS.length]} />
                        ))}
                      </Pie>
                      <ReTooltip content={<ChartTooltip />} />
                    </PieChart>
                  </ResponsiveContainer>
                )}
              </div>
              <div className="space-y-1.5 px-5 pb-5">
                {usage.data?.distribution.slice(0, 4).map((entry, index) => (
                  <div key={entry.model} className="flex items-center justify-between gap-2 text-2xs">
                    <span className="flex min-w-0 items-center gap-2 text-ink-muted">
                      <span
                        className="h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }}
                      />
                      <span className="truncate font-mono">{entry.model}</span>
                    </span>
                    <span className="tabular-nums">{formatPercent(entry.share_pct)}</span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          {/* Token + infra */}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Card>
              <CardHeader title="Token usage" subtitle="Last 24 hours" />
              <div className="grid grid-cols-2 gap-y-3 px-5 pb-5 pt-3">
                {[
                  ['Input', data.tokens.input_24h],
                  ['Output', data.tokens.output_24h],
                  ['Cached', data.tokens.cached_24h],
                  ['Total', data.tokens.total_24h],
                ].map(([label, value]) => (
                  <div key={label as string}>
                    <p className="metric-label">{label}</p>
                    <p className="mt-0.5 text-base font-semibold tabular-nums">{formatCompact(value as number)}</p>
                  </div>
                ))}
              </div>
            </Card>

            <Card>
              <CardHeader title="Compute" subtitle="Host utilisation" />
              <div className="space-y-3 px-5 pb-5 pt-3">
                <div>
                  <div className="flex justify-between text-2xs text-ink-muted">
                    <span>CPU ({data.resources.cpu_count} cores)</span>
                    <span className="tabular-nums">{formatPercent(data.resources.cpu_percent)}</span>
                  </div>
                  <Meter value={data.resources.cpu_percent} tone={data.resources.cpu_percent > 85 ? 'err' : 'info'} className="mt-1.5" />
                </div>
                <div>
                  <div className="flex justify-between text-2xs text-ink-muted">
                    <span>Memory</span>
                    <span className="tabular-nums">
                      {formatBytes(data.resources.memory.used_bytes)} / {formatBytes(data.resources.memory.total_bytes)}
                    </span>
                  </div>
                  <Meter value={data.resources.memory.used_pct} tone={data.resources.memory.used_pct > 85 ? 'err' : 'info'} className="mt-1.5" />
                </div>
                <div>
                  <div className="flex justify-between text-2xs text-ink-muted">
                    <span>GPU</span>
                    <span className="tabular-nums">
                      {data.resources.gpu.length
                        ? `${data.resources.gpu.length} device(s)`
                        : 'none detected'}
                    </span>
                  </div>
                  {data.resources.gpu.map((gpu) => (
                    <Meter key={gpu.index} value={gpu.utilisation_pct} tone="info" className="mt-1.5" />
                  ))}
                </div>
              </div>
            </Card>

            <Card>
              <CardHeader title="Queue & concurrency" subtitle="Execution pipeline" />
              <div className="space-y-3 px-5 pb-5 pt-3">
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-2 text-xs text-ink-muted">
                    <QueueListIcon className="h-4 w-4" /> Queue depth
                  </span>
                  <span className="text-base font-semibold tabular-nums">{data.resources.queue_depth}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-2 text-xs text-ink-muted">
                    <BoltIcon className="h-4 w-4" /> In flight
                  </span>
                  <span className="text-base font-semibold tabular-nums">
                    {data.resources.in_flight_executions}
                    <span className="ml-1 text-2xs font-normal text-ink-subtle">/ {data.resources.concurrency_limit}</span>
                  </span>
                </div>
                <Meter
                  value={(data.resources.in_flight_executions / Math.max(data.resources.concurrency_limit, 1)) * 100}
                  tone="info"
                />
                <div className="flex items-center justify-between border-t border-line/60 pt-3">
                  <span className="flex items-center gap-2 text-xs text-ink-muted">
                    <ArrowTrendingUpIcon className="h-4 w-4" /> Network
                  </span>
                  <span className="text-2xs tabular-nums text-ink-muted">
                    ↓ {formatBytes(data.resources.network.rx_bytes)} · ↑ {formatBytes(data.resources.network.tx_bytes)}
                  </span>
                </div>
              </div>
            </Card>

            <Card>
              <CardHeader title="Attention" subtitle="Items needing action" />
              <div className="space-y-2.5 px-5 pb-5 pt-3">
                <Link
                  to="/approvals"
                  className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2.5 transition-colors hover:bg-accent-soft"
                >
                  <span className="text-xs text-ink-muted">Pending approvals</span>
                  <Badge tone={data.approvals.pending > 0 ? 'warn' : 'idle'}>{data.approvals.pending}</Badge>
                </Link>
                <Link
                  to="/logs?level=ERROR"
                  className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2.5 transition-colors hover:bg-accent-soft"
                >
                  <span className="flex items-center gap-2 text-xs text-ink-muted">
                    <ExclamationTriangleIcon className="h-4 w-4" /> Errors today
                  </span>
                  <Badge tone={data.errors_today > 0 ? 'err' : 'idle'}>{data.errors_today}</Badge>
                </Link>
                <Link
                  to="/executions?status=failed"
                  className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2.5 transition-colors hover:bg-accent-soft"
                >
                  <span className="text-xs text-ink-muted">Failed executions (24h)</span>
                  <Badge tone={data.executions.failed_24h > 0 ? 'err' : 'idle'}>{data.executions.failed_24h}</Badge>
                </Link>
                <div className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2.5">
                  <span className="flex items-center gap-2 text-xs text-ink-muted">
                    <CurrencyDollarIcon className="h-4 w-4" /> Retries (24h)
                  </span>
                  <Badge tone={data.executions.retries_24h > 0 ? 'warn' : 'idle'}>{data.executions.retries_24h}</Badge>
                </div>
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}
