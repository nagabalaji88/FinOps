import { describe, expect, it } from 'vitest'
import { parseSseFrames } from './api'

/** A frame exactly as the API emits it — sse-starlette uses CRLF. */
function frame(payload: Record<string, unknown>, id: number, eol = '\r\n'): string {
  return [`id: ${id}`, `event: ${payload.type}`, `data: ${JSON.stringify(payload)}`, '', ''].join(eol)
}

const started = { sequence: 1, type: 'execution.started', node: null, timestamp: 't', payload: {} }
const node = { sequence: 2, type: 'node.started', node: 'planner', timestamp: 't', payload: {} }

describe('parseSseFrames', () => {
  it('parses CRLF frames, which is what the server actually sends', () => {
    const { events, rest } = parseSseFrames(frame(started, 1) + frame(node, 2))
    expect(events.map((event) => event.type)).toEqual(['execution.started', 'node.started'])
    expect(rest).toBe('')
  })

  it('parses LF frames too', () => {
    const { events } = parseSseFrames(frame(started, 1, '\n') + frame(node, 2, '\n'))
    expect(events).toHaveLength(2)
  })

  it('holds back a frame split across two reads and completes it on the next', () => {
    const whole = frame(started, 1)
    const cut = Math.floor(whole.length / 2)

    const first = parseSseFrames(whole.slice(0, cut))
    expect(first.events).toEqual([])
    expect(first.rest).toBe(whole.slice(0, cut))

    const second = parseSseFrames(first.rest + whole.slice(cut))
    expect(second.events).toHaveLength(1)
    expect(second.events[0].sequence).toBe(1)
  })

  it('drops heartbeats and comment pings without dropping real events', () => {
    const buffer =
      ': ping\r\n\r\n' +
      'event: heartbeat\r\ndata: {"timestamp":"t"}\r\n\r\n' +
      'event: heartbeat\r\ndata: {"type":"heartbeat","timestamp":"t"}\r\n\r\n' +
      frame(node, 2)
    const { events } = parseSseFrames(buffer)
    expect(events).toHaveLength(1)
    expect(events[0].type).toBe('node.started')
  })

  it('survives a malformed frame and keeps parsing', () => {
    const { events } = parseSseFrames('data: {not json\r\n\r\n' + frame(started, 1))
    expect(events).toHaveLength(1)
  })

  it('returns nothing for an empty buffer', () => {
    expect(parseSseFrames('')).toEqual({ events: [], rest: '' })
  })
})
