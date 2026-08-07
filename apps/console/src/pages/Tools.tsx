import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { PlayIcon } from '@heroicons/react/24/outline'
import { api, type ToolDefinition, Badge, Button, Card, EmptyState, ErrorState, JsonView, Meter, Modal, PageHeader, SkeletonCard, useAuth, useToasts, cn, formatDuration, formatNumber, formatPercent, relativeTime } from '@finops/shared'

export default function Tools() {
  const { can } = useAuth()
  const push = useToasts((state) => state.push)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('all')
  const [testing, setTesting] = useState<ToolDefinition | null>(null)
  const [args, setArgs] = useState('{}')
  const [result, setResult] = useState<unknown>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['tools'],
    queryFn: () => api.get<ToolDefinition[]>('/tools'),
    refetchInterval: 30_000,
  })

  const invoke = useMutation({
    mutationFn: () => api.post(`/tools/${testing!.name}/invoke`, { arguments: JSON.parse(args) }),
    onSuccess: (response) => setResult(response),
    onError: (mutationError) =>
      push({ title: 'Invocation failed', description: (mutationError as Error).message, tone: 'err' }),
  })

  const categories = useMemo(
    () => ['all', ...Array.from(new Set((data ?? []).map((tool) => tool.category))).sort()],
    [data],
  )

  const tools = useMemo(
    () =>
      (data ?? [])
        .filter((tool) => (category === 'all' ? true : tool.category === category))
        .filter((tool) =>
          search ? `${tool.name} ${tool.description}`.toLowerCase().includes(search.toLowerCase()) : true,
        ),
    [data, category, search],
  )

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div className="space-y-5">
      <PageHeader
        title="Tools & APIs"
        description="Every capability an agent can invoke, with live health, latency and failure counts."
        actions={
          <div className="flex gap-2">
            <input
              className="input w-48"
              placeholder="Filter tools…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="Filter tools"
            />
            <select
              className="input w-40"
              value={category}
              onChange={(event) => setCategory(event.target.value)}
              aria-label="Filter by category"
            >
              {categories.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>
        }
      />

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <SkeletonCard key={index} rows={3} />
          ))}
        </div>
      ) : tools.length === 0 ? (
        <Card>
          <EmptyState title="No tools match" />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {tools.map((tool) => {
            const health = tool.health
            return (
              <Card key={tool.name} interactive className="flex flex-col">
                <div className="flex items-start justify-between gap-3 px-5 pt-5">
                  <div className="min-w-0">
                    <h3 className="truncate font-mono text-xs font-semibold">{tool.name}</h3>
                    <p className="mt-1.5 line-clamp-2 text-2xs leading-relaxed text-ink-muted">{tool.description}</p>
                  </div>
                  <Badge status={health?.status ?? 'unknown'}>{health?.status ?? 'unused'}</Badge>
                </div>

                <div className="mt-3 flex flex-wrap gap-1.5 px-5">
                  <span className="chip">{tool.category}</span>
                  {tool.requires_approval ? <span className="chip">approval</span> : null}
                  {tool.writes_data ? <span className="chip">writes</span> : null}
                  <span className="chip">{tool.timeout_seconds}s timeout</span>
                </div>

                <dl className="mt-4 grid grid-cols-3 gap-y-3 border-t border-line/60 px-5 py-4 text-xs">
                  <div>
                    <dt className="metric-label">Calls</dt>
                    <dd className="mt-0.5 font-semibold tabular-nums">{formatNumber(health?.total_calls ?? 0)}</dd>
                  </div>
                  <div>
                    <dt className="metric-label">p95</dt>
                    <dd className="mt-0.5 font-semibold tabular-nums">{formatDuration(health?.p95_latency_ms ?? 0)}</dd>
                  </div>
                  <div>
                    <dt className="metric-label">Failures</dt>
                    <dd
                      className={cn(
                        'mt-0.5 font-semibold tabular-nums',
                        (health?.failures ?? 0) > 0 && 'text-state-err',
                      )}
                    >
                      {health?.failures ?? 0}
                    </dd>
                  </div>
                </dl>

                <div className="px-5">
                  <Meter
                    value={health?.success_rate_pct ?? 100}
                    tone={(health?.success_rate_pct ?? 100) > 95 ? 'ok' : 'warn'}
                  />
                  <p className="mt-1 text-2xs text-ink-subtle">
                    {formatPercent(health?.success_rate_pct ?? 100)} success
                    {health?.retries ? ` · ${health.retries} retries` : ''}
                    {health?.timeouts ? ` · ${health.timeouts} timeouts` : ''}
                    {health?.last_called_at ? ` · last ${relativeTime(health.last_called_at)}` : ''}
                  </p>
                </div>

                {health?.last_error ? (
                  <p className="mx-5 mt-2 line-clamp-2 rounded-lg bg-state-err/10 px-2 py-1 text-2xs text-state-err">
                    {health.last_error}
                  </p>
                ) : null}

                <div className="mt-auto flex items-center gap-2 px-5 py-4">
                  <Button
                    size="sm"
                    onClick={() => {
                      setTesting(tool)
                      setResult(null)
                      setArgs('{}')
                    }}
                  >
                    Schema
                  </Button>
                  {can('tool:invoke') && !tool.requires_approval ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setTesting(tool)
                        setResult(null)
                        setArgs('{}')
                      }}
                    >
                      <PlayIcon className="h-3.5 w-3.5" /> Invoke
                    </Button>
                  ) : null}
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {testing ? (
        <Modal
          open
          onOpenChange={(open) => !open && setTesting(null)}
          title={testing.name}
          description={testing.description}
          size="lg"
        >
          <div className="space-y-4">
            <div>
              <p className="metric-label mb-1.5">JSON schema</p>
              <JsonView data={testing.schema} maxHeight="max-h-56" />
            </div>

            {can('tool:invoke') && !testing.requires_approval ? (
              <>
                <div>
                  <label htmlFor="tool-args" className="metric-label mb-1.5 block">
                    Arguments
                  </label>
                  <textarea
                    id="tool-args"
                    rows={5}
                    className="input font-mono text-2xs"
                    value={args}
                    onChange={(event) => setArgs(event.target.value)}
                  />
                </div>
                <div className="flex justify-end">
                  <Button variant="primary" loading={invoke.isPending} onClick={() => invoke.mutate()}>
                    Invoke tool
                  </Button>
                </div>
              </>
            ) : testing.requires_approval ? (
              <p className="rounded-xl border border-state-warn/25 bg-state-warn/10 px-3 py-2 text-2xs text-state-warn">
                This tool is gated by human approval and can only run inside an agent execution where the
                approval workflow is enforced.
              </p>
            ) : null}

            {result ? (
              <div>
                <p className="metric-label mb-1.5">Result</p>
                <JsonView data={result} maxHeight="max-h-72" />
              </div>
            ) : null}
          </div>
        </Modal>
      ) : null}
    </div>
  )
}
