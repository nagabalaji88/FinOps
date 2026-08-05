import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Approval } from '@/lib/api'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  JsonView,
  Modal,
  PageHeader,
  SkeletonCard,
  Tabs,
  TabPanel,
} from '@/components/ui'
import { useAuth, useToasts } from '@/store'
import { formatDateTime, relativeTime, titleCase } from '@/lib/utils'

export default function Approvals() {
  const queryClient = useQueryClient()
  const push = useToasts((state) => state.push)
  const { can } = useAuth()
  const [tab, setTab] = useState('pending')
  const [selected, setSelected] = useState<Approval | null>(null)
  const [comments, setComments] = useState('')

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['approvals', tab],
    queryFn: () => api.get<{ counts: Record<string, number>; items: Approval[] }>('/approvals', { status: tab }),
    refetchInterval: 10_000,
  })

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'approve' | 'reject' }) =>
      api.post(`/approvals/${id}/decision`, { decision, comments: comments || undefined }),
    onSuccess: (_result, variables) => {
      push({ title: `Request ${variables.decision}d`, tone: variables.decision === 'approve' ? 'ok' : 'warn' })
      setSelected(null)
      setComments('')
      void queryClient.invalidateQueries({ queryKey: ['approvals'] })
    },
    onError: (mutationError) =>
      push({ title: 'Decision failed', description: (mutationError as Error).message, tone: 'err' }),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div className="space-y-5">
      <PageHeader
        title="Human approvals"
        description="Executions suspended awaiting a reviewer decision, with the full decision timeline."
      />

      <Tabs
        value={tab}
        onValueChange={setTab}
        tabs={[
          { value: 'pending', label: 'Pending', count: data?.counts?.pending },
          { value: 'approved', label: 'Approved', count: data?.counts?.approved },
          { value: 'rejected', label: 'Rejected', count: data?.counts?.rejected },
          { value: 'all', label: 'All' },
        ]}
      >
        <TabPanel value={tab}>
          {isLoading ? (
            <div className="grid gap-4 lg:grid-cols-2">
              {Array.from({ length: 4 }).map((_, index) => (
                <SkeletonCard key={index} rows={3} />
              ))}
            </div>
          ) : data?.items.length === 0 ? (
            <Card>
              <EmptyState
                title="Nothing to review"
                description="Approval requests appear here the moment an agent suspends for a human decision."
              />
            </Card>
          ) : (
            <div className="grid gap-4 lg:grid-cols-2">
              {data?.items.map((approval) => (
                <Card key={approval.id} interactive className="flex flex-col">
                  <div className="flex items-start justify-between gap-3 px-5 pt-5">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold">{approval.title}</h3>
                      <p className="mt-1 text-2xs text-ink-muted">
                        {titleCase(approval.agent_key)} · requested {relativeTime(approval.created_at)} by{' '}
                        {approval.requested_by ?? 'system'}
                      </p>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1.5">
                      <Badge
                        tone={
                          approval.risk_level === 'critical' || approval.risk_level === 'high'
                            ? 'err'
                            : approval.risk_level === 'medium'
                              ? 'warn'
                              : 'idle'
                        }
                      >
                        {approval.risk_level} risk
                      </Badge>
                      <Badge status={approval.status}>{approval.status}</Badge>
                    </div>
                  </div>

                  <p className="mt-3 line-clamp-3 px-5 text-xs leading-relaxed text-ink-muted">{approval.summary}</p>

                  {approval.payload?.tool ? (
                    <div className="mx-5 mt-3 rounded-xl border border-line/70 bg-surface-muted/50 px-3 py-2">
                      <p className="metric-label">Tool call</p>
                      <p className="mt-0.5 font-mono text-2xs text-ink">{approval.payload.tool}</p>
                    </div>
                  ) : null}

                  <div className="mt-auto flex items-center gap-2 px-5 py-4">
                    <Link
                      to={`/executions/${approval.execution_id}`}
                      className="text-2xs text-state-info hover:underline"
                    >
                      View execution →
                    </Link>
                    <div className="ml-auto flex gap-2">
                      <Button size="sm" onClick={() => setSelected(approval)}>
                        Details
                      </Button>
                      {approval.status === 'pending' && can('approval:decide') ? (
                        <>
                          <Button
                            size="sm"
                            variant="danger"
                            onClick={() => decide.mutate({ id: approval.id, decision: 'reject' })}
                          >
                            Reject
                          </Button>
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => decide.mutate({ id: approval.id, decision: 'approve' })}
                          >
                            Approve
                          </Button>
                        </>
                      ) : null}
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </TabPanel>
      </Tabs>

      {selected ? (
        <Modal open onOpenChange={(open) => !open && setSelected(null)} title={selected.title} size="lg">
          <div className="space-y-4">
            <p className="whitespace-pre-wrap text-sm text-ink-muted">{selected.summary}</p>

            <div>
              <p className="metric-label mb-1.5">Payload under review</p>
              <JsonView data={selected.payload} />
            </div>

            <div>
              <p className="metric-label mb-1.5">Decision timeline</p>
              <ol className="space-y-2">
                {selected.timeline.map((entry, index) => (
                  <li key={index} className="flex gap-3 text-2xs">
                    <span className="w-32 shrink-0 text-ink-subtle">{formatDateTime(entry.at)}</span>
                    <span className="font-medium text-ink">{entry.event}</span>
                    <span className="text-ink-muted">{entry.by}</span>
                    {entry.comments ? <span className="text-ink-muted">— {entry.comments}</span> : null}
                  </li>
                ))}
              </ol>
            </div>

            {selected.status === 'pending' && can('approval:decide') ? (
              <div className="space-y-2 border-t border-line/60 pt-4">
                <label htmlFor="comments" className="metric-label">
                  Reviewer comments
                </label>
                <textarea
                  id="comments"
                  rows={3}
                  className="input"
                  value={comments}
                  onChange={(event) => setComments(event.target.value)}
                  placeholder="Record the rationale for this decision (stored in the audit trail)"
                />
                <div className="flex justify-end gap-2">
                  <Button
                    variant="danger"
                    loading={decide.isPending}
                    onClick={() => decide.mutate({ id: selected.id, decision: 'reject' })}
                  >
                    Reject
                  </Button>
                  <Button
                    variant="primary"
                    loading={decide.isPending}
                    onClick={() => decide.mutate({ id: selected.id, decision: 'approve' })}
                  >
                    Approve
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
        </Modal>
      ) : null}
    </div>
  )
}
