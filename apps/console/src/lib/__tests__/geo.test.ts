import { describe, expect, it } from 'vitest'
import {
  angularDistance,
  graticule,
  greatCircle,
  markerRadius,
  project,
  shortestDelta,
  visibleRuns,
} from '../geo'

const CENTRE = { x: 100, y: 100 }
const RADIUS = 100

describe('orthographic projection', () => {
  it('places the point under the view centre at the centre of the disc', () => {
    const projected = project({ latitude: 12, longitude: 40 }, { lambda: 40, phi: 12 }, RADIUS, CENTRE)
    expect(projected.x).toBeCloseTo(100, 6)
    expect(projected.y).toBeCloseTo(100, 6)
    expect(projected.depth).toBeCloseTo(1, 6)
    expect(projected.visible).toBe(true)
  })

  it('hides the far hemisphere', () => {
    const front = project({ latitude: 0, longitude: 0 }, { lambda: 0, phi: 0 }, RADIUS, CENTRE)
    const back = project({ latitude: 0, longitude: 180 }, { lambda: 0, phi: 0 }, RADIUS, CENTRE)
    expect(front.visible).toBe(true)
    expect(back.visible).toBe(false)
    expect(back.depth).toBeLessThan(0)
  })

  it('puts a point 90° east on the right limb, and north above the centre', () => {
    const east = project({ latitude: 0, longitude: 90 }, { lambda: 0, phi: 0 }, RADIUS, CENTRE)
    expect(east.x).toBeCloseTo(200, 6)
    expect(east.y).toBeCloseTo(100, 6)

    const north = project({ latitude: 45, longitude: 0 }, { lambda: 0, phi: 0 }, RADIUS, CENTRE)
    expect(north.y).toBeLessThan(100)
    expect(north.x).toBeCloseTo(100, 6)
  })

  it('never projects outside the disc', () => {
    for (let lat = -90; lat <= 90; lat += 15) {
      for (let lon = -180; lon <= 180; lon += 15) {
        const p = project({ latitude: lat, longitude: lon }, { lambda: 33, phi: -12 }, RADIUS, CENTRE)
        const distance = Math.hypot(p.x - CENTRE.x, p.y - CENTRE.y)
        expect(distance).toBeLessThanOrEqual(RADIUS + 1e-6)
      }
    }
  })
})

describe('great circles', () => {
  it('starts and ends on the requested endpoints', () => {
    const mumbai = { latitude: 19.076, longitude: 72.877 }
    const london = { latitude: 51.5, longitude: -0.13 }
    const path = greatCircle(mumbai, london, 24)
    expect(path).toHaveLength(25)
    expect(path[0].point.latitude).toBeCloseTo(mumbai.latitude, 4)
    expect(path[0].point.longitude).toBeCloseTo(mumbai.longitude, 4)
    expect(path[24].point.latitude).toBeCloseTo(london.latitude, 4)
    expect(path[24].point.longitude).toBeCloseTo(london.longitude, 4)
  })

  it('bows away from the surface in the middle and returns to it at the ends', () => {
    const path = greatCircle({ latitude: 0, longitude: 0 }, { latitude: 0, longitude: 90 }, 8)
    expect(path[0].lift).toBeCloseTo(1, 6)
    expect(path[8].lift).toBeCloseTo(1, 6)
    expect(path[4].lift).toBeGreaterThan(1)
  })

  it('measures angular distance', () => {
    expect(angularDistance({ latitude: 0, longitude: 0 }, { latitude: 0, longitude: 90 })).toBeCloseTo(90, 6)
    expect(angularDistance({ latitude: -90, longitude: 0 }, { latitude: 90, longitude: 0 })).toBeCloseTo(180, 4)
  })

  it('keeps a lifted arc inside the disc it belongs to', () => {
    // The lift fades to nothing at the limb, so no arc may spill past the globe's edge.
    const path = greatCircle({ latitude: 19.076, longitude: 72.877 }, { latitude: 39.5, longitude: -98.35 }, 64)
    for (const lambda of [0, 45, 72, 120, 200, 300]) {
      for (const run of visibleRuns(path, { lambda, phi: 16 }, RADIUS, CENTRE)) {
        for (const point of run) {
          expect(Math.hypot(point.x - CENTRE.x, point.y - CENTRE.y)).toBeLessThanOrEqual(RADIUS + 1e-6)
        }
      }
    }
  })

  it('splits a path that crosses behind the globe into separate runs', () => {
    // Equatorial ring sampled from the view centre outwards: visible, behind, visible again.
    const ring = []
    for (let lon = 0; lon <= 360; lon += 5) ring.push({ point: { latitude: 0, longitude: lon }, lift: 1 })
    const runs = visibleRuns(ring, { lambda: 0, phi: 0 }, RADIUS, CENTRE)
    expect(runs.length).toBe(2)
    expect(runs.every((run) => run.length > 1)).toBe(true)
  })
})

describe('graticule', () => {
  it('emits meridians and parallels at the requested spacing', () => {
    const lines = graticule(30)
    expect(lines).toHaveLength(12 + 5) // 12 meridians, parallels at ±60, ±30, 0
    expect(lines.every((line) => line.length > 1)).toBe(true)
  })
})

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
