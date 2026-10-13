import { describe, expect, it } from 'vitest'
import {
  formatBytes,
  formatCompact,
  formatCurrency,
  formatDuration,
  formatPercent,
  statusTone,
  titleCase,
} from './utils'

describe('formatters', () => {
  it('formats durations across magnitudes', () => {
    expect(formatDuration(0.4)).toBe('400µs')
    expect(formatDuration(42)).toBe('42ms')
    expect(formatDuration(1500)).toBe('1.50s')
    expect(formatDuration(125_000)).toBe('2m 5s')
    expect(formatDuration(null)).toBe('—')
  })

  it('formats currency with a small-value floor', () => {
    expect(formatCurrency(12.3456)).toBe('$12.35')
    expect(formatCurrency(0.0004)).toBe('<$0.01')
    expect(formatCurrency(null)).toBe('—')
  })

  it('compacts large numbers only', () => {
    expect(formatCompact(950)).toBe('950')
    expect(formatCompact(1_250_000)).toBe('1.3M')
  })

  it('formats percentages and bytes', () => {
    expect(formatPercent(93.456)).toBe('93.5%')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(0)).toBe('—')
  })

  it('title cases identifiers', () => {
    expect(titleCase('customer_service')).toBe('Customer Service')
    expect(titleCase('tool.completed')).toBe('Tool Completed')
  })
})

describe('statusTone', () => {
  it('maps execution states to semantic tones', () => {
    expect(statusTone('succeeded')).toBe('ok')
    expect(statusTone('running')).toBe('info')
    expect(statusTone('awaiting_approval')).toBe('warn')
    expect(statusTone('failed')).toBe('err')
    expect(statusTone('unknown-state')).toBe('idle')
  })
})
