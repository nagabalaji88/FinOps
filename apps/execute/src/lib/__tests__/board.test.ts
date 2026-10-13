import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { BOARD_SIZE, resolveBoard, useBoard } from '../board'

const AGENTS = [
  { key: 'customer_service' },
  { key: 'aml_investigation' },
  { key: 'kyc_onboarding' },
  { key: 'investment_research' },
  { key: 'knowledge_assistant' },
]

describe('resolveBoard', () => {
  it('shows exactly two agents', () => {
    expect(BOARD_SIZE).toBe(2)
    expect(resolveBoard(AGENTS, ['customer_service', 'aml_investigation'])).toHaveLength(2)
    expect(resolveBoard(AGENTS, [])).toHaveLength(2)
    expect(resolveBoard(AGENTS, ['a', 'b', 'c', 'd'])).toHaveLength(2)
  })

  it('honours the requested pair and its order', () => {
    const board = resolveBoard(AGENTS, ['knowledge_assistant', 'kyc_onboarding'])
    expect(board.map((agent) => agent.key)).toEqual(['knowledge_assistant', 'kyc_onboarding'])
  })

  it('fills a slot whose stored key no longer exists', () => {
    const board = resolveBoard(AGENTS, ['retired_agent', 'investment_research'])
    expect(board).toHaveLength(2)
    expect(board[1].key).toBe('investment_research')
    expect(board[0].key).not.toBe('investment_research')
  })

  it('never repeats an agent, even when both slots name the same one', () => {
    const board = resolveBoard(AGENTS, ['aml_investigation', 'aml_investigation'])
    expect(board).toHaveLength(2)
    expect(new Set(board.map((agent) => agent.key)).size).toBe(2)
  })

  it('degrades gracefully when the backend reports fewer agents than slots', () => {
    expect(resolveBoard([{ key: 'only_one' }], ['only_one', 'missing'])).toHaveLength(1)
    expect(resolveBoard([], ['a', 'b'])).toEqual([])
  })
})

describe('useBoard', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('starts from the deployment default', () => {
    const { result } = renderHook(() => useBoard())
    expect(result.current[0]).toEqual(['customer_service', 'aml_investigation'])
  })

  it('replaces one slot and remembers the choice', () => {
    const { result } = renderHook(() => useBoard())
    act(() => result.current[1](1, 'knowledge_assistant'))
    expect(result.current[0]).toEqual(['customer_service', 'knowledge_assistant'])

    const reloaded = renderHook(() => useBoard())
    expect(reloaded.result.current[0]).toEqual(['customer_service', 'knowledge_assistant'])
  })

  it('exchanges the slots instead of showing the same agent twice', () => {
    const { result } = renderHook(() => useBoard())
    act(() => result.current[1](0, 'aml_investigation'))
    expect(result.current[0]).toEqual(['aml_investigation', 'customer_service'])
  })

  it('ignores corrupt stored state', () => {
    localStorage.setItem('finops.execute.board', '{"not":"an array"}')
    const { result } = renderHook(() => useBoard())
    expect(result.current[0]).toHaveLength(BOARD_SIZE)
  })
})
