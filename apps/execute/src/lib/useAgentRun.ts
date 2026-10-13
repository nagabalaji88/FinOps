/**
 * Drives one agent run from submission to terminal state.
 *
 * The engine suspends an execution when it hits a human-approval gate, and the SSE stream
 * closes at that point. So this hook tracks the last sequence number it saw and
 * re-subscribes from there once a decision has been recorded — the run continues in the
 * same panel, with the same event feed, rather than starting over.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  api,
  subscribeToExecution,
  type Approval,
  type Execution,
  type ExecutionEvent,
} from '@finops/shared'

export type RunPhase =
  | 'idle'
  | 'submitting'
  | 'running'
  | 'awaiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

const TERMINAL: Record<string, RunPhase> = {
  'execution.completed': 'succeeded',
  'execution.failed': 'failed',
  'execution.cancelled': 'cancelled',
}

/**
 * Where an event moves the run.
 *
 * `execution.suspended` is the approval gate; the next event after a decision means the
 * engine has resumed, so the phase leaves the gate on its own rather than waiting for the
 * decision call to report back.
 */
export function nextPhase(current: RunPhase, eventType: string): RunPhase {
  if (eventType === 'execution.suspended') return 'awaiting_approval'
  if (TERMINAL[eventType]) return TERMINAL[eventType]
  if (current === 'awaiting_approval') return 'running'
  return current
}

export interface RunState {
  phase: RunPhase
  executionId: string | null
  events: ExecutionEvent[]
  execution: Execution | null
  approval: Approval | null
  error: string | null
  startedAt: number | null
}

const EMPTY: RunState = {
  phase: 'idle',
  executionId: null,
  events: [],
  execution: null,
  approval: null,
  error: null,
  startedAt: null,
}

export function useAgentRun(agentKey: string) {
  const [state, setState] = useState<RunState>(EMPTY)
  const lastSequence = useRef(0)
  const [streamEpoch, setStreamEpoch] = useState(0)
  const executionId = state.executionId

  const reset = useCallback(() => {
    lastSequence.current = 0
    setState(EMPTY)
  }, [])

  const start = useCallback(
    async (input: Record<string, unknown>) => {
      lastSequence.current = 0
      setState({ ...EMPTY, phase: 'submitting' })
      try {
        const response = await api.post<{ execution_id: string; status: string }>(
          `/agents/${agentKey}/execute`,
          { input },
        )
        setState((current) => ({
          ...current,
          phase: 'running',
          executionId: response.execution_id,
          startedAt: Date.now(),
        }))
        setStreamEpoch((epoch) => epoch + 1)
      } catch (error) {
        setState({
          ...EMPTY,
          phase: 'failed',
          error: error instanceof ApiError ? error.message : String(error),
        })
      }
    },
    [agentKey],
  )

  /** Read the durable record once a run stops moving, so figures come from the database. */
  const refresh = useCallback(async (id: string) => {
    try {
      const execution = await api.get<Execution>(`/executions/${id}`)
      setState((current) => (current.executionId === id ? { ...current, execution } : current))
      return execution
    } catch {
      return null
    }
  }, [])

  /** Find the approval this execution is waiting on. */
  const loadApproval = useCallback(async (id: string) => {
    try {
      const response = await api.get<{ items: Approval[] }>('/approvals', { status: 'pending' })
      const approval = response.items.find((item) => item.execution_id === id) ?? null
      setState((current) => (current.executionId === id ? { ...current, approval } : current))
    } catch {
      /* the caller still sees the suspended state; the gate simply cannot be rendered */
    }
  }, [])

  useEffect(() => {
    if (!executionId || streamEpoch === 0) return undefined
    let cancelled = false

    const unsubscribe = subscribeToExecution(
      executionId,
      {
        onEvent: (event) => {
          if (cancelled) return
          if (event.sequence) lastSequence.current = Math.max(lastSequence.current, event.sequence)
          setState((current) => {
            if (current.executionId !== executionId) return current
            return {
              ...current,
              events: [...current.events, event],
              phase: nextPhase(current.phase, event.type),
            }
          })
          if (event.type === 'execution.suspended') void loadApproval(executionId)
          if (TERMINAL[event.type] || event.type === 'execution.suspended') {
            void refresh(executionId)
          }
        },
        onError: () => {
          if (!cancelled) void refresh(executionId)
        },
      },
      lastSequence.current,
    )

    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [executionId, streamEpoch, refresh, loadApproval])

  /** Record a decision on the open gate, then follow the resumed execution. */
  const decide = useCallback(
    async (decision: 'approve' | 'reject', comments: string) => {
      const approval = state.approval
      if (!approval) return
      await api.post(`/approvals/${approval.id}/decision`, { decision, comments })
      setState((current) => ({ ...current, approval: null, phase: 'running' }))
      setStreamEpoch((epoch) => epoch + 1)
    },
    [state.approval],
  )

  /** Poll a gate this user is not allowed to decide, and resume when someone else does. */
  useEffect(() => {
    if (state.phase !== 'awaiting_approval' || !state.approval || !executionId) return undefined
    const approvalId = state.approval.id
    const timer = setInterval(async () => {
      try {
        const approval = await api.get<Approval>(`/approvals/${approvalId}`)
        if (approval.status !== 'pending') {
          setState((current) => ({ ...current, approval: null, phase: 'running' }))
          setStreamEpoch((epoch) => epoch + 1)
        }
      } catch {
        /* keep waiting */
      }
    }, 4000)
    return () => clearInterval(timer)
  }, [state.phase, state.approval, executionId])

  const cancel = useCallback(async () => {
    if (!executionId) return
    try {
      await api.post(`/executions/${executionId}/cancel`, { reason: 'Cancelled from Execute' })
    } catch (error) {
      setState((current) => ({
        ...current,
        error: error instanceof ApiError ? error.message : String(error),
      }))
    }
  }, [executionId])

  return { ...state, start, reset, decide, cancel }
}
