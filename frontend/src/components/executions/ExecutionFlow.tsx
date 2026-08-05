import { useCallback, useMemo, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from 'reactflow'
import 'reactflow/dist/style.css'
import type { ExecutionGraph, GraphNode } from '@/lib/api'
import { Badge, JsonView, Modal } from '@/components/ui'
import { cn, formatCurrency, formatDuration, formatNumber } from '@/lib/utils'

const STATE_STYLES: Record<GraphNode['state'], string> = {
  executed: 'border-state-ok/40 bg-state-ok/5',
  active: 'border-state-info/50 bg-state-info/10 animate-pulse-soft',
  error: 'border-state-err/50 bg-state-err/10',
  skipped: 'border-line/60 bg-surface-muted/40 opacity-60',
}

function GraphNodeCard({ data }: NodeProps<GraphNode & { onOpen: (node: GraphNode) => void }>) {
  return (
    <button
      onClick={() => data.onOpen(data)}
      className={cn(
        'w-52 rounded-xl border px-3 py-2.5 text-left shadow-glass backdrop-blur transition-all duration-200 hover:shadow-glass-lg',
        STATE_STYLES[data.state],
      )}
    >
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs font-semibold text-ink">{data.label}</span>
        <Badge
          tone={
            data.state === 'error' ? 'err' : data.state === 'executed' ? 'ok' : data.state === 'active' ? 'info' : 'idle'
          }
        >
          {data.state}
        </Badge>
      </div>
      <p className="mt-1 line-clamp-2 text-2xs leading-snug text-ink-muted">{data.description}</p>
      {data.span_count > 0 ? (
        <div className="mt-2 flex items-center gap-2 text-2xs tabular-nums text-ink-subtle">
          <span>{formatDuration(data.duration_ms)}</span>
          {data.tokens > 0 ? <span>· {formatNumber(data.tokens)} tok</span> : null}
          {data.cost_usd > 0 ? <span>· {formatCurrency(data.cost_usd, 4)}</span> : null}
          {data.retries > 0 ? <span className="text-state-warn">· {data.retries} retries</span> : null}
        </div>
      ) : null}
      <Handle type="source" position={Position.Right} />
    </button>
  )
}

const nodeTypes = { graphNode: GraphNodeCard }

const LAYOUT: Record<string, { x: number; y: number }> = {
  planner: { x: 0, y: 120 },
  retriever: { x: 260, y: 20 },
  memory: { x: 260, y: 220 },
  llm: { x: 530, y: 120 },
  tools: { x: 790, y: 120 },
  validation: { x: 1050, y: 120 },
  guardrails: { x: 1310, y: 120 },
  human_approval: { x: 1570, y: 120 },
  response: { x: 1830, y: 120 },
}

/** Interactive DAG of the execution, annotated with what actually ran. */
export function ExecutionFlow({ graph }: { graph: ExecutionGraph }) {
  const [selected, setSelected] = useState<GraphNode | null>(null)
  const onOpen = useCallback((node: GraphNode) => setSelected(node), [])

  const nodes: Node[] = useMemo(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        type: 'graphNode',
        position: LAYOUT[node.id] ?? { x: 0, y: 0 },
        data: { ...node, onOpen },
        draggable: true,
      })),
    [graph.nodes, onOpen],
  )

  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((edge, index) => {
        const source = graph.nodes.find((node) => node.id === edge.source)
        const target = graph.nodes.find((node) => node.id === edge.target)
        const live = source?.state !== 'skipped' && target?.state !== 'skipped'
        return {
          id: `${edge.source}-${edge.target}-${index}`,
          source: edge.source,
          target: edge.target,
          label: edge.label,
          animated: target?.state === 'active',
          style: { strokeWidth: live ? 1.6 : 1, opacity: live ? 1 : 0.35 },
          labelStyle: { fontSize: 10, fill: 'rgb(var(--ink-subtle))' },
          labelBgStyle: { fill: 'rgb(var(--surface-raised))' },
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
        }
      }),
    [graph.edges, graph.nodes],
  )

  return (
    <>
      <div className="card h-[440px] overflow-hidden">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.18 }}
          minZoom={0.3}
          maxZoom={1.6}
          proOptions={{ hideAttribution: true }}
          nodesConnectable={false}
          edgesFocusable={false}
        >
          <Background color="rgb(var(--line))" gap={22} size={1} />
          <Controls showInteractive={false} className="!bottom-3 !left-3" />
        </ReactFlow>
      </div>

      {selected ? (
        <Modal
          open
          onOpenChange={(open) => !open && setSelected(null)}
          title={selected.label}
          description={selected.description}
          size="lg"
        >
          <div className="space-y-4">
            <div className="flex flex-wrap gap-1.5">
              <Badge tone={selected.state === 'error' ? 'err' : selected.state === 'executed' ? 'ok' : 'idle'}>
                {selected.state}
              </Badge>
              <span className="chip">{selected.span_count} spans</span>
              <span className="chip">{formatDuration(selected.duration_ms)}</span>
              <span className="chip">{formatNumber(selected.tokens)} tokens</span>
              <span className="chip">{formatCurrency(selected.cost_usd, 5)}</span>
            </div>
            {selected.spans.length ? (
              <div className="overflow-hidden rounded-xl border border-line/70">
                <table className="w-full text-xs">
                  <thead className="bg-surface-muted/60 text-2xs uppercase tracking-wider text-ink-subtle">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Span</th>
                      <th className="px-3 py-2 text-left font-medium">Status</th>
                      <th className="px-3 py-2 text-right font-medium">Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.spans.map((span) => (
                      <tr key={span.span_id} className="table-row">
                        <td className="px-3 py-2 font-mono text-2xs">{span.name}</td>
                        <td className="px-3 py-2">
                          <Badge status={span.status}>{span.status}</Badge>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{formatDuration(span.duration_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-xs text-ink-muted">This node did not run in this execution.</p>
            )}
            <JsonView data={selected} maxHeight="max-h-64" />
          </div>
        </Modal>
      ) : null}
    </>
  )
}
