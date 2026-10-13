/**
 * Globe helpers.
 *
 * The projection itself is d3-geo's: it clips filled landmasses along the limb correctly,
 * which the hand-rolled orthographic maths here never could. What remains is the small
 * amount that is ours — how a marker is sized, and which way the globe should turn.
 */

export interface LatLon {
  latitude: number
  longitude: number
}

export interface Rotation {
  /** Longitude at the centre of the disc, degrees. */
  lambda: number
  /** Latitude at the centre of the disc, degrees. */
  phi: number
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
