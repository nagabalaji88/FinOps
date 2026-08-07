/**
 * The human-in-the-loop gate.
 *
 * When the engine suspends a run, the operator either decides here and the execution
 * resumes from its checkpoint, or — where segregation of duties forbids it — sees exactly
 * who has to decide instead, while the panel waits for that decision.
 */
import { useState } from 'react'
import { motion } from 'framer-motion'
import { HandRaisedIcon } from '@heroicons/react/24/outline'
import { Badge, Button, JsonView, Spinner, useAuth, type Approval } from '@finops/shared'

const RISK_TONE = { low: 'idle', medium: 'info', high: 'warn', critical: 'err' } as const

export function ApprovalGate({
  approval,
  onDecide,
}: {
  approval: Approval
  onDecide: (decision: 'approve' | 'reject', comments: string) => Promise<void>
}) {
  const { user, can } = useAuth()
  const [comments, setComments] = useState('')
  const [pending, setPending] = useState<'approve' | 'reject' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const isAdmin = user?.roles.includes('admin') ?? false
  const hasRole = isAdmin || (user?.roles.includes(approval.required_role) ?? false)
  // The API enforces this too; the UI states it up front rather than failing on submit.
  const ownRequest = !isAdmin && approval.requested_by === user?.email
  const canDecide = can('approval:decide') && hasRole && !ownRequest

  const submit = async (decision: 'approve' | 'reject') => {
    setPending(decision)
    setError(null)
    try {
      await onDecide(decision, comments)
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError))
      setPending(null)
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
      className="rounded-2xl border border-state-warn/30 bg-state-warn/[0.06] p-4"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-state-warn/15">
          <HandRaisedIcon className="h-4 w-4 text-state-warn" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold tracking-tight text-ink">{approval.title}</h4>
            <Badge tone={RISK_TONE[approval.risk_level]}>{approval.risk_level} risk</Badge>
            <Badge tone="idle">{approval.node}</Badge>
          </div>
          <p className="mt-1 text-xs text-ink-muted">{approval.summary}</p>

          {approval.payload && Object.keys(approval.payload).length ? (
            <div className="mt-3">
              <p className="metric-label mb-1.5">What is being approved</p>
              <JsonView data={approval.payload} maxHeight="max-h-56" />
            </div>
          ) : null}

          {canDecide ? (
            <div className="mt-3 space-y-2.5">
              <label htmlFor={`comments-${approval.id}`} className="metric-label">
                Reviewer comment
              </label>
              <textarea
                id={`comments-${approval.id}`}
                rows={2}
                className="input resize-y"
                placeholder="Recorded on the approval timeline and in the audit trail."
                value={comments}
                onChange={(event) => setComments(event.target.value)}
              />
              {error ? (
                <p className="rounded-lg border border-state-err/25 bg-state-err/10 px-3 py-2 text-2xs text-state-err">
                  {error}
                </p>
              ) : null}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="primary"
                  loading={pending === 'approve'}
                  disabled={pending !== null}
                  onClick={() => void submit('approve')}
                >
                  Approve and resume
                </Button>
                <Button
                  variant="outline"
                  loading={pending === 'reject'}
                  disabled={pending !== null}
                  onClick={() => void submit('reject')}
                >
                  Reject
                </Button>
              </div>
            </div>
          ) : (
            <div className="mt-3 flex items-center gap-2.5 rounded-xl border border-line/70 bg-surface-raised/60 px-3 py-2.5">
              <Spinner className="h-3.5 w-3.5 text-ink-subtle" />
              <p className="text-2xs text-ink-muted">
                {ownRequest
                  ? `Segregation of duties: you started this run, so it needs a different reviewer holding the ${approval.required_role} role.`
                  : `Waiting on a reviewer with the ${approval.required_role} role. This panel resumes the moment a decision is recorded.`}
              </p>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  )
}
