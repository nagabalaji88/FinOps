import { useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { ChevronRightIcon } from '@heroicons/react/24/outline'
import { type Span, type Trace, Badge, JsonView, cn, copyToClipboard, formatCurrency, formatDuration } from '@finops/shared'

const KIND_COLOR: Record<string, string> = {
  agent: 'bg-ink',
  planner: 'bg-[#7c83d8]',
  retriever: 'bg-[#4aa3c7]',
  memory: 'bg-[#5b9bd5]',
  llm: 'bg-[#34a382]',
  tool: 'bg-[#c98b2a]',
  validation: 'bg-[#8b7fd4]',
  guardrail: 'bg-[#b1558f]',
  approval: 'bg-[#d08a3e]',
  response: 'bg-[#3f8f6f]',
}

interface TreeSpan extends Span {
  depth: number
  children: TreeSpan[]
}

function buildTree(spans: Span[]): TreeSpan[] {
  const byId = new Map<string, TreeSpan>()
  for (const span of spans) byId.set(span.span_id, { ...span, depth: 0, children: [] })
  const roots: TreeSpan[] = []
  for (const span of byId.values()) {
    const parent = span.parent_span_id ? byId.get(span.parent_span_id) : undefined
    if (parent) {
      span.depth = parent.depth + 1
      parent.children.push(span)
    } else {
      roots.push(span)
    }
  }
  const flatten = (nodes: TreeSpan[]): TreeSpan[] =>
    nodes
      .sort((a, b) => new Date(a.start_time).getTime() - new Date(b.start_time).getTime())
      .flatMap((node) => [node, ...flatten(node.children)])
  return flatten(roots)
}

/** OpenTelemetry-style waterfall: every span is selectable and shows its full payload. */
export function TraceTimeline({ trace }: { trace: Trace }) {
  const [selected, setSelected] = useState<Span | null>(null)
  const rows = useMemo(() => buildTree(trace.spans), [trace.spans])

  return (
    <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
      <div className="card overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line/60 px-4 py-3">
          <div className="flex items-center gap-3 text-2xs text-ink-muted">
            <button
              onClick={() => copyToClipboard(trace.trace_id)}
              className="font-mono text-ink transition-colors hover:text-state-info"
              title="Copy trace id"
            >
              trace {trace.trace_id.slice(0, 16)}…
            </button>
            <span>{trace.span_count} spans</span>
            <span>{formatDuration(trace.total_duration_ms)}</span>
            <span>{formatCurrency(trace.total_cost_usd, 4)}</span>
          </div>
          {trace.error_count > 0 ? <Badge tone="err">{trace.error_count} errored</Badge> : <Badge tone="ok">healthy</Badge>}
        </div>

        <div className="max-h-[560px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 z-10 bg-surface-raised/95 backdrop-blur">
              <tr className="text-left text-2xs uppercase tracking-wider text-ink-subtle">
                <th className="px-4 py-2 font-medium">Span</th>
                <th className="w-24 px-2 py-2 font-medium">Duration</th>
                <th className="px-2 py-2 font-medium">Timeline</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((span) => (
                <tr
                  key={span.span_id}
                  onClick={() => setSelected(span)}
                  className={cn(
                    'table-row cursor-pointer',
                    selected?.span_id === span.span_id && 'bg-accent-soft/70',
                  )}
                >
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-2" style={{ paddingLeft: span.depth * 12 }}>
                      <span className={cn('h-2 w-2 shrink-0 rounded-sm', KIND_COLOR[span.kind] ?? 'bg-state-idle')} />
                      <span className="truncate font-mono text-2xs text-ink">{span.name}</span>
                      {span.status === 'error' ? <Badge tone="err">error</Badge> : null}
                      {span.retry_count > 0 ? <Badge tone="warn">{span.retry_count} retries</Badge> : null}
                    </div>
                  </td>
                  <td className="px-2 py-2 text-right tabular-nums text-ink-muted">
                    {formatDuration(span.duration_ms)}
                  </td>
                  <td className="px-2 py-2 pr-4">
                    <div className="relative h-3 w-full overflow-hidden rounded-sm bg-accent-soft/70">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${Math.max(span.width_pct, 0.6)}%` }}
                        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
                        className={cn(
                          'absolute top-0 h-full rounded-sm',
                          span.status === 'error' ? 'bg-state-err' : (KIND_COLOR[span.kind] ?? 'bg-state-idle'),
                        )}
                        style={{ left: `${span.offset_pct}%` }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card overflow-hidden">
        {!selected ? (
          <div className="flex h-full min-h-[240px] flex-col items-center justify-center gap-2 px-6 text-center">
            <ChevronRightIcon className="h-5 w-5 text-ink-subtle" />
            <p className="text-xs text-ink-muted">Select a span to inspect its attributes, payloads and errors.</p>
          </div>
        ) : (
          <div className="max-h-[560px] space-y-4 overflow-y-auto p-4">
            <div>
              <p className="font-mono text-xs text-ink">{selected.name}</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <Badge status={selected.status}>{selected.status}</Badge>
                <span className="chip">{selected.kind}</span>
                <span className="chip">{formatDuration(selected.duration_ms)}</span>
                {selected.cost_usd > 0 ? <span className="chip">{formatCurrency(selected.cost_usd, 5)}</span> : null}
              </div>
            </div>

            <dl className="grid grid-cols-2 gap-2 text-2xs">
              {[
                ['Span ID', selected.span_id],
                ['Parent', selected.parent_span_id ?? 'root'],
                ['Trace ID', selected.trace_id],
                ['Request ID', selected.request_id ?? '—'],
                ['Started', new Date(selected.start_time).toISOString()],
                ['Ended', selected.end_time ? new Date(selected.end_time).toISOString() : '—'],
                ['Tokens in', String(selected.tokens.input)],
                ['Tokens out', String(selected.tokens.output)],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="metric-label">{label}</dt>
                  <dd className="truncate font-mono text-ink-muted" title={value}>
                    {value}
                  </dd>
                </div>
              ))}
            </dl>

            {selected.error ? (
              <div className="rounded-xl border border-state-err/25 bg-state-err/10 p-3">
                <p className="metric-label text-state-err">Error</p>
                <p className="mt-1 font-mono text-2xs text-state-err">{selected.error}</p>
              </div>
            ) : null}

            {Object.keys(selected.attributes ?? {}).length > 0 ? (
              <div>
                <p className="metric-label mb-1.5">Attributes</p>
                <JsonView data={selected.attributes} maxHeight="max-h-48" />
              </div>
            ) : null}

            {selected.input ? (
              <div>
                <p className="metric-label mb-1.5">Input</p>
                <JsonView data={selected.input} maxHeight="max-h-56" />
              </div>
            ) : null}

            {selected.output ? (
              <div>
                <p className="metric-label mb-1.5">Output</p>
                <JsonView data={selected.output} maxHeight="max-h-56" />
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  )
}
