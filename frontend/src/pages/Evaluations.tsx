import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip as ReTooltip,
} from 'recharts'
import { api } from '@/lib/api'
import { Badge, Card, CardHeader, EmptyState, ErrorState, Meter, PageHeader, SkeletonCard } from '@/components/ui'
import { formatCurrency, formatDuration, relativeTime, titleCase } from '@/lib/utils'

interface AgentEvaluation {
  agent_key: string
  evaluations: number
  faithfulness: number
  groundedness: number
  hallucination_score: number
  citation_score: number
  tool_success_rate: number
  answer_relevance: number
  avg_latency_ms: number
  avg_cost_usd: number
  human_rating: number | null
  human_feedback_count: number
}

interface EvaluationRow {
  id: string
  execution_id: string
  agent_key: string
  evaluator: string
  faithfulness: number | null
  groundedness: number | null
  hallucination_score: number | null
  citation_score: number | null
  tool_success_rate: number | null
  human_rating: number | null
  human_feedback: string | null
  latency_ms: number | null
  cost_usd: number | null
  created_at: string
}

export default function Evaluations() {
  const metrics = useQuery({
    queryKey: ['eval-metrics'],
    queryFn: () => api.get<{ agents: AgentEvaluation[] }>('/platform-metrics/evaluations', { days: 30 }),
  })
  const rows = useQuery({
    queryKey: ['evaluations'],
    queryFn: () => api.get<EvaluationRow[]>('/evaluations', { limit: 100 }),
  })

  if (metrics.isError) return <ErrorState error={metrics.error} retry={() => metrics.refetch()} />

  const agents = metrics.data?.agents ?? []

  return (
    <div className="space-y-5">
      <PageHeader
        title="Evaluation"
        description="Faithfulness, groundedness, hallucination rate, citation coverage, tool success and human feedback."
      />

      {metrics.isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 3 }).map((_, index) => (
            <SkeletonCard key={index} rows={4} />
          ))}
        </div>
      ) : agents.length === 0 ? (
        <Card>
          <EmptyState
            title="No evaluations yet"
            description="Run an evaluation from any completed execution to populate these scores."
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {agents.map((agent) => {
            const radar = [
              { metric: 'Faithfulness', value: (agent.faithfulness ?? 0) * 100 },
              { metric: 'Groundedness', value: (agent.groundedness ?? 0) * 100 },
              { metric: 'Citations', value: (agent.citation_score ?? 0) * 100 },
              { metric: 'Tool success', value: (agent.tool_success_rate ?? 0) * 100 },
              { metric: 'Relevance', value: (agent.answer_relevance ?? 0) * 100 },
              { metric: 'Non-hallucination', value: (1 - (agent.hallucination_score ?? 0)) * 100 },
            ]
            return (
              <Card key={agent.agent_key}>
                <CardHeader
                  title={titleCase(agent.agent_key)}
                  subtitle={`${agent.evaluations} evaluations · ${agent.human_feedback_count} human ratings`}
                  action={
                    agent.human_rating ? <Badge tone="ok">{agent.human_rating.toFixed(1)}★</Badge> : null
                  }
                />
                <div className="h-52 px-2 py-2">
                  <ResponsiveContainer width="100%" height="100%">
                    <RadarChart data={radar} outerRadius="72%">
                      <PolarGrid stroke="rgb(var(--line))" />
                      <PolarAngleAxis dataKey="metric" tick={{ fill: 'rgb(var(--ink-subtle))', fontSize: 9 }} />
                      <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
                      <ReTooltip
                        contentStyle={{
                          background: 'rgb(var(--surface-raised))',
                          border: '1px solid rgb(var(--line))',
                          borderRadius: 8,
                          fontSize: 11,
                        }}
                      />
                      <Radar dataKey="value" stroke="#5b7cfa" fill="#5b7cfa" fillOpacity={0.22} />
                    </RadarChart>
                  </ResponsiveContainer>
                </div>
                <div className="space-y-2 px-5 pb-5">
                  {[
                    ['Faithfulness', agent.faithfulness],
                    ['Groundedness', agent.groundedness],
                    ['Citation coverage', agent.citation_score],
                    ['Tool success', agent.tool_success_rate],
                  ].map(([label, value]) => (
                    <div key={label as string}>
                      <div className="flex justify-between text-2xs text-ink-muted">
                        <span>{label as string}</span>
                        <span className="tabular-nums">{((value as number) * 100).toFixed(1)}%</span>
                      </div>
                      <Meter
                        value={(value as number) * 100}
                        tone={(value as number) > 0.8 ? 'ok' : (value as number) > 0.5 ? 'warn' : 'err'}
                        className="mt-1"
                      />
                    </div>
                  ))}
                  <p className="pt-1 text-2xs text-ink-subtle">
                    Avg {formatDuration(agent.avg_latency_ms)} · {formatCurrency(agent.avg_cost_usd, 4)} per run
                  </p>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <Card className="overflow-hidden">
        <CardHeader title="Recent evaluations" subtitle="Automatic scores and human feedback" />
        <div className="scroll-x">
          <table className="w-full min-w-[840px] text-xs">
            <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
              <tr>
                {['Execution', 'Agent', 'Evaluator', 'Faithful', 'Grounded', 'Citations', 'Tools', 'Human', 'When'].map(
                  (header) => (
                    <th key={header} className="px-4 py-2.5 text-left font-medium">
                      {header}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {rows.data?.map((row) => (
                <tr key={row.id} className="table-row">
                  <td className="px-4 py-2.5">
                    <Link to={`/executions/${row.execution_id}`} className="font-mono text-2xs hover:underline">
                      {row.execution_id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="px-4 py-2.5">{titleCase(row.agent_key)}</td>
                  <td className="px-4 py-2.5">
                    <Badge tone="idle">{row.evaluator}</Badge>
                  </td>
                  <td className="px-4 py-2.5 tabular-nums">{row.faithfulness?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-2.5 tabular-nums">{row.groundedness?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-2.5 tabular-nums">{row.citation_score?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-2.5 tabular-nums">{row.tool_success_rate?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-2.5">{row.human_rating ? `${row.human_rating}★` : '—'}</td>
                  <td className="px-4 py-2.5 text-ink-muted">{relativeTime(row.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.data?.length === 0 ? <EmptyState title="No evaluation records" /> : null}
      </Card>
    </div>
  )
}
