import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ArrowDownTrayIcon, PauseIcon, PlayIcon } from '@heroicons/react/24/outline'
import { api, tokenStore, type LogRecord, Badge, Button, Card, EmptyState, ErrorState, JsonView, PageHeader, Skeleton, cn, formatDateTime, formatTime } from '@finops/shared'

const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

export default function Logs() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [level, setLevel] = useState(searchParams.get('level') ?? '')
  const agent = searchParams.get('agent') ?? ''
  const [following, setFollowing] = useState(false)
  const [liveRecords, setLiveRecords] = useState<LogRecord[]>([])
  const [selected, setSelected] = useState<LogRecord | null>(null)
  const [viewMode, setViewMode] = useState<'table' | 'json'>('table')
  const containerRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['logs', query, level, agent],
    queryFn: () =>
      api.get<{ total: number; items: LogRecord[] }>('/logs', {
        q: query || undefined,
        level: level || undefined,
        agent_key: agent || undefined,
        limit: 300,
      }),
    refetchInterval: following ? false : 15_000,
  })

  useEffect(() => {
    if (!following) return
    const controller = new AbortController()
    const run = async () => {
      const token = tokenStore.read().access
      const url = new URL('/api/v1/logs/stream', window.location.origin)
      if (level) url.searchParams.set('level', level)
      if (agent) url.searchParams.set('agent_key', agent)
      const response = await fetch(url.toString(), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: controller.signal,
      })
      if (!response.body) return
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          const line = frame.split('\n').find((item) => item.startsWith('data:'))
          if (!line) continue
          try {
            const record = JSON.parse(line.slice(5).trim())
            if (record?.message) setLiveRecords((current) => [...current.slice(-400), record])
          } catch {
            /* keep-alive */
          }
        }
      }
    }
    void run().catch(() => setFollowing(false))
    return () => controller.abort()
  }, [following, level, agent])

  useEffect(() => {
    if (following && containerRef.current) containerRef.current.scrollTop = containerRef.current.scrollHeight
  }, [liveRecords, following])

  const records = following ? liveRecords : (data?.items ?? [])

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div className="space-y-5">
      <PageHeader
        title="Logs"
        description="Structured, correlation-tagged logs across every agent and execution."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="input w-48"
              placeholder="Search message…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search logs"
            />
            <select
              className="input w-36"
              value={level}
              onChange={(event) => {
                setLevel(event.target.value)
                setSearchParams(event.target.value ? { level: event.target.value } : {})
              }}
              aria-label="Filter by level"
            >
              {LEVELS.map((value) => (
                <option key={value} value={value}>
                  {value || 'All levels'}
                </option>
              ))}
            </select>
            <Button size="sm" variant={following ? 'primary' : 'outline'} onClick={() => setFollowing(!following)}>
              {following ? <PauseIcon className="h-3.5 w-3.5" /> : <PlayIcon className="h-3.5 w-3.5" />}
              {following ? 'Streaming' : 'Follow'}
            </Button>
            <Button
              size="sm"
              onClick={() => {
                const token = tokenStore.read().access
                const url = `/api/v1/logs/export?fmt=jsonl&since_minutes=1440`
                fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
                  .then((response) => response.blob())
                  .then((blob) => {
                    const href = URL.createObjectURL(blob)
                    const anchor = document.createElement('a')
                    anchor.href = href
                    anchor.download = 'finops-logs.jsonl'
                    anchor.click()
                    URL.revokeObjectURL(href)
                  })
              }}
            >
              <ArrowDownTrayIcon className="h-3.5 w-3.5" /> Download
            </Button>
            <div className="flex rounded-xl border border-line/70 p-0.5">
              {(['table', 'json'] as const).map((mode) => (
                <button
                  key={mode}
                  onClick={() => setViewMode(mode)}
                  className={cn(
                    'rounded-lg px-2.5 py-1 text-2xs',
                    viewMode === mode ? 'bg-accent-soft text-ink' : 'text-ink-muted',
                  )}
                >
                  {mode.toUpperCase()}
                </button>
              ))}
            </div>
          </div>
        }
      />

      <Card className="overflow-hidden">
        <div ref={containerRef} className="max-h-[640px] overflow-y-auto">
          {isLoading && !following ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 10 }).map((_, index) => (
                <Skeleton key={index} className="h-4 w-full" />
              ))}
            </div>
          ) : records.length === 0 ? (
            <EmptyState title="No log records" description="Adjust the filters or run an agent." />
          ) : viewMode === 'json' ? (
            <div className="p-4">
              <JsonView data={records} maxHeight="max-h-[560px]" />
            </div>
          ) : (
            <table className="w-full text-2xs">
              <thead className="sticky top-0 z-10 border-b border-line/70 bg-surface-raised/95 uppercase tracking-wider text-ink-subtle backdrop-blur">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Time</th>
                  <th className="px-2 py-2 text-left font-medium">Level</th>
                  <th className="px-2 py-2 text-left font-medium">Logger</th>
                  <th className="px-2 py-2 text-left font-medium">Message</th>
                  <th className="px-2 py-2 text-left font-medium">Correlation</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {records.map((record, index) => (
                  <tr key={record.id ?? index} className="table-row cursor-pointer" onClick={() => setSelected(record)}>
                    <td className="whitespace-nowrap px-4 py-1.5 text-ink-subtle">{formatTime(record.timestamp)}</td>
                    <td className="px-2 py-1.5">
                      <Badge
                        tone={
                          record.level === 'ERROR' || record.level === 'CRITICAL'
                            ? 'err'
                            : record.level === 'WARNING'
                              ? 'warn'
                              : 'idle'
                        }
                      >
                        {record.level}
                      </Badge>
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-ink-subtle">{record.logger}</td>
                    <td className="px-2 py-1.5 text-ink-muted">{record.message}</td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-ink-subtle">
                      {record.correlation_id?.slice(0, 10) ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {!following && data ? (
          <div className="border-t border-line/60 px-4 py-2 text-2xs text-ink-subtle">
            Showing {records.length} of {data.total} records
          </div>
        ) : null}
      </Card>

      {selected ? (
        <Card className="p-4">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-xs font-medium">{formatDateTime(selected.timestamp)}</p>
            <button className="text-2xs text-ink-subtle hover:text-ink" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
          <JsonView data={selected} />
        </Card>
      ) : null}
    </div>
  )
}
