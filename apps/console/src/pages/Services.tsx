import { useQuery } from '@tanstack/react-query'
import { api, type ServiceStatus, Badge, Card, CardHeader, ErrorState, PageHeader, SkeletonCard, formatDuration, titleCase } from '@finops/shared'

interface ServicesResponse {
  generated_at: string
  summary: { total: number; healthy: number; not_configured: number; unhealthy: number }
  services: ServiceStatus[]
  circuit_breakers: {
    name: string
    state: string
    consecutive_failures: number
    total_failures: number
    total_successes: number
    total_rejections: number
  }[]
}

const CATEGORY_LABEL: Record<string, string> = {
  ai_provider: 'AI providers',
  datastore: 'Data stores',
  cache: 'Cache',
  streaming: 'Streaming',
  vector: 'Vector store',
  graph: 'Graph',
  search: 'Search',
  object_store: 'Object storage',
  secrets: 'Secrets',
  identity: 'Identity',
  observability: 'Observability',
  orchestration: 'Orchestration',
  workers: 'Workers',
  knowledge_connector: 'Knowledge connectors',
}

export default function Services() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['services'],
    queryFn: () => api.get<ServicesResponse>('/services'),
    refetchInterval: 30_000,
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  const grouped = (data?.services ?? []).reduce<Record<string, ServiceStatus[]>>((accumulator, service) => {
    const key = service.category ?? 'other'
    accumulator[key] = [...(accumulator[key] ?? []), service]
    return accumulator
  }, {})

  return (
    <div className="space-y-5">
      <PageHeader
        title="Connected services"
        description="Live health of every provider, datastore and integration this deployment depends on."
        actions={
          data ? (
            <div className="flex gap-2">
              <Badge tone="ok">{data.summary.healthy} healthy</Badge>
              <Badge tone="idle">{data.summary.not_configured} not configured</Badge>
              {data.summary.unhealthy > 0 ? <Badge tone="err">{data.summary.unhealthy} unhealthy</Badge> : null}
            </div>
          ) : null
        }
      />

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2">
          {Array.from({ length: 4 }).map((_, index) => (
            <SkeletonCard key={index} rows={4} />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {Object.entries(grouped).map(([category, services]) => (
              <Card key={category}>
                <CardHeader title={CATEGORY_LABEL[category] ?? titleCase(category)} subtitle={`${services.length} services`} />
                <div className="space-y-2 px-5 pb-5 pt-3">
                  {services.map((service) => (
                    <div key={service.name} className="rounded-xl border border-line/70 px-3 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-xs font-medium">{titleCase(service.name)}</span>
                        <Badge status={service.status}>{service.status.replace('_', ' ')}</Badge>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-ink-subtle">
                        {service.latency_ms !== undefined ? <span>{formatDuration(service.latency_ms)}</span> : null}
                        {service.models?.length ? <span>{service.models.length} models</span> : null}
                        {typeof service.backend === 'string' ? <span>backend {service.backend}</span> : null}
                        {typeof service.points === 'number' ? <span>{service.points} vectors</span> : null}
                      </div>
                      {service.required?.length ? (
                        <p className="mt-1 font-mono text-2xs text-ink-subtle">needs {service.required.join(', ')}</p>
                      ) : null}
                      {service.error ? <p className="mt-1 text-2xs text-state-err">{service.error}</p> : null}
                    </div>
                  ))}
                </div>
              </Card>
            ))}
          </div>

          <Card className="overflow-hidden">
            <CardHeader title="Circuit breakers" subtitle="Per-provider and per-tool failure isolation" />
            {data && data.circuit_breakers.length > 0 ? (
              <div className="scroll-x">
                <table className="w-full min-w-[560px] text-xs">
                  <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
                    <tr>
                      {['Circuit', 'State', 'Consecutive failures', 'Failures', 'Successes', 'Rejected'].map((header) => (
                        <th key={header} className="px-4 py-2.5 text-left font-medium">
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.circuit_breakers.map((breaker) => (
                      <tr key={breaker.name} className="table-row">
                        <td className="px-4 py-2.5 font-mono text-2xs">{breaker.name}</td>
                        <td className="px-4 py-2.5">
                          <Badge tone={breaker.state === 'closed' ? 'ok' : breaker.state === 'open' ? 'err' : 'warn'}>
                            {breaker.state}
                          </Badge>
                        </td>
                        <td className="px-4 py-2.5 tabular-nums">{breaker.consecutive_failures}</td>
                        <td className="px-4 py-2.5 tabular-nums">{breaker.total_failures}</td>
                        <td className="px-4 py-2.5 tabular-nums">{breaker.total_successes}</td>
                        <td className="px-4 py-2.5 tabular-nums">{breaker.total_rejections}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="px-5 pb-5 pt-3 text-xs text-ink-muted">No circuits have been exercised yet.</p>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
