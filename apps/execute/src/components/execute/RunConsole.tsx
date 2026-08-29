/**
 * The run console: input, live execution, human gate, result.
 *
 * The input form is generated from the agent's own declared schema, so it always matches
 * what the backend will accept. Everything after submission is the engine's real output —
 * events streamed over SSE, and the durable execution record read back at the end.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  ArrowPathIcon,
  ClipboardDocumentIcon,
  PlayIcon,
  StopIcon,
} from '@heroicons/react/24/outline'
import {
  AnimatedNumber,
  Badge,
  Button,
  JsonView,
  Spinner,
  copyToClipboard,
  formatCurrency,
  formatDuration,
  formatNumber,
  titleCase,
  useToasts,
  type Agent,
} from '@finops/shared'
import { ApprovalGate } from './ApprovalGate'
import { EventFeed } from './EventFeed'
import { useAgentRun, type RunPhase } from '@/lib/useAgentRun'

const PHASE_LABEL: Record<RunPhase, string> = {
  idle: 'Ready',
  submitting: 'Submitting',
  running: 'Running',
  awaiting_approval: 'Awaiting approval',
  succeeded: 'Succeeded',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

const PHASE_TONE: Record<RunPhase, 'ok' | 'warn' | 'err' | 'info' | 'idle'> = {
  idle: 'idle',
  submitting: 'info',
  running: 'info',
  awaiting_approval: 'warn',
  succeeded: 'ok',
  failed: 'err',
  cancelled: 'warn',
}

const LONG_FIELDS = new Set(['query', 'question', 'message', 'request', 'notes', 'narrative'])

/** Ticking elapsed time while a run is in flight. */
function Elapsed({ since, frozen }: { since: number | null; frozen: number | null }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (since === null || frozen !== null) return undefined
    const timer = setInterval(() => setNow(Date.now()), 100)
    return () => clearInterval(timer)
  }, [since, frozen])
  if (since === null) return <span className="tabular-nums">—</span>
  return <span className="tabular-nums">{formatDuration(frozen ?? now - since)}</span>
}

export function RunConsole({ agent }: { agent: Agent }) {
  const run = useAgentRun(agent.key)
  const push = useToasts((state) => state.push)
  const feedRef = useRef<HTMLDivElement>(null)

  const fields = useMemo(() => Object.entries(agent.input_schema ?? {}), [agent.input_schema])
  const [values, setValues] = useState<Record<string, string>>({})

  // Reset the form to the agent's own example whenever the slot changes agent.
  useEffect(() => {
    const initial: Record<string, string> = {}
    for (const [field, value] of Object.entries(agent.example_input ?? {})) {
      initial[field] = typeof value === 'string' ? value : JSON.stringify(value)
    }
    setValues(initial)
    run.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.key])

  // Follow the feed as events arrive.
  useEffect(() => {
    const node = feedRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [run.events.length])

  useEffect(() => {
    if (run.phase === 'succeeded') push({ title: 'Run succeeded', description: agent.name, tone: 'ok' })
    if (run.phase === 'failed' && run.executionId) {
      push({ title: 'Run failed', description: run.execution?.error ?? agent.name, tone: 'err' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.phase])

  const missing = fields
    .filter(([field, definition]) => definition.required && !values[field]?.trim())
    .map(([field]) => field)

  const active = run.phase === 'running' || run.phase === 'submitting' || run.phase === 'awaiting_approval'
  const execution = run.execution
  const finalResponse = execution?.final_response ?? null
  const citations = (execution?.output?.citations as { title?: string; source?: string }[] | undefined) ?? []

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const input: Record<string, unknown> = {}
    for (const [field, definition] of fields) {
      const raw = values[field]
      if (raw === undefined || raw.trim() === '') continue
      input[field] = definition.type === 'number' ? Number(raw) : raw
    }
    void run.start(input)
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      {/* Input */}
      <div className="space-y-4">
        <form onSubmit={submit} className="card p-5">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold tracking-tight">Input</h3>
            <Badge tone={PHASE_TONE[run.phase]} dot={active}>
              {PHASE_LABEL[run.phase]}
            </Badge>
          </div>

          <div className="space-y-3.5">
            {fields.length === 0 ? (
              <p className="text-xs text-ink-muted">This agent takes no structured input.</p>
            ) : (
              fields.map(([field, definition]) => (
                <div key={field} className="space-y-1.5">
                  <label htmlFor={`${agent.key}-${field}`} className="metric-label">
                    {definition.label ?? titleCase(field)}
                    {definition.required ? <span className="ml-1 text-state-err">*</span> : null}
                  </label>
                  {LONG_FIELDS.has(field) ? (
                    <textarea
                      id={`${agent.key}-${field}`}
                      rows={4}
                      className="input resize-y"
                      value={values[field] ?? ''}
                      disabled={active}
                      onChange={(event) =>
                        setValues((current) => ({ ...current, [field]: event.target.value }))
                      }
                    />
                  ) : (
                    <input
                      id={`${agent.key}-${field}`}
                      type={
                        definition.type === 'password'
                          ? 'password'
                          : definition.type === 'number'
                            ? 'number'
                            : 'text'
                      }
                      className="input"
                      value={values[field] ?? ''}
                      disabled={active}
                      onChange={(event) =>
                        setValues((current) => ({ ...current, [field]: event.target.value }))
                      }
                    />
                  )}
                </div>
              ))
            )}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button type="submit" variant="primary" loading={run.phase === 'submitting'} disabled={active || missing.length > 0}>
              <PlayIcon className="h-4 w-4" />
              Run {agent.name}
            </Button>
            {active && run.executionId ? (
              <Button type="button" variant="outline" onClick={() => void run.cancel()}>
                <StopIcon className="h-4 w-4" />
                Cancel
              </Button>
            ) : null}
            {!active && run.executionId ? (
              <Button type="button" variant="ghost" onClick={run.reset}>
                <ArrowPathIcon className="h-4 w-4" />
                New run
              </Button>
            ) : null}
          </div>

          <p className="mt-3 text-2xs text-ink-subtle">
            Model <span className="font-mono text-ink-muted">{agent.model ?? 'router default'}</span> ·{' '}
            {agent.tools.length} tools
            {agent.requires_approval ? ' · a human approves before the final action' : ''}
          </p>
        </form>

        {/* Live counters */}
        <div className="card grid grid-cols-4 divide-x divide-line/60">
          {[
            { label: 'Elapsed', value: <Elapsed since={run.startedAt} frozen={execution?.latency_ms ?? null} /> },
            {
              label: 'Tools',
              value: (
                <AnimatedNumber
                  value={run.events.filter((e) => e.type === 'tool.completed' || e.type === 'tool.failed').length}
                  format={(v) => formatNumber(Math.round(v))}
                />
              ),
            },
            {
              label: 'Tokens',
              value: (
                <AnimatedNumber
                  value={execution?.tokens?.total ?? 0}
                  format={(v) => formatNumber(Math.round(v))}
                />
              ),
            },
            {
              label: 'Cost',
              value: <AnimatedNumber value={execution?.cost_usd ?? 0} format={(v) => formatCurrency(v, 4)} />,
            },
          ].map((metric) => (
            <div key={metric.label} className="px-3 py-3">
              <p className="metric-label">{metric.label}</p>
              <p className="mt-1 text-sm font-semibold tabular-nums text-ink">{metric.value}</p>
            </div>
          ))}
        </div>

        <AnimatePresence>
          {run.approval ? <ApprovalGate approval={run.approval} onDecide={run.decide} /> : null}
        </AnimatePresence>

        {run.error ? (
          <p className="rounded-xl border border-state-err/25 bg-state-err/10 px-3 py-2.5 text-xs text-state-err">
            {run.error}
          </p>
        ) : null}
      </div>

      {/* Live run */}
      <div className="space-y-4">
        <div className="card overflow-hidden">
          <div className="flex items-center justify-between gap-3 border-b border-line/60 px-4 py-3">
            <h3 className="text-sm font-semibold tracking-tight">Execution</h3>
            {run.executionId ? (
              <button
                type="button"
                onClick={() => {
                  void copyToClipboard(run.executionId!)
                  push({ title: 'Execution id copied', tone: 'info' })
                }}
                className="flex items-center gap-1.5 font-mono text-2xs text-ink-subtle transition-colors hover:text-ink"
              >
                <ClipboardDocumentIcon className="h-3.5 w-3.5" />
                {run.executionId.slice(0, 8)}
              </button>
            ) : null}
          </div>
          <div ref={feedRef} className="max-h-[26rem] overflow-y-auto">
            {run.executionId ? (
              <EventFeed events={run.events} />
            ) : (
              <p className="px-4 py-10 text-center text-xs text-ink-subtle">
                Submit the form to start a real execution. Every node, tool call and model
                response appears here as it happens.
              </p>
            )}
          </div>
        </div>

        <AnimatePresence>
          {finalResponse || execution?.error ? (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
              className="card p-5"
            >
              <div className="mb-2.5 flex items-center justify-between gap-3">
                <h3 className="text-sm font-semibold tracking-tight">
                  {execution?.error ? 'Failure' : 'Response'}
                </h3>
                {execution ? (
                  <span className="text-2xs text-ink-subtle">
                    {formatDuration(execution.latency_ms)} · {execution.tool_call_count} tools ·{' '}
                    {execution.llm_call_count} model calls
                  </span>
                ) : null}
              </div>

              {execution?.error ? (
                <p className="rounded-xl border border-state-err/25 bg-state-err/10 px-3 py-2.5 text-xs text-state-err">
                  <span className="font-medium">{execution.error_type}</span>: {execution.error}
                </p>
              ) : (
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{finalResponse}</p>
              )}

              {citations.length ? (
                <div className="mt-4">
                  <p className="metric-label mb-1.5">Citations</p>
                  <ul className="space-y-1">
                    {citations.map((citation, index) => (
                      <li key={index} className="text-2xs text-ink-muted">
                        <span className="font-medium text-ink">[{index + 1}]</span>{' '}
                        {citation.title ?? citation.source ?? 'source'}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {execution?.output && Object.keys(execution.output).length ? (
                <div className="mt-4">
                  <p className="metric-label mb-1.5">Structured output</p>
                  <JsonView data={execution.output} maxHeight="max-h-72" />
                </div>
              ) : null}
            </motion.div>
          ) : null}
        </AnimatePresence>

        {run.phase === 'running' && !finalResponse ? (
          <div className="flex items-center justify-center gap-2 py-2 text-2xs text-ink-subtle">
            <Spinner className="h-3.5 w-3.5" />
            Streaming from the engine…
          </div>
        ) : null}
      </div>
    </div>
  )
}
