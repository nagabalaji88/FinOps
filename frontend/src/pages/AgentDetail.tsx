import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeftIcon, PlayIcon } from '@heroicons/react/24/outline'
import { api, type Agent, type Execution } from '@/lib/api'
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  JsonView,
  PageHeader,
  Skeleton,
  StatusDot,
  TabPanel,
  Tabs,
} from '@/components/ui'
import { ExecuteDialog } from '@/components/agents/ExecuteDialog'
import { useAuth, useToasts } from '@/store'
import { formatCurrency, formatDateTime, formatDuration, formatNumber, formatPercent, relativeTime } from '@/lib/utils'

export default function AgentDetail() {
  const { agentKey = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') ?? 'overview'
  const queryClient = useQueryClient()
  const push = useToasts((state) => state.push)
  const { can } = useAuth()
  const [executing, setExecuting] = useState(false)
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null)

  const agent = useQuery({
    queryKey: ['agent', agentKey],
    queryFn: () => api.get<Agent>(`/agents/${agentKey}`),
    refetchInterval: 20_000,
  })
  const executions = useQuery({
    queryKey: ['agent-executions', agentKey],
    queryFn: () => api.get<{ items: Execution[] }>('/executions', { agent_key: agentKey, limit: 25 }),
    enabled: tab === 'executions' || tab === 'overview',
  })
  const versions = useQuery({
    queryKey: ['agent-versions', agentKey],
    queryFn: () => api.get<any[]>(`/agents/${agentKey}/versions`),
    enabled: tab === 'versions',
  })

  const save = useMutation({
    mutationFn: (config: Record<string, unknown>) => api.put(`/agents/${agentKey}/config`, config),
    onSuccess: (response: any) => {
      push({ title: `Version ${response.version} saved`, description: 'Publish it to make it live', tone: 'ok' })
      void queryClient.invalidateQueries({ queryKey: ['agent', agentKey] })
      void queryClient.invalidateQueries({ queryKey: ['agent-versions', agentKey] })
    },
    onError: (error) => push({ title: 'Save failed', description: (error as Error).message, tone: 'err' }),
  })

  const publish = useMutation({
    mutationFn: (version: number) => api.post(`/agents/${agentKey}/versions/${version}/publish`),
    onSuccess: () => {
      push({ title: 'Version published', tone: 'ok' })
      void queryClient.invalidateQueries({ queryKey: ['agent-versions', agentKey] })
      void queryClient.invalidateQueries({ queryKey: ['agent', agentKey] })
    },
  })

  const rollback = useMutation({
    mutationFn: (version: number) => api.post(`/agents/${agentKey}/versions/${version}/rollback`),
    onSuccess: () => {
      push({ title: 'Rolled back', tone: 'warn' })
      void queryClient.invalidateQueries({ queryKey: ['agent-versions', agentKey] })
      void queryClient.invalidateQueries({ queryKey: ['agent', agentKey] })
    },
  })

  if (agent.isError) return <ErrorState error={agent.error} retry={() => agent.refetch()} />
  const data = agent.data
  const config = (data?.config ?? {}) as Record<string, any>
  const metrics = data?.metrics

  return (
    <div className="space-y-5">
      <div>
        <Link to="/agents" className="inline-flex items-center gap-1.5 text-2xs text-ink-muted hover:text-ink">
          <ArrowLeftIcon className="h-3 w-3" /> Agents
        </Link>
      </div>

      <PageHeader
        title={data?.name ?? agentKey}
        description={data?.description}
        actions={
          data && data.availability === 'implemented' ? (
            <div className="flex items-center gap-2">
              <Badge status={data.lifecycle_state}>{data.lifecycle_state}</Badge>
              {metrics ? <Badge status={metrics.health}>{metrics.health}</Badge> : null}
              {can('agent:execute') ? (
                <Button variant="primary" size="sm" onClick={() => setExecuting(true)}>
                  <PlayIcon className="h-3.5 w-3.5" /> Execute
                </Button>
              ) : null}
            </div>
          ) : (
            <Badge tone="idle">Coming soon</Badge>
          )
        }
      />

      {!data ? (
        <Skeleton className="h-64 w-full" />
      ) : data.availability === 'coming_soon' ? (
        <Card>
          <EmptyState
            title="Not implemented yet"
            description={`${data.name} is on the delivery roadmap${data.planned_quarter ? ` for ${data.planned_quarter}` : ''}. Owned by ${data.owner}.`}
          />
        </Card>
      ) : (
        <Tabs
          value={tab}
          onValueChange={(value) => setSearchParams({ tab: value }, { replace: true })}
          tabs={[
            { value: 'overview', label: 'Overview' },
            { value: 'config', label: 'Configuration' },
            { value: 'tools', label: 'Tools', count: data.tools.length },
            { value: 'executions', label: 'Executions' },
            { value: 'metrics', label: 'Metrics' },
            { value: 'versions', label: 'Versions', count: data.version },
          ]}
        >
          <TabPanel value="overview">
            <div className="grid gap-4 lg:grid-cols-3">
              <Card className="lg:col-span-2">
                <CardHeader title="System prompt" subtitle="Operating instructions enforced on every run" />
                <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap px-5 pb-5 pt-3 text-xs leading-relaxed text-ink-muted">
                  {config.system_prompt ?? '—'}
                </pre>
              </Card>
              <Card>
                <CardHeader title="Configuration summary" />
                <dl className="space-y-2 px-5 pb-5 pt-3 text-xs">
                  {[
                    ['Model', config.model ?? 'router default'],
                    ['Temperature', config.temperature],
                    ['Max iterations', config.max_iterations],
                    ['Retrieval top-k', config.retrieval_top_k],
                    ['Memory', config.memory_enabled ? 'enabled' : 'disabled'],
                    ['Citations required', config.require_citations ? 'yes' : 'no'],
                    ['PII masking', config.mask_pii ? 'on' : 'off'],
                    ['Final approval', config.final_approval_required ? config.final_approval_risk_threshold : 'not required'],
                    ['Cost cap', formatCurrency(config.cost_cap_usd ?? 0, 2)],
                    ['SLA', formatDuration(data.sla_latency_ms)],
                    ['Owner', data.owner],
                    ['Department', data.department],
                  ].map(([label, value]) => (
                    <div key={label as string} className="flex justify-between gap-3">
                      <dt className="text-ink-subtle">{label as string}</dt>
                      <dd className="truncate text-right font-medium">{String(value ?? '—')}</dd>
                    </div>
                  ))}
                </dl>
              </Card>
            </div>
          </TabPanel>

          <TabPanel value="config">
            <Card>
              <CardHeader
                title="Edit configuration"
                subtitle="Saving creates a new version; publish to make it live"
                action={
                  can('agent:write') ? (
                    <Button
                      size="sm"
                      variant="primary"
                      loading={save.isPending}
                      onClick={() => draft && save.mutate(draft)}
                      disabled={!draft}
                    >
                      Save version
                    </Button>
                  ) : null
                }
              />
              <div className="grid gap-4 px-5 pb-5 pt-3 md:grid-cols-2">
                {[
                  ['temperature', 'Temperature', 'number', 0, 2, 0.05],
                  ['max_tokens', 'Max tokens', 'number', 256, 32000, 128],
                  ['max_iterations', 'Max iterations', 'number', 1, 40, 1],
                  ['retrieval_top_k', 'Retrieval top-k', 'number', 1, 30, 1],
                  ['cost_cap_usd', 'Cost cap (USD)', 'number', 0.1, 100, 0.1],
                ].map(([field, label, , min, max, step]) => (
                  <div key={field as string}>
                    <label htmlFor={field as string} className="metric-label mb-1.5 block">
                      {label as string}
                    </label>
                    <input
                      id={field as string}
                      type="number"
                      min={min as number}
                      max={max as number}
                      step={step as number}
                      className="input"
                      defaultValue={config[field as string]}
                      disabled={!can('agent:write')}
                      onChange={(event) =>
                        setDraft((current) => ({ ...(current ?? {}), [field as string]: Number(event.target.value) }))
                      }
                    />
                  </div>
                ))}
                <div>
                  <label htmlFor="model" className="metric-label mb-1.5 block">
                    Model
                  </label>
                  <input
                    id="model"
                    className="input font-mono"
                    defaultValue={config.model ?? ''}
                    placeholder="router default"
                    disabled={!can('agent:write')}
                    onChange={(event) => setDraft((current) => ({ ...(current ?? {}), model: event.target.value }))}
                  />
                </div>
                <div className="md:col-span-2">
                  <label htmlFor="prompt" className="metric-label mb-1.5 block">
                    System prompt
                  </label>
                  <textarea
                    id="prompt"
                    rows={12}
                    className="input font-mono text-2xs"
                    defaultValue={config.system_prompt ?? ''}
                    disabled={!can('agent:write')}
                    onChange={(event) =>
                      setDraft((current) => ({ ...(current ?? {}), system_prompt: event.target.value }))
                    }
                  />
                </div>
                <div className="md:col-span-2 flex flex-wrap gap-4">
                  {[
                    ['memory_enabled', 'Conversation memory'],
                    ['require_citations', 'Require citations'],
                    ['mask_pii', 'Mask PII'],
                    ['strict_validation', 'Strict validation'],
                    ['final_approval_required', 'Final human approval'],
                  ].map(([field, label]) => (
                    <label key={field} className="flex items-center gap-2 text-xs text-ink-muted">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 rounded border-line"
                        defaultChecked={Boolean(config[field])}
                        disabled={!can('agent:write')}
                        onChange={(event) =>
                          setDraft((current) => ({ ...(current ?? {}), [field]: event.target.checked }))
                        }
                      />
                      {label}
                    </label>
                  ))}
                </div>
              </div>
            </Card>
          </TabPanel>

          <TabPanel value="tools">
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {data.tool_details?.map((tool) => (
                <Card key={tool.name}>
                  <div className="px-5 pt-5">
                    <div className="flex items-start justify-between gap-2">
                      <p className="font-mono text-xs font-semibold">{tool.name}</p>
                      {tool.requires_approval ? <Badge tone="warn">approval</Badge> : null}
                    </div>
                    <p className="mt-1.5 text-2xs leading-relaxed text-ink-muted">{tool.description}</p>
                  </div>
                  <div className="px-5 pb-5 pt-3">
                    <JsonView data={tool.schema} maxHeight="max-h-40" />
                  </div>
                </Card>
              ))}
            </div>
          </TabPanel>

          <TabPanel value="executions">
            <Card className="overflow-hidden">
              <CardHeader title="Recent executions" />
              <div className="scroll-x">
                <table className="w-full min-w-[720px] text-xs">
                  <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
                    <tr>
                      {['Execution', 'Status', 'Started', 'Latency', 'Cost', 'Tools'].map((header) => (
                        <th key={header} className="px-4 py-2.5 text-left font-medium">
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {executions.data?.items.map((execution) => (
                      <tr key={execution.id} className="table-row">
                        <td className="px-4 py-2.5">
                          <Link to={`/executions/${execution.id}`} className="flex items-center gap-2 hover:underline">
                            <StatusDot status={execution.status} />
                            <span className="font-mono text-2xs">{execution.id.slice(0, 8)}</span>
                          </Link>
                        </td>
                        <td className="px-4 py-2.5">
                          <Badge status={execution.status}>{execution.status}</Badge>
                        </td>
                        <td className="px-4 py-2.5 text-ink-muted">{relativeTime(execution.created_at)}</td>
                        <td className="px-4 py-2.5 tabular-nums">{formatDuration(execution.latency_ms)}</td>
                        <td className="px-4 py-2.5 tabular-nums">{formatCurrency(execution.cost_usd, 4)}</td>
                        <td className="px-4 py-2.5 tabular-nums">{execution.tool_call_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {executions.data?.items.length === 0 ? <EmptyState title="No executions yet" /> : null}
            </Card>
          </TabPanel>

          <TabPanel value="metrics">
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {metrics
                ? [
                    ['Executions (all time)', formatNumber(metrics.executions_total)],
                    ['Executions (24h)', formatNumber(metrics.executions_24h)],
                    ['Success rate', formatPercent(metrics.success_rate_pct)],
                    ['Error rate', formatPercent(metrics.error_rate_pct)],
                    ['Avg latency', formatDuration(metrics.avg_latency_ms)],
                    ['Cost today', formatCurrency(metrics.cost_today_usd, 4)],
                    ['Retries (24h)', formatNumber(metrics.retry_count_24h)],
                    ['Open incidents', formatNumber(metrics.open_incidents)],
                  ].map(([label, value]) => (
                    <Card key={label} className="px-5 py-4">
                      <p className="metric-label">{label}</p>
                      <p className="metric-value mt-2">{value}</p>
                    </Card>
                  ))
                : null}
            </div>
          </TabPanel>

          <TabPanel value="versions">
            <Card className="overflow-hidden">
              <CardHeader title="Version history" subtitle="Publish or roll back configuration" />
              <div className="scroll-x">
                <table className="w-full min-w-[720px] text-xs">
                  <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
                    <tr>
                      {['Version', 'Changelog', 'Published', 'Current', 'Created', ''].map((header) => (
                        <th key={header} className="px-4 py-2.5 text-left font-medium">
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {versions.data?.map((version) => (
                      <tr key={version.version} className="table-row">
                        <td className="px-4 py-2.5 font-mono">v{version.version}</td>
                        <td className="px-4 py-2.5 text-ink-muted">{version.changelog}</td>
                        <td className="px-4 py-2.5">
                          <Badge tone={version.published ? 'ok' : 'idle'}>{version.published ? 'published' : 'draft'}</Badge>
                        </td>
                        <td className="px-4 py-2.5">{version.is_current ? <Badge tone="ok">current</Badge> : null}</td>
                        <td className="px-4 py-2.5 text-ink-muted">{formatDateTime(version.created_at)}</td>
                        <td className="px-4 py-2.5 text-right">
                          {can('agent:publish') ? (
                            <div className="flex justify-end gap-1.5">
                              {!version.published ? (
                                <Button size="sm" variant="ghost" onClick={() => publish.mutate(version.version)}>
                                  Publish
                                </Button>
                              ) : null}
                              {!version.is_current ? (
                                <Button size="sm" variant="ghost" onClick={() => rollback.mutate(version.version)}>
                                  Roll back
                                </Button>
                              ) : null}
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </TabPanel>
        </Tabs>
      )}

      {executing && data ? <ExecuteDialog agent={data} onClose={() => setExecuting(false)} /> : null}
    </div>
  )
}
