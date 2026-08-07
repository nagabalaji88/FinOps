/**
 * The live event feed for a run.
 *
 * These are the engine's own events — the same records the trace and the event log are
 * built from — rendered as they arrive. Tool calls expand to show the arguments that were
 * sent and the result that came back.
 */
import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  ArrowPathIcon,
  BoltIcon,
  ChatBubbleLeftRightIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  CircleStackIcon,
  ExclamationTriangleIcon,
  HandRaisedIcon,
  MagnifyingGlassIcon,
  ShieldCheckIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline'
import { JsonView, cn, formatDuration, type ExecutionEvent } from '@finops/shared'

type Tone = 'ok' | 'warn' | 'err' | 'info' | 'idle'

interface Descriptor {
  icon: typeof BoltIcon
  tone: Tone
  title: (payload: Record<string, any>) => string
  detail?: (payload: Record<string, any>) => string | null
  expandable?: boolean
}

const DESCRIPTORS: Record<string, Descriptor> = {
  'execution.queued': { icon: BoltIcon, tone: 'idle', title: () => 'Queued' },
  'execution.started': { icon: BoltIcon, tone: 'info', title: () => 'Execution started' },
  'node.started': {
    icon: ChevronRightIcon,
    tone: 'idle',
    title: (p) => p.label ?? p.node ?? 'Node',
    detail: (p) => (p.kind ? String(p.kind) : null),
  },
  planning: {
    icon: ChatBubbleLeftRightIcon,
    tone: 'info',
    title: () => 'Plan produced',
    detail: (p) => p.plan?.objective ?? null,
    expandable: true,
  },
  'knowledge.search': {
    icon: MagnifyingGlassIcon,
    tone: 'info',
    title: (p) => `Retrieved ${p.results ?? 0} passages`,
    detail: (p) => `${p.backend ?? 'store'} · ${formatDuration(p.latency_ms)}`,
    expandable: true,
  },
  'memory.retrieval': {
    icon: CircleStackIcon,
    tone: 'idle',
    title: (p) => `Recalled ${p.messages ?? 0} prior turns`,
  },
  'llm.started': {
    icon: ChatBubbleLeftRightIcon,
    tone: 'idle',
    title: (p) => `Model call ${p.iteration ?? ''}`.trim(),
    detail: (p) => `${(p.tools_offered ?? []).length} tools offered`,
  },
  'llm.completed': {
    icon: ChatBubbleLeftRightIcon,
    tone: 'info',
    title: (p) => `${p.model ?? 'Model'} responded`,
    detail: (p) =>
      `${formatDuration(p.latency_ms)} · ${(p.tokens?.input ?? 0) + (p.tokens?.output ?? 0)} tokens · $${(
        p.cost_usd ?? 0
      ).toFixed(5)}`,
    expandable: true,
  },
  'tool.started': {
    icon: WrenchScrewdriverIcon,
    tone: 'idle',
    title: (p) => `${p.tool} …`,
    expandable: true,
  },
  'tool.completed': {
    icon: WrenchScrewdriverIcon,
    tone: 'ok',
    title: (p) => p.tool,
    detail: (p) => `${formatDuration(p.latency_ms)}${p.retries ? ` · ${p.retries} retries` : ''}`,
    expandable: true,
  },
  'tool.failed': {
    icon: ExclamationTriangleIcon,
    tone: 'err',
    title: (p) => `${p.tool} failed`,
    detail: (p) => (typeof p.result_preview === 'string' ? p.result_preview.slice(0, 160) : null),
    expandable: true,
  },
  retry: { icon: ArrowPathIcon, tone: 'warn', title: (p) => `Retry ${p.attempt ?? ''}`.trim() },
  validation: {
    icon: ShieldCheckIcon,
    tone: 'ok',
    title: (p) => `Validation ${(p.findings?.length ?? 0) - (p.failed ?? 0)}/${p.findings?.length ?? 0}`,
    expandable: true,
  },
  guardrail: {
    icon: ShieldCheckIcon,
    tone: 'warn',
    title: (p) => `Guardrails: ${p.findings?.length ?? 0} findings`,
    detail: (p) => (p.modified ? 'response was modified' : null),
    expandable: true,
  },
  'approval.requested': {
    icon: HandRaisedIcon,
    tone: 'warn',
    title: (p) => `Approval requested: ${p.title ?? ''}`,
    detail: (p) => (p.risk ? `${p.risk} risk` : null),
  },
  'execution.suspended': { icon: HandRaisedIcon, tone: 'warn', title: () => 'Suspended for review' },
  'final.response': { icon: CheckCircleIcon, tone: 'ok', title: () => 'Final response produced' },
  'execution.completed': { icon: CheckCircleIcon, tone: 'ok', title: () => 'Execution completed' },
  'execution.failed': {
    icon: ExclamationTriangleIcon,
    tone: 'err',
    title: (p) => `Execution failed: ${p.error ?? ''}`.trim(),
  },
  'execution.cancelled': { icon: ExclamationTriangleIcon, tone: 'warn', title: () => 'Cancelled' },
  'node.failed': {
    icon: ExclamationTriangleIcon,
    tone: 'err',
    title: (p) => `${p.node} failed`,
    detail: (p) => (p.error ? String(p.error).slice(0, 160) : null),
  },
}

/** Events that add nothing a human needs to read alongside the ones above. */
const HIDDEN = new Set(['node.completed', 'node.skipped', 'vector.search', 'llm.delta', 'cost'])

const TONE_TEXT: Record<Tone, string> = {
  ok: 'text-state-ok',
  warn: 'text-state-warn',
  err: 'text-state-err',
  info: 'text-state-info',
  idle: 'text-ink-subtle',
}

function Row({ event, index }: { event: ExecutionEvent; index: number }) {
  const [open, setOpen] = useState(false)
  const descriptor = DESCRIPTORS[event.type]
  if (!descriptor) return null
  const Icon = descriptor.icon
  const detail = descriptor.detail?.(event.payload ?? {})
  const expandable = descriptor.expandable && event.payload && Object.keys(event.payload).length > 0

  return (
    <motion.li
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1], delay: Math.min(index * 0.01, 0.2) }}
      className="border-b border-line/50 last:border-0"
    >
      <div
        className={cn('flex items-start gap-2.5 px-3 py-2', expandable && 'cursor-pointer hover:bg-accent-soft/40')}
        onClick={expandable ? () => setOpen((value) => !value) : undefined}
        role={expandable ? 'button' : undefined}
        aria-expanded={expandable ? open : undefined}
      >
        <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', TONE_TEXT[descriptor.tone])} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-xs text-ink">{descriptor.title(event.payload ?? {})}</p>
          {detail ? <p className="truncate text-2xs text-ink-subtle">{detail}</p> : null}
        </div>
        <time className="shrink-0 pt-0.5 font-mono text-2xs text-ink-subtle">
          {new Date(event.timestamp).toLocaleTimeString('en-GB', { hour12: false })}
        </time>
      </div>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden px-3 pb-2.5"
          >
            <JsonView data={event.payload} maxHeight="max-h-64" />
          </motion.div>
        ) : null}
      </AnimatePresence>
    </motion.li>
  )
}

export function EventFeed({ events }: { events: ExecutionEvent[] }) {
  const visible = events.filter((event) => !HIDDEN.has(event.type) && DESCRIPTORS[event.type])
  if (!visible.length) {
    return <p className="px-3 py-6 text-center text-2xs text-ink-subtle">Waiting for the first event…</p>
  }
  return (
    <ul className="divide-y divide-line/50">
      {visible.map((event, index) => (
        <Row key={`${event.sequence}-${event.type}`} event={event} index={index} />
      ))}
    </ul>
  )
}
