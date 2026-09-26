import { describe, expect, it } from 'vitest'
import { markerRadius, shortestDelta } from '../geo'

describe('helpers', () => {
  it('rotates the short way round', () => {
    expect(shortestDelta(350, 10)).toBeCloseTo(20, 6)
    expect(shortestDelta(10, 350)).toBeCloseTo(-20, 6)
    expect(shortestDelta(0, 180)).toBeCloseTo(180, 6)
  })

  it('scales markers by share without letting small markets vanish', () => {
    expect(markerRadius(0)).toBeCloseTo(3.5, 6)
    expect(markerRadius(100)).toBeCloseTo(11, 6)
    expect(markerRadius(0.4)).toBeGreaterThan(3.5)
    expect(markerRadius(0.4)).toBeLessThan(markerRadius(25))
  })
})
