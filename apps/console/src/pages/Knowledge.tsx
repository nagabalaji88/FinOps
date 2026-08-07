import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowPathIcon, CloudArrowUpIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline'
import { api, Badge, Button, Card, CardHeader, ErrorState, Meter, PageHeader, SkeletonCard, useAuth, useToasts, formatDuration, formatNumber, relativeTime } from '@finops/shared'

interface KnowledgeSource {
  key: string
  name: string
  connector: string
  status: string
  enabled: boolean
  documents: number
  chunks: number
  embedding_model: string | null
  vector_dimensions: number
  classification: string
  last_error: string | null
  last_sync_at: string | null
}

interface KnowledgeDashboard {
  sources: KnowledgeSource[]
  connectors: { key: string; label: string; configured: boolean; missing_settings: string[] }[]
  corpus: {
    documents: number
    chunks: number
    embedded_chunks: number
    embedding_coverage_pct: number
    vector_dimensions: number
    vector_backend: string
  }
  search: { queries_7d: number; avg_latency_ms: number; avg_results: number }
}

export default function Knowledge() {
  const queryClient = useQueryClient()
  const push = useToasts((state) => state.push)
  const { can } = useAuth()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<any[] | null>(null)
  const [uploadTarget, setUploadTarget] = useState<string>('runbooks')
  const fileInput = useRef<HTMLInputElement>(null)

  const dashboard = useQuery({
    queryKey: ['knowledge'],
    queryFn: () => api.get<KnowledgeDashboard>('/dashboard/knowledge'),
    refetchInterval: 30_000,
  })

  const search = useMutation({
    mutationFn: () => api.post<{ results: any[]; latency_ms: number; backend: string; method: string }>(
      '/knowledge/search',
      { query, top_k: 8 },
    ),
    onSuccess: (response) => setResults(response.results),
    onError: (error) => push({ title: 'Search failed', description: (error as Error).message, tone: 'err' }),
  })

  const sync = useMutation({
    mutationFn: (key: string) => api.post(`/knowledge/sources/${key}/sync`),
    onSuccess: (response: any) => {
      push({
        title: response.status === 'connected' ? 'Source synced' : 'Sync skipped',
        description: response.missing ? `Missing: ${response.missing.join(', ')}` : undefined,
        tone: response.status === 'connected' ? 'ok' : 'warn',
      })
      void queryClient.invalidateQueries({ queryKey: ['knowledge'] })
    },
    onError: (error) => push({ title: 'Sync failed', description: (error as Error).message, tone: 'err' }),
  })

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      form.append('title', file.name)
      return api.upload(`/knowledge/sources/${uploadTarget}/documents`, form)
    },
    onSuccess: (response: any) => {
      push({ title: 'Document indexed', description: `${response.chunks} chunks embedded`, tone: 'ok' })
      void queryClient.invalidateQueries({ queryKey: ['knowledge'] })
    },
    onError: (error) => push({ title: 'Upload failed', description: (error as Error).message, tone: 'err' }),
  })

  if (dashboard.isError) return <ErrorState error={dashboard.error} retry={() => dashboard.refetch()} />
  const data = dashboard.data

  return (
    <div className="space-y-5">
      <PageHeader
        title="Knowledge"
        description="Connected sources, embedding coverage and retrieval quality across the enterprise corpus."
      />

      {!data ? (
        <div className="grid gap-4 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <SkeletonCard key={index} rows={1} />
          ))}
        </div>
      ) : (
        <>
          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {[
              ['Documents', formatNumber(data.corpus.documents), `${data.sources.length} sources`],
              ['Chunks', formatNumber(data.corpus.chunks), `${data.corpus.vector_dimensions}-dim vectors`],
              [
                'Embedding coverage',
                `${data.corpus.embedding_coverage_pct}%`,
                `${formatNumber(data.corpus.embedded_chunks)} embedded`,
              ],
              [
                'Search latency',
                formatDuration(data.search.avg_latency_ms),
                `${formatNumber(data.search.queries_7d)} queries / 7d`,
              ],
            ].map(([label, value, hint]) => (
              <Card key={label} className="px-5 py-4">
                <p className="metric-label">{label}</p>
                <p className="metric-value mt-2">{value}</p>
                <p className="mt-1.5 text-2xs text-ink-subtle">{hint}</p>
              </Card>
            ))}
          </section>

          <Card>
            <CardHeader
              title="Semantic search"
              subtitle={`Hybrid vector + keyword retrieval · backend ${data.corpus.vector_backend}`}
            />
            <form
              className="flex flex-col gap-2 px-5 pb-4 pt-3 sm:flex-row"
              onSubmit={(event) => {
                event.preventDefault()
                if (query.trim()) search.mutate()
              }}
            >
              <input
                className="input flex-1"
                placeholder="Ask the corpus, e.g. “what is the SAR filing deadline?”"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Search the knowledge base"
              />
              <Button type="submit" variant="primary" loading={search.isPending}>
                <MagnifyingGlassIcon className="h-4 w-4" /> Search
              </Button>
            </form>
            {results ? (
              <div className="space-y-2 px-5 pb-5">
                {results.length === 0 ? (
                  <p className="text-xs text-ink-muted">No matches above the score threshold.</p>
                ) : (
                  results.map((result) => (
                    <div key={result.chunk_id} className="rounded-xl border border-line/70 px-3 py-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <p className="truncate text-xs font-medium">{result.title}</p>
                        <Badge tone="idle">{result.score}</Badge>
                      </div>
                      <p className="mt-1 line-clamp-3 text-2xs leading-relaxed text-ink-muted">{result.content}</p>
                      <p className="mt-1 text-2xs text-ink-subtle">
                        {result.source}
                        {result.heading ? ` › ${result.heading}` : ''} · chunk {result.chunk_index}
                      </p>
                    </div>
                  ))
                )}
              </div>
            ) : null}
          </Card>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2 overflow-hidden">
              <CardHeader
                title="Connected sources"
                subtitle="Sync status, document and chunk counts"
                action={
                  can('knowledge:write') ? (
                    <div className="flex items-center gap-2">
                      <select
                        className="input w-36 py-1 text-2xs"
                        value={uploadTarget}
                        onChange={(event) => setUploadTarget(event.target.value)}
                        aria-label="Upload target source"
                      >
                        {data.sources.map((source) => (
                          <option key={source.key} value={source.key}>
                            {source.name}
                          </option>
                        ))}
                      </select>
                      <input
                        ref={fileInput}
                        type="file"
                        className="hidden"
                        accept=".md,.txt,.json,.csv,.pdf"
                        onChange={(event) => {
                          const file = event.target.files?.[0]
                          if (file) upload.mutate(file)
                          event.target.value = ''
                        }}
                      />
                      <Button size="sm" loading={upload.isPending} onClick={() => fileInput.current?.click()}>
                        <CloudArrowUpIcon className="h-3.5 w-3.5" /> Upload
                      </Button>
                    </div>
                  ) : null
                }
              />
              <div className="scroll-x">
                <table className="w-full min-w-[720px] text-xs">
                  <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
                    <tr>
                      {['Source', 'Connector', 'Status', 'Docs', 'Chunks', 'Embedding', 'Last sync', ''].map((header) => (
                        <th key={header} className="px-4 py-2.5 text-left font-medium">
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.sources.map((source) => (
                      <tr key={source.key} className="table-row">
                        <td className="px-4 py-2.5">
                          <p className="font-medium">{source.name}</p>
                          <p className="text-2xs text-ink-subtle">{source.classification}</p>
                        </td>
                        <td className="px-4 py-2.5 font-mono text-2xs text-ink-muted">{source.connector}</td>
                        <td className="px-4 py-2.5">
                          <Badge status={source.status}>{source.status.replace('_', ' ')}</Badge>
                        </td>
                        <td className="px-4 py-2.5 tabular-nums">{formatNumber(source.documents)}</td>
                        <td className="px-4 py-2.5 tabular-nums">{formatNumber(source.chunks)}</td>
                        <td className="px-4 py-2.5 font-mono text-2xs text-ink-muted">
                          {source.embedding_model ?? '—'}
                        </td>
                        <td className="px-4 py-2.5 text-ink-muted">{relativeTime(source.last_sync_at)}</td>
                        <td className="px-4 py-2.5 text-right">
                          {can('knowledge:write') ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => sync.mutate(source.key)}
                              loading={sync.isPending && sync.variables === source.key}
                              aria-label={`Sync ${source.name}`}
                            >
                              <ArrowPathIcon className="h-3.5 w-3.5" />
                            </Button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card>
              <CardHeader title="Connectors" subtitle="Configuration status" />
              <div className="space-y-2 px-5 pb-5 pt-3">
                {data.connectors.map((connector) => (
                  <div key={connector.key} className="rounded-xl border border-line/70 px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs">{connector.label}</span>
                      <Badge tone={connector.configured ? 'ok' : 'idle'}>
                        {connector.configured ? 'configured' : 'not configured'}
                      </Badge>
                    </div>
                    {connector.missing_settings.length > 0 ? (
                      <p className="mt-1 font-mono text-2xs text-ink-subtle">
                        needs {connector.missing_settings.join(', ')}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <Card>
            <CardHeader title="Embedding coverage" subtitle="Chunks with a stored vector" />
            <div className="px-5 pb-5 pt-3">
              <Meter
                value={data.corpus.embedding_coverage_pct}
                tone={data.corpus.embedding_coverage_pct > 95 ? 'ok' : 'warn'}
              />
              <p className="mt-2 text-2xs text-ink-subtle">
                {formatNumber(data.corpus.embedded_chunks)} of {formatNumber(data.corpus.chunks)} chunks embedded at{' '}
                {data.corpus.vector_dimensions} dimensions.
              </p>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
