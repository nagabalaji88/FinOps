import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, Badge, Card, CardHeader, EmptyState, ErrorState, Meter, PageHeader, SkeletonCard, formatCurrency, formatNumber } from '@finops/shared'

interface Dimension {
  key: string
  cost_usd: number
  calls: number
  tokens: number
}

interface CostSummary {
  window_days: number
  totals: Record<string, number>
  budgets: Record<string, number>
  forecast: Record<string, number>
  by_agent: Dimension[]
  by_model: Dimension[]
  by_provider: Dimension[]
  by_user: Dimension[]
  by_department: Dimension[]
  by_tool: Dimension[]
  by_category: Dimension[]
  daily_series: { date: string; cost_usd: number; calls: number }[]
  alerts: { severity: string; scope: string; message: string }[]
}

const COLORS = ['#5b7cfa', '#34a382', '#c98b2a', '#b1558f', '#4aa3c7', '#8b7fd4', '#c05353']

const axis = {
  stroke: 'rgb(var(--line))',
  tick: { fill: 'rgb(var(--ink-subtle))', fontSize: 10 },
  tickLine: false,
  axisLine: false,
}

function Tip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-line bg-surface-raised px-2.5 py-2 text-2xs shadow-glass-lg">
      <p className="mb-1 font-medium">{label}</p>
      {payload.map((entry: any) => (
        <p key={entry.dataKey} className="text-ink-muted">
          {entry.name}: <span className="tabular-nums text-ink">{entry.value}</span>
        </p>
      ))}
    </div>
  )
}

function DimensionTable({ title, subtitle, rows }: { title: string; subtitle: string; rows: Dimension[] }) {
  const total = rows.reduce((sum, row) => sum + row.cost_usd, 0)
  return (
    <Card className="overflow-hidden">
      <CardHeader title={title} subtitle={subtitle} />
      {rows.length === 0 ? (
        <EmptyState title="No spend recorded" />
      ) : (
        <div className="space-y-2 px-5 pb-5 pt-3">
          {rows.slice(0, 8).map((row) => (
            <div key={row.key}>
              <div className="flex items-baseline justify-between gap-3 text-xs">
                <span className="truncate text-ink-muted">{row.key}</span>
                <span className="shrink-0 tabular-nums font-medium">{formatCurrency(row.cost_usd, 4)}</span>
              </div>
              <Meter value={total ? (row.cost_usd / total) * 100 : 0} tone="info" className="mt-1" />
              <p className="mt-0.5 text-2xs text-ink-subtle">
                {formatNumber(row.calls)} calls · {formatNumber(row.tokens)} tokens
              </p>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function Costs() {
  const [days, setDays] = useState(30)
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['costs', days],
    queryFn: () => api.get<CostSummary>('/costs/summary', { days }),
    refetchInterval: 60_000,
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div className="space-y-5">
      <PageHeader
        title="Cost"
        description="Attribution across agent, model, provider, user, department and tool, with forecasting and budget alerts."
        actions={
          <select
            className="input w-36"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            aria-label="Reporting window"
          >
            {[7, 30, 90, 180, 365].map((value) => (
              <option key={value} value={value}>
                Last {value} days
              </option>
            ))}
          </select>
        }
      />

      {isLoading || !data ? (
        <div className="grid gap-4 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <SkeletonCard key={index} rows={1} />
          ))}
        </div>
      ) : (
        <>
          {data.alerts.length > 0 ? (
            <div className="space-y-2">
              {data.alerts.map((alert, index) => (
                <Card
                  key={index}
                  className={`px-4 py-3 ${alert.severity === 'critical' ? 'border-state-err/30 bg-state-err/5' : 'border-state-warn/30 bg-state-warn/5'}`}
                >
                  <div className="flex items-center gap-2">
                    <Badge tone={alert.severity === 'critical' ? 'err' : 'warn'}>{alert.scope}</Badge>
                    <p className="text-xs">{alert.message}</p>
                  </div>
                </Card>
              ))}
            </div>
          ) : null}

          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {[
              ['Today', data.totals.today_usd, `Budget ${formatCurrency(data.budgets.daily_usd, 0)}`],
              ['This week', data.totals.week_usd, ''],
              ['Month to date', data.totals.month_usd, `Budget ${formatCurrency(data.budgets.monthly_usd, 0)}`],
              [
                'Month-end forecast',
                data.forecast.month_end_projection_usd,
                `${formatCurrency(data.forecast.daily_run_rate_usd, 2)}/day run rate`,
              ],
            ].map(([label, value, hint]) => (
              <Card key={label as string} className="px-5 py-4">
                <p className="metric-label">{label as string}</p>
                <p className="metric-value mt-2">{formatCurrency(value as number, 2)}</p>
                {hint ? <p className="mt-1.5 text-2xs text-ink-subtle">{hint as string}</p> : null}
              </Card>
            ))}
          </section>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Daily spend" subtitle={`Last ${data.window_days} days`} />
              <div className="h-64 px-2 pb-4 pt-4">
                {data.daily_series.length === 0 ? (
                  <div className="flex h-full items-center justify-center text-xs text-ink-subtle">
                    No spend recorded in this window
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={data.daily_series} margin={{ top: 4, right: 12, bottom: 0, left: -16 }}>
                      <CartesianGrid stroke="rgb(var(--line))" strokeOpacity={0.5} vertical={false} />
                      <XAxis dataKey="date" {...axis} minTickGap={30} />
                      <YAxis {...axis} width={54} />
                      <ReTooltip content={<Tip />} />
                      <Line
                        type="monotone"
                        dataKey="cost_usd"
                        name="Spend (USD)"
                        stroke="#5b7cfa"
                        strokeWidth={1.8}
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader title="By category" subtitle="LLM, embedding, tool, API" />
              <div className="h-52 px-2 py-3">
                {data.by_category.length === 0 ? (
                  <div className="flex h-full items-center justify-center text-xs text-ink-subtle">No data</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={data.by_category}
                        dataKey="cost_usd"
                        nameKey="key"
                        innerRadius="56%"
                        outerRadius="80%"
                        paddingAngle={2}
                        stroke="none"
                      >
                        {data.by_category.map((entry, index) => (
                          <Cell key={entry.key} fill={COLORS[index % COLORS.length]} />
                        ))}
                      </Pie>
                      <ReTooltip content={<Tip />} />
                    </PieChart>
                  </ResponsiveContainer>
                )}
              </div>
              <div className="space-y-1 px-5 pb-5">
                {data.by_category.map((entry, index) => (
                  <div key={entry.key} className="flex justify-between text-2xs">
                    <span className="flex items-center gap-2 text-ink-muted">
                      <span className="h-1.5 w-1.5 rounded-full" style={{ background: COLORS[index % COLORS.length] }} />
                      {entry.key}
                    </span>
                    <span className="tabular-nums">{formatCurrency(entry.cost_usd, 4)}</span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <DimensionTable title="By agent" subtitle="Which agents consume budget" rows={data.by_agent} />
            <DimensionTable title="By model" subtitle="Spend per model" rows={data.by_model} />
            <DimensionTable title="By provider" subtitle="Vendor concentration" rows={data.by_provider} />
            <DimensionTable title="By user" subtitle="Individual consumption" rows={data.by_user} />
            <DimensionTable title="By department" subtitle="Chargeback view" rows={data.by_department} />
            <DimensionTable title="By tool / API" subtitle="Tool-attributed spend" rows={data.by_tool} />
          </div>

          <Card>
            <CardHeader title="Model economics" subtitle="Cost against call volume" />
            <div className="h-64 px-2 pb-4 pt-4">
              {data.by_model.length === 0 ? (
                <div className="flex h-full items-center justify-center text-xs text-ink-subtle">No model spend</div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.by_model.slice(0, 10)} margin={{ top: 4, right: 12, bottom: 0, left: -16 }}>
                    <CartesianGrid stroke="rgb(var(--line))" strokeOpacity={0.5} vertical={false} />
                    <XAxis dataKey="key" {...axis} angle={-12} height={44} textAnchor="end" interval={0} />
                    <YAxis {...axis} width={54} />
                    <ReTooltip content={<Tip />} cursor={{ fill: 'rgb(var(--ink) / 0.04)' }} />
                    <Bar dataKey="cost_usd" name="Spend (USD)" fill="#5b7cfa" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
