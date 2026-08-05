/**
 * Orthographic globe maths.
 *
 * The geography screen draws a real sphere: every marker sits at its own latitude and
 * longitude, corridors follow great-circle paths, and anything on the far side of the
 * globe is clipped rather than drawn through it.
 */

export interface LatLon {
  latitude: number
  longitude: number
}

export interface Projected {
  x: number
  y: number
  /** Cosine of the angle from the view centre. Positive means the near hemisphere. */
  depth: number
  visible: boolean
}

export interface Rotation {
  /** Longitude at the centre of the disc, degrees. */
  lambda: number
  /** Latitude at the centre of the disc, degrees. */
  phi: number
}

const RAD = Math.PI / 180

export function project(
  point: LatLon,
  rotation: Rotation,
  radius: number,
  centre: { x: number; y: number },
): Projected {
  const phi = point.latitude * RAD
  const lambda = (point.longitude - rotation.lambda) * RAD
  const phi0 = rotation.phi * RAD

  const cosPhi = Math.cos(phi)
  const sinPhi = Math.sin(phi)
  const cosLambda = Math.cos(lambda)

  const x = cosPhi * Math.sin(lambda)
  const y = Math.cos(phi0) * sinPhi - Math.sin(phi0) * cosPhi * cosLambda
  const depth = Math.sin(phi0) * sinPhi + Math.cos(phi0) * cosPhi * cosLambda

  return {
    x: centre.x + radius * x,
    y: centre.y - radius * y,
    depth,
    visible: depth >= 0,
  }
}

/** Unit vector on the sphere, used for great-circle interpolation. */
function toVector(point: LatLon): [number, number, number] {
  const phi = point.latitude * RAD
  const lambda = point.longitude * RAD
  return [Math.cos(phi) * Math.cos(lambda), Math.cos(phi) * Math.sin(lambda), Math.sin(phi)]
}

function toLatLon(vector: [number, number, number]): LatLon {
  const [x, y, z] = vector
  return {
    latitude: Math.asin(z) / RAD,
    longitude: Math.atan2(y, x) / RAD,
  }
}

/** Angular distance between two points, in degrees. */
export function angularDistance(a: LatLon, b: LatLon): number {
  const [ax, ay, az] = toVector(a)
  const [bx, by, bz] = toVector(b)
  return Math.acos(Math.min(1, Math.max(-1, ax * bx + ay * by + az * bz))) / RAD
}

/**
 * Sample the great circle between two points (spherical linear interpolation), lifting the
 * midpoint off the surface so the arc reads as a flight path rather than a chord.
 */
export function greatCircle(a: LatLon, b: LatLon, samples = 48): { point: LatLon; lift: number }[] {
  const va = toVector(a)
  const vb = toVector(b)
  const dot = Math.min(1, Math.max(-1, va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]))
  const omega = Math.acos(dot)
  const points: { point: LatLon; lift: number }[] = []

  for (let i = 0; i <= samples; i += 1) {
    const t = i / samples
    let vector: [number, number, number]
    if (omega < 1e-6) {
      vector = va
    } else {
      const sa = Math.sin((1 - t) * omega) / Math.sin(omega)
      const sb = Math.sin(t * omega) / Math.sin(omega)
      vector = [
        va[0] * sa + vb[0] * sb,
        va[1] * sa + vb[1] * sb,
        va[2] * sa + vb[2] * sb,
      ]
    }
    // Parabolic lift, at most 12% of the radius and scaled by how far the arc travels.
    const lift = 1 + Math.sin(Math.PI * t) * 0.12 * (omega / Math.PI)
    points.push({ point: toLatLon(vector), lift })
  }
  return points
}

/**
 * Project a path and split it wherever it crosses behind the globe, so each returned run
 * can be drawn as its own polyline.
 */
export function visibleRuns(
  path: { point: LatLon; lift: number }[],
  rotation: Rotation,
  radius: number,
  centre: { x: number; y: number },
): { x: number; y: number }[][] {
  const runs: { x: number; y: number }[][] = []
  let current: { x: number; y: number }[] = []
  for (const { point, lift } of path) {
    // Visibility is judged on the surface; the lift fades out towards the limb so an arc
    // never spills outside the disc it belongs to.
    const surface = project(point, rotation, radius, centre)
    if (surface.visible) {
      // depth² rather than depth: the projected radius grows as 1/√(1−depth²) towards the
      // limb, and only a quadratic fade stays inside the disc for every lift we generate.
      const eased = 1 + (lift - 1) * surface.depth * surface.depth
      const projected = project(point, rotation, radius * eased, centre)
      current.push({ x: projected.x, y: projected.y })
    } else if (current.length) {
      runs.push(current)
      current = []
    }
  }
  if (current.length) runs.push(current)
  return runs.filter((run) => run.length > 1)
}

export function toPath(points: { x: number; y: number }[]): string {
  return points.map((p, index) => `${index === 0 ? 'M' : 'L'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ')
}

/** Meridians and parallels at the given spacing, as latitude/longitude paths. */
export function graticule(step = 30): LatLon[][] {
  const lines: LatLon[][] = []
  for (let lon = -180; lon < 180; lon += step) {
    const meridian: LatLon[] = []
    for (let lat = -90; lat <= 90; lat += 3) meridian.push({ latitude: lat, longitude: lon })
    lines.push(meridian)
  }
  for (let lat = -90 + step; lat < 90; lat += step) {
    const parallel: LatLon[] = []
    for (let lon = -180; lon <= 180; lon += 3) parallel.push({ latitude: lat, longitude: lon })
    lines.push(parallel)
  }
  return lines
}

/** Shortest signed rotation, in degrees, that brings `from` onto `to`. */
export function shortestDelta(from: number, to: number): number {
  let delta = (to - from) % 360
  if (delta > 180) delta -= 360
  if (delta < -180) delta += 360
  return delta
}

/** Marker radius scaled by share of value, clamped so small markets stay visible. */
export function markerRadius(share: number, min = 3.5, max = 11): number {
  const normalised = Math.sqrt(Math.max(0, Math.min(100, share)) / 100)
  return min + (max - min) * normalised
}
