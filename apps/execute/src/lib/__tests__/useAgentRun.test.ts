import { describe, expect, it } from 'vitest'
import { nextPhase, type RunPhase } from '../useAgentRun'

/** The event sequence a gated run actually produces, start to finish. */
const GATED = [
  'execution.started',
  'node.started',
  'llm.completed',
  'tool.completed',
  'approval.requested',
  'execution.suspended',
  // ... reviewer decides, engine resumes from its checkpoint ...
  'node.started',
  'llm.completed',
  'final.response',
  'execution.completed',
]

function replay(events: string[], from: RunPhase = 'running'): RunPhase[] {
  const phases: RunPhase[] = []
  let phase = from
  for (const event of events) {
    phase = nextPhase(phase, event)
    phases.push(phase)
  }
  return phases
}

describe('nextPhase', () => {
  it('stays running through ordinary progress', () => {
    expect(replay(['node.started', 'llm.started', 'tool.completed', 'guardrail'])).toEqual([
      'running',
      'running',
      'running',
      'running',
    ])
  })

  it('enters the gate on suspension and leaves it when the engine resumes', () => {
    const phases = replay(GATED)
    expect(phases[5]).toBe('awaiting_approval')
    expect(phases[6]).toBe('running')
    expect(phases.at(-1)).toBe('succeeded')
  })

  it('never leaves the gate before the engine emits again', () => {
    expect(nextPhase('awaiting_approval', 'execution.suspended')).toBe('awaiting_approval')
  })

  it('maps each terminal event to its own phase', () => {
    expect(nextPhase('running', 'execution.completed')).toBe('succeeded')
    expect(nextPhase('running', 'execution.failed')).toBe('failed')
    expect(nextPhase('running', 'execution.cancelled')).toBe('cancelled')
  })

  it('can fail out of the gate — a rejected approval still ends the run', () => {
    expect(replay(['execution.suspended', 'execution.completed'])).toEqual([
      'awaiting_approval',
      'succeeded',
    ])
    expect(nextPhase('awaiting_approval', 'execution.failed')).toBe('failed')
  })
})
