import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import {
  ArrowDownTrayIcon,
  ArrowLeftIcon,
  BoltIcon,
  ClipboardDocumentIcon,
  StopCircleIcon,
} from '@heroicons/react/24/outline'
import { api, subscribeToExecution, type Citation, type Execution, type ExecutionEvent, type ExecutionGraph, type GuardrailFinding, type LogRecord, type PlanStep, type ToolCallSummary, type Trace, type ValidationFinding, Badge, Button, Card, CardHeader, EmptyState, ErrorState, JsonView, Skeleton, StatusDot, TabPanel, Tabs, useAuth, useToasts, cn, copyToClipboard, downloadFile, formatCurrency, formatDateTime, formatDuration, formatNumber, formatTime, titleCase } from '@finops/shared'
import { TraceTimeline } from '@/components/executions/TraceTimeline'
import { ExecutionFlow } from '@/components/executions/ExecutionFlow'

const EVENT_TONE: Record<string, 'ok' | 'warn' | 'err' | 'info' | 'idle'> = {
  'execution.started': 'info',
  'execution.completed': 'ok',
  'execution.failed': 'err',
  'execution.cancelled': 'err',
  'execution.suspended': 'warn',
  'node.started': 'idle',
  'node.completed': 'idle',
  'node.failed': 'err',
  planning: 'info',
  reasoning: 'info',
  'knowledge.search': 'info',
  'vector.search': 'info',
  'memory.retrieval': 'info',
  'database.query': 'idle',
  'api.call': 'idle',
  'llm.started': 'info',
  'llm.completed': 'ok',
  'tool.started': 'info',
  'tool.completed': 'ok',
  'tool.failed': 'err',
  retry: 'warn',
  guardrail: 'warn',
  validation: 'info',
  'approval.requested': 'warn',
  'final.response': 'ok',
}

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'timeout'])

export default function ExecutionDetail() {
  const { executionId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') ?? 'stream'
  const queryClient = useQueryClient()
  const push = useToasts((state) => state.push)
  const { can } = useAuth()
  const [events, setEvents] = useState<ExecutionEvent[]>([])
  const [streaming, setStreaming] = useState(false)
  const streamRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)

  const execution = useQuery({
    queryKey: ['execution', executionId],
    queryFn: () => api.get<Execution>(`/executions/${executionId}`),
    refetchInterval: (query) => (TERMINAL.has(query.state.data?.status ?? '') ? false : 4000),
  })

  const live = !TERMINAL.has(execution.data?.status ?? '')

  const trace = useQuery({
    queryKey: ['trace', executionId, execution.data?.status],
    queryFn: () => api.get<Trace>(`/executions/${executionId}/trace`),
    enabled: Boolean(execution.data),
  })

  const graph = useQuery({
    queryKey: ['exec-graph', executionId, execution.data?.status],
    queryFn: () => api.get<ExecutionGraph>(`/executions/${executionId}/graph`),
    enabled: Boolean(execution.data),
  })

  const logs = useQuery({
    queryKey: ['exec-logs', executionId, execution.data?.status],
    queryFn: () => api.get<LogRecord[]>(`/executions/${executionId}/logs`),
    enabled: Boolean(execution.data) && tab === 'logs',
  })

  const cancel = useMutation({
    mutationFn: () => api.post(`/executions/${executionId}/cancel`),
    onSuccess: () => {
      push({ title: 'Cancellation requested', tone: 'warn' })
      void queryClient.invalidateQueries({ queryKey: ['execution', executionId] })
    },
  })

  const evaluate = useMutation({
    mutationFn: () => api.post(`/evaluations/${executionId}/run`),
    onSuccess: () => push({ title: 'Evaluation complete', tone: 'ok' }),
    onError: (error) => push({ title: 'Evaluation failed', description: (error as Error).message, tone: 'err' }),
  })

  // Live event stream (SSE) with replay from sequence 0.
  useEffect(() => {
    if (!executionId) return
    setEvents([])
    const unsubscribe = subscribeToExecution(executionId, {
      onOpen: () => setStreaming(true),
      onError: () => setStreaming(false),
      onEvent: (event) => {
        if (!event?.type || event.type === 'heartbeat') return
        setEvents((current) => {
          if (current.some((item) => item.sequence === event.sequence)) return current
          return [...current, event].sort((a, b) => a.sequence - b.sequence)
        })
        if (['execution.completed', 'execution.failed', 'execution.suspended'].includes(event.type)) {
          void queryClient.invalidateQueries({ queryKey: ['execution', executionId] })
          void queryClient.invalidateQueries({ queryKey: ['trace', executionId] })
          void queryClient.invalidateQueries({ queryKey: ['exec-graph', executionId] })
          setStreaming(false)
        }
      },
    })
    return () => {
      unsubscribe()
      setStreaming(false)
    }
  }, [executionId, queryClient])

  useEffect(() => {
    if (autoScroll && streamRef.current) {
      streamRef.current.scrollTop = streamRef.current.scrollHeight
    }
  }, [events, autoScroll])

  const data = execution.data
  const summary = useMemo(
    () =>
      data
        ? [
            ['Status', <Badge key="s" status={data.status}>{data.status.replace('_', ' ')}</Badge>],
            ['Latency', formatDuration(data.latency_ms)],
            ['Queue wait', formatDuration(data.queue_ms)],
            ['Cost', formatCurrency(data.cost_usd, 5)],
            ['Tokens', formatNumber(data.tokens.total)],
            ['LLM calls', formatNumber(data.llm_call_count)],
            ['Tool calls', formatNumber(data.tool_call_count)],
            ['Retries', formatNumber(data.retry_count)],
          ]
        : [],
    [data],
  )

  if (execution.isError) return <ErrorState error={execution.error} retry={() => execution.refetch()} />

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            to="/executions"
            className="inline-flex items-center gap-1.5 text-2xs text-ink-muted transition-colors hover:text-ink"
          >
            <ArrowLeftIcon className="h-3 w-3" /> Executions
          </Link>
          <h1 className="mt-1.5 flex items-center gap-2.5 text-lg font-semibold tracking-tight">
            {data ? <StatusDot status={data.status} pulse={live} /> : null}
            {data?.agent_key ? titleCase(data.agent_key) : <Skeleton className="h-6 w-40" />}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-ink-muted">
            <button
              onClick={() => copyToClipboard(executionId)}
              className="font-mono transition-colors hover:text-ink"
              title="Copy execution id"
            >
              {executionId.slice(0, 8)}…
            </button>
            {data ? (
              <>
                <span className="font-mono">trace {data.trace_id.slice(0, 12)}…</span>
                <span>{data.model ?? 'no model'}</span>
                <span>{data.user_email ?? 'system'}</span>
                <span>{formatDateTime(data.created_at)}</span>
              </>
            ) : null}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {streaming ? (
            <Badge tone="info" dot>
              streaming
            </Badge>
          ) : null}
          {data && live && can('execution:cancel') ? (
            <Button size="sm" variant="danger" onClick={() => cancel.mutate()} loading={cancel.isPending}>
              <StopCircleIcon className="h-3.5 w-3.5" /> Cancel
            </Button>
          ) : null}
          {data?.status === 'succeeded' && can('eval:run') ? (
            <Button size="sm" onClick={() => evaluate.mutate()} loading={evaluate.isPending}>
              <BoltIcon className="h-3.5 w-3.5" /> Evaluate
            </Button>
          ) : null}
          {data ? (
            <Button
              size="sm"
              onClick={() =>
                downloadFile(
                  JSON.stringify({ execution: data, trace: trace.data, events }, null, 2),
                  `execution-${executionId}.json`,
                )
              }
            >
              <ArrowDownTrayIcon className="h-3.5 w-3.5" /> Export
            </Button>
          ) : null}
        </div>
      </div>

      {/* Summary strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
        {summary.map(([label, value]) => (
          <Card key={label as string} className="px-3.5 py-3">
            <p className="metric-label">{label as string}</p>
            <div className="mt-1 text-sm font-semibold tabular-nums">{value as React.ReactNode}</div>
          </Card>
        ))}
      </div>

      {data?.error ? (
        <Card className="border-state-err/30 bg-state-err/5 px-4 py-3">
          <p className="metric-label text-state-err">{data.error_type ?? 'Error'}</p>
          <p className="mt-1 font-mono text-xs text-state-err">{data.error}</p>
        </Card>
      ) : null}

      <Tabs
        value={tab}
        onValueChange={(value) => setSearchParams({ tab: value }, { replace: true })}
        tabs={[
          { value: 'stream', label: 'Live stream', count: events.length },
          { value: 'trace', label: 'Trace', count: trace.data?.span_count },
          { value: 'graph', label: 'Execution graph' },
          { value: 'output', label: 'Output' },
          { value: 'logs', label: 'Logs', count: logs.data?.length },
          { value: 'input', label: 'Input' },
        ]}
      >
        {/* --- Live stream --- */}
        <TabPanel value="stream">
          <Card className="overflow-hidden">
            <CardHeader
              title="Execution stream"
              subtitle="Planning, reasoning, retrieval, tool calls, approvals and completion, as they happen"
              action={
                <label className="flex items-center gap-2 text-2xs text-ink-muted">
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={(event) => setAutoScroll(event.target.checked)}
                    className="h-3 w-3 rounded border-line"
                  />
                  Follow
                </label>
              }
            />
            <div ref={streamRef} className="max-h-[560px] space-y-1.5 overflow-y-auto px-4 pb-4 pt-3">
              {events.length === 0 ? (
                <EmptyState
                  title={live ? 'Waiting for the first event' : 'No events recorded'}
                  description={
                    live
                      ? 'Events appear here as the agent plans, retrieves, calls tools and answers.'
                      : 'This execution produced no event log.'
                  }
                />
              ) : (
                <AnimatePresence initial={false}>
                  {events.map((event) => (
                    <motion.div
                      key={event.sequence}
                      initial={{ opacity: 0, x: -6 }}
                      animate={{ opacity: 1, x: 0 }}
                      className="flex gap-3 rounded-lg px-2 py-1.5 transition-colors hover:bg-accent-soft/50"
                    >
                      <span className="w-16 shrink-0 pt-0.5 font-mono text-2xs tabular-nums text-ink-subtle">
                        {formatTime(event.timestamp)}
                      </span>
                      <Badge tone={EVENT_TONE[event.type] ?? 'idle'} className="h-fit shrink-0">
                        {event.type}
                      </Badge>
                      <div className="min-w-0 flex-1">
                        <EventBody event={event} />
                      </div>
                    </motion.div>
                  ))}
                </AnimatePresence>
              )}
            </div>
          </Card>
        </TabPanel>

        {/* --- Trace --- */}
        <TabPanel value="trace">
          {trace.isLoading ? (
            <Card className="p-6">
              <Skeleton className="h-64 w-full" />
            </Card>
          ) : trace.data && trace.data.spans.length > 0 ? (
            <TraceTimeline trace={trace.data} />
          ) : (
            <Card>
              <EmptyState title="No spans yet" description="Spans appear as soon as the first node starts." />
            </Card>
          )}
        </TabPanel>

        {/* --- Graph --- */}
        <TabPanel value="graph">
          {graph.data ? (
            <ExecutionFlow graph={graph.data} />
          ) : (
            <Card className="p-6">
              <Skeleton className="h-72 w-full" />
            </Card>
          )}
        </TabPanel>

        {/* --- Output --- */}
        <TabPanel value="output">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="lg:col-span-2">
              <CardHeader
                title="Final response"
                action={
                  data?.final_response ? (
                    <Button size="sm" variant="ghost" onClick={() => copyToClipboard(data.final_response ?? '')}>
                      <ClipboardDocumentIcon className="h-3.5 w-3.5" /> Copy
                    </Button>
                  ) : null
                }
              />
              <div className="px-5 pb-5 pt-3">
                {data?.final_response ? (
                  <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{data.final_response}</p>
                ) : (
                  <p className="text-xs text-ink-muted">No response produced.</p>
                )}
              </div>
            </Card>

            {Array.isArray(data?.output?.citations) && (data?.output?.citations as unknown[]).length > 0 ? (
              <Card>
                <CardHeader title="Citations" subtitle="Sources retrieved for this answer" />
                <ul className="space-y-2 px-5 pb-5 pt-3">
                  {(data!.output!.citations as Citation[]).map((citation) => (
                    <li key={citation.id} className="rounded-xl border border-line/70 px-3 py-2">
                      <p className="text-xs font-medium">
                        <span className="mr-1.5 font-mono text-ink-subtle">{citation.id}</span>
                        {citation.title}
                      </p>
                      <p className="mt-1 line-clamp-2 text-2xs text-ink-muted">{citation.excerpt}</p>
                      <p className="mt-1 text-2xs text-ink-subtle">
                        {citation.source} · score {citation.score}
                      </p>
                    </li>
                  ))}
                </ul>
              </Card>
            ) : null}

            {data?.plan ? (
              <Card>
                <CardHeader title="Plan" subtitle="Produced by the planner node" />
                <div className="px-5 pb-5 pt-3">
                  <JsonView data={data.plan} />
                </div>
              </Card>
            ) : null}

            {data?.output ? (
              <Card className="lg:col-span-2">
                <CardHeader title="Structured output" subtitle="Validation, guardrails, tool calls and usage" />
                <div className="px-5 pb-5 pt-3">
                  <JsonView data={data.output} maxHeight="max-h-[420px]" />
                </div>
              </Card>
            ) : null}
          </div>
        </TabPanel>

        {/* --- Logs --- */}
        <TabPanel value="logs">
          <Card className="overflow-hidden">
            <CardHeader title="Correlated logs" subtitle="Structured records emitted by this execution" />
            <div className="max-h-[560px] overflow-y-auto px-4 pb-4 pt-2 font-mono text-2xs">
              {logs.isLoading ? (
                <Skeleton className="h-40 w-full" />
              ) : logs.data?.length ? (
                logs.data.map((record, index) => (
                  <div key={index} className="flex gap-3 border-b border-line/40 py-1.5 last:border-0">
                    <span className="w-20 shrink-0 text-ink-subtle">{formatTime(record.timestamp)}</span>
                    <span
                      className={cn(
                        'w-14 shrink-0 font-semibold',
                        record.level === 'ERROR' || record.level === 'CRITICAL'
                          ? 'text-state-err'
                          : record.level === 'WARNING'
                            ? 'text-state-warn'
                            : 'text-ink-subtle',
                      )}
                    >
                      {record.level}
                    </span>
                    <span className="min-w-0 flex-1 whitespace-pre-wrap text-ink-muted">{record.message}</span>
                  </div>
                ))
              ) : (
                <EmptyState title="No logs" />
              )}
            </div>
          </Card>
        </TabPanel>

        {/* --- Input --- */}
        <TabPanel value="input">
          <Card>
            <CardHeader title="Request payload" subtitle={`Trigger: ${data?.trigger ?? '—'}`} />
            <div className="px-5 pb-5 pt-3">
              <JsonView data={data?.input ?? {}} />
            </div>
          </Card>
        </TabPanel>
      </Tabs>
    </div>
  )
}

function EventBody({ event }: { event: ExecutionEvent }) {
  const payload = event.payload ?? {}
  switch (event.type) {
    case 'planning':
      return (
        <div>
          <p className="text-xs text-ink">{payload.plan?.objective ?? 'Plan created'}</p>
          {Array.isArray(payload.plan?.steps) && payload.plan.steps.length > 0 ? (
            <ol className="mt-1 space-y-0.5">
              {(payload.plan.steps as PlanStep[]).map((step, index) => (
                <li key={index} className="text-2xs text-ink-muted">
                  {step.step ?? index + 1}. {step.action}
                  {step.tool ? <span className="ml-1 font-mono text-ink-subtle">({step.tool})</span> : null}
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      )
    case 'reasoning':
      return <p className="line-clamp-4 whitespace-pre-wrap text-xs text-ink-muted">{payload.text}</p>
    case 'knowledge.search':
      return (
        <p className="text-xs text-ink-muted">
          <span className="text-ink">{payload.results}</span> chunks for “{payload.query}” via{' '}
          <span className="font-mono">{payload.backend}</span> in {formatDuration(payload.latency_ms)}
        </p>
      )
    case 'llm.completed':
      return (
        <p className="text-xs text-ink-muted">
          <span className="font-mono text-ink">{payload.model}</span> · {formatDuration(payload.latency_ms)} ·{' '}
          {payload.tokens?.input}/{payload.tokens?.output} tokens · {formatCurrency(payload.cost_usd, 5)}
          {payload.tool_calls?.length ? (
            <span className="ml-1 text-ink">
              → {(payload.tool_calls as ToolCallSummary[]).map((call) => call.name).join(', ')}
            </span>
          ) : null}
        </p>
      )
    case 'tool.started':
      return (
        <p className="text-xs text-ink-muted">
          <span className="font-mono text-ink">{payload.tool}</span>{' '}
          <span className="text-ink-subtle">{JSON.stringify(payload.arguments).slice(0, 140)}</span>
        </p>
      )
    case 'tool.completed':
    case 'tool.failed':
      return (
        <p className="text-xs text-ink-muted">
          <span className="font-mono text-ink">{payload.tool}</span> · {formatDuration(payload.latency_ms)}
          {payload.retries ? ` · ${payload.retries} retries` : ''}
          {payload.ok === false ? <span className="ml-1 text-state-err">{payload.result_preview}</span> : null}
        </p>
      )
    case 'approval.requested':
      return (
        <p className="text-xs">
          <span className="text-state-warn">{payload.title}</span>{' '}
          <Link to="/approvals" className="text-state-info hover:underline">
            review →
          </Link>
        </p>
      )
    case 'guardrail':
      return (
        <p className="text-xs text-ink-muted">
          {Array.isArray(payload.findings) && payload.findings.length
            ? (payload.findings as GuardrailFinding[]).map((finding) => `${finding.rule}: ${finding.action}`).join(', ')
            : 'No findings'}
        </p>
      )
    case 'validation':
      return (
        <p className="text-xs text-ink-muted">
          {Array.isArray(payload.findings)
            ? (payload.findings as ValidationFinding[]).map((finding) => `${finding.check}=${finding.status}`).join(' · ')
            : ''}
        </p>
      )
    case 'final.response':
      return <p className="line-clamp-3 whitespace-pre-wrap text-xs text-ink">{payload.response}</p>
    case 'execution.completed':
      return (
        <p className="text-xs text-ink-muted">
          Completed in {formatDuration(payload.latency_ms)} · {formatCurrency(payload.cost_usd, 5)} ·{' '}
          {formatNumber(payload.tokens)} tokens
        </p>
      )
    case 'execution.failed':
      return <p className="text-xs text-state-err">{payload.error}</p>
    default:
      return (
        <p className="text-xs text-ink-muted">
          {payload.label ?? payload.node ?? Object.keys(payload).slice(0, 4).join(', ') ?? ''}
        </p>
      )
  }
}
