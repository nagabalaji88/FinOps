/**
 * Orthographic globe.
 *
 * Real coastlines and national borders (Natural Earth 1:110m, via world-atlas) are drawn
 * through d3-geo, which clips the far hemisphere along the limb properly — a hand-rolled
 * clipper can split a polyline but cannot close a filled landmass that runs off the edge.
 * Markers sit at their true latitude and longitude. The globe spins on its own, can be
 * dragged, and flies to a marker when one is selected elsewhere on the page.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { geoDistance, geoGraticule10, geoOrthographic, geoPath } from 'd3-geo'
import { feature, mesh } from 'topojson-client'
import type { GeometryCollection, Topology } from 'topojson-specification'
import worldAtlas from 'world-atlas/countries-110m.json'
import { type LatLon, type Rotation, markerRadius, shortestDelta } from '@/lib/geo'
import { cn } from '@finops/shared'

export type RiskLevel = 'high' | 'elevated' | 'standard' | 'domestic'

export interface GlobeMarker extends LatLon {
  id: string
  name: string
  kind: 'country' | 'city'
  risk: RiskLevel
  share: number
  primary: string
  secondary: string
}

const RISK_COLOR: Record<RiskLevel, string> = {
  high: 'rgb(var(--state-err))',
  elevated: 'rgb(var(--state-warn))',
  standard: 'rgb(var(--state-info))',
  domestic: 'rgb(var(--state-ok))',
}

const SIZE = 520
const RADIUS = 208
const CENTRE = { x: SIZE / 2, y: SIZE / 2 }
const SPIN_DEGREES_PER_SECOND = 5.5

// Parsed once for the lifetime of the module, not per render: the topology is static.
// Land is every country as a single FeatureCollection, so the fill costs one path element
// rather than 177; borders are the interior mesh, so no boundary is drawn twice.
const topology = worldAtlas as unknown as Topology
const countries = topology.objects.countries as GeometryCollection
const LAND = feature(topology, countries)
const BORDERS = mesh(topology, countries, (a, b) => a !== b)
const GRATICULE = geoGraticule10()

export function Globe({
  markers,
  selectedId,
  onSelect,
  initialCentre,
  className,
}: {
  markers: GlobeMarker[]
  selectedId: string | null
  onSelect: (id: string | null) => void
  /** Where the globe faces on load — normally the bank's domestic market. */
  initialCentre?: LatLon
  className?: string
}) {
  const reduceMotion = useReducedMotion()
  const [rotation, setRotation] = useState<Rotation>({
    lambda: initialCentre?.longitude ?? 78,
    phi: 16,
  })
  const [hovered, setHovered] = useState<string | null>(null)
  const [spinning, setSpinning] = useState(true)
  const drag = useRef<{ x: number; y: number; lambda: number; phi: number } | null>(null)
  const target = useRef<Rotation | null>(null)
  const frame = useRef<number>(0)
  const last = useRef<number>(0)

  const selected = useMemo(
    () => markers.find((marker) => marker.id === selectedId) ?? null,
    [markers, selectedId],
  )

  // Fly to the selected marker rather than jumping.
  useEffect(() => {
    if (!selected) {
      target.current = null
      return
    }
    target.current = { lambda: selected.longitude, phi: Math.max(-60, Math.min(60, selected.latitude)) }
    if (reduceMotion) {
      setRotation({ lambda: selected.longitude, phi: selected.latitude })
      target.current = null
    }
  }, [selected, reduceMotion])

  useEffect(() => {
    if (reduceMotion) return undefined
    const tick = (now: number) => {
      const elapsed = last.current ? Math.min(now - last.current, 64) : 16
      last.current = now
      setRotation((current) => {
        if (target.current) {
          const dLambda = shortestDelta(current.lambda, target.current.lambda)
          const dPhi = target.current.phi - current.phi
          if (Math.abs(dLambda) < 0.15 && Math.abs(dPhi) < 0.15) {
            target.current = null
            return current
          }
          const ease = 1 - Math.exp(-elapsed / 180)
          return { lambda: current.lambda + dLambda * ease, phi: current.phi + dPhi * ease }
        }
        if (!spinning || drag.current) return current
        return { ...current, lambda: (current.lambda + (SPIN_DEGREES_PER_SECOND * elapsed) / 1000) % 360 }
      })
      frame.current = requestAnimationFrame(tick)
    }
    frame.current = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(frame.current)
      last.current = 0
    }
  }, [reduceMotion, spinning])

  const onPointerDown = useCallback(
    (event: React.PointerEvent<SVGSVGElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId)
      drag.current = { x: event.clientX, y: event.clientY, lambda: rotation.lambda, phi: rotation.phi }
      target.current = null
    },
    [rotation],
  )

  const onPointerMove = useCallback((event: React.PointerEvent<SVGSVGElement>) => {
    if (!drag.current) return
    const dx = event.clientX - drag.current.x
    const dy = event.clientY - drag.current.y
    setRotation({
      lambda: drag.current.lambda + dx * 0.35,
      phi: Math.max(-80, Math.min(80, drag.current.phi + dy * 0.3)),
    })
  }, [])

  const endDrag = useCallback(() => {
    drag.current = null
  }, [])

  // d3 rotates the sphere, so the angles are negated: [-lambda, -phi] brings the requested
  // longitude and latitude to the centre of the disc. clipAngle(90) drops the far side.
  const geography = useMemo(() => {
    const projection = geoOrthographic()
      .scale(RADIUS)
      .translate([CENTRE.x, CENTRE.y])
      .rotate([-rotation.lambda, -rotation.phi])
      .clipAngle(90)
    const path = geoPath(projection)
    return {
      projection,
      land: path(LAND) ?? '',
      borders: path(BORDERS) ?? '',
      graticule: path(GRATICULE) ?? '',
    }
  }, [rotation])

  const placed = useMemo(() => {
    const { projection } = geography
    // clipAngle only clips what geoPath draws: calling the projection directly still returns
    // a coordinate for the far hemisphere, and it lands inside the disc. Visibility has to be
    // judged on the angle from the view centre, which is the point .rotate() negates *to* —
    // [lambda, phi], not the negated pair handed to .rotate().
    const centre: [number, number] = [rotation.lambda, rotation.phi]
    return markers
      .map((marker) => {
        const point = projection([marker.longitude, marker.latitude])
        const angle = geoDistance([marker.longitude, marker.latitude], centre)
        return { marker, point, depth: Math.cos(angle) }
      })
      .filter(
        (entry): entry is { marker: GlobeMarker; point: [number, number]; depth: number } =>
          entry.point !== null && entry.depth >= 0,
      )
      .sort((a, b) => a.depth - b.depth)
  }, [markers, geography, rotation])

  const active = hovered ?? selectedId
  const activeEntry = placed.find((entry) => entry.marker.id === active) ?? null

  return (
    <div className={cn('relative select-none', className)}>
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className="h-full w-full cursor-grab touch-none active:cursor-grabbing"
        role="img"
        aria-label="Globe showing transaction activity by location"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
        onPointerCancel={endDrag}
        onMouseEnter={() => setSpinning(false)}
        onMouseLeave={() => {
          setSpinning(true)
          setHovered(null)
        }}
      >
        <defs>
          <radialGradient id="globe-ocean" cx="34%" cy="26%" r="82%">
            <stop offset="0%" stopColor="rgb(var(--surface-raised))" stopOpacity="1" />
            <stop offset="58%" stopColor="rgb(var(--surface-muted))" stopOpacity="0.96" />
            <stop offset="100%" stopColor="rgb(var(--accent-soft))" stopOpacity="0.92" />
          </radialGradient>
          <radialGradient id="globe-limb" cx="50%" cy="50%" r="50%">
            <stop offset="82%" stopColor="rgb(var(--ink))" stopOpacity="0" />
            <stop offset="100%" stopColor="rgb(var(--ink))" stopOpacity="0.14" />
          </radialGradient>
          <radialGradient id="globe-halo" cx="50%" cy="50%" r="50%">
            <stop offset="72%" stopColor="rgb(var(--ink))" stopOpacity="0" />
            <stop offset="92%" stopColor="rgb(var(--ink))" stopOpacity="0.05" />
            <stop offset="100%" stopColor="rgb(var(--ink))" stopOpacity="0" />
          </radialGradient>
        </defs>

        <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS + 34} fill="url(#globe-halo)" />

        <motion.g
          initial={{ opacity: 0, scale: 0.94 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
          style={{ transformOrigin: `${CENTRE.x}px ${CENTRE.y}px` }}
        >
          {/* Ocean */}
          <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS} fill="url(#globe-ocean)" />

          {/* Graticule, under the land so it reads as sea markings */}
          <path
            d={geography.graticule}
            fill="none"
            stroke="rgb(var(--line))"
            strokeWidth={0.5}
            opacity={0.4}
            vectorEffect="non-scaling-stroke"
          />

          {/* Land and national borders */}
          <path d={geography.land} fill="rgb(var(--accent-soft))" fillOpacity={0.85} stroke="none" />
          <path
            d={geography.borders}
            fill="none"
            stroke="rgb(var(--line))"
            strokeWidth={0.6}
            strokeOpacity={0.9}
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
          <path
            d={geography.land}
            fill="none"
            stroke="rgb(var(--ink-subtle))"
            strokeWidth={0.7}
            strokeOpacity={0.55}
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />

          {/* Shading towards the limb, then the outline of the disc */}
          <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS} fill="url(#globe-limb)" />
          <circle
            cx={CENTRE.x}
            cy={CENTRE.y}
            r={RADIUS}
            fill="none"
            stroke="rgb(var(--line))"
            strokeWidth={1}
          />

          {/* Markers */}
          {placed.map(({ marker, point, depth }, index) => {
            const [x, y] = point
            const radius = markerRadius(
              marker.share,
              marker.kind === 'city' ? 2.8 : 4,
              marker.kind === 'city' ? 6 : 11,
            )
            const isActive = marker.id === active
            const fade = 0.35 + 0.65 * Math.min(1, depth * 1.6)
            return (
              <motion.g
                key={marker.id}
                initial={{ opacity: 0, scale: 0 }}
                animate={{ opacity: fade, scale: 1 }}
                transition={{
                  delay: reduceMotion ? 0 : Math.min(index * 0.045, 0.7),
                  duration: 0.45,
                  ease: [0.34, 1.56, 0.64, 1],
                }}
                style={{ transformOrigin: `${x}px ${y}px`, cursor: 'pointer' }}
                onMouseEnter={() => setHovered(marker.id)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => onSelect(marker.id === selectedId ? null : marker.id)}
                role="button"
                aria-label={`${marker.name}: ${marker.primary}`}
              >
                {(isActive || marker.risk === 'high') && !reduceMotion && (
                  <circle
                    cx={x}
                    cy={y}
                    r={radius}
                    fill="none"
                    stroke={RISK_COLOR[marker.risk]}
                    strokeWidth={1.2}
                    className="globe-ping"
                  />
                )}
                <circle
                  cx={x}
                  cy={y}
                  r={radius + 4}
                  fill={RISK_COLOR[marker.risk]}
                  fillOpacity={isActive ? 0.22 : 0.12}
                />
                <circle
                  cx={x}
                  cy={y}
                  r={radius}
                  fill={RISK_COLOR[marker.risk]}
                  stroke="rgb(var(--surface-raised))"
                  strokeWidth={marker.kind === 'city' ? 1 : 1.5}
                />
              </motion.g>
            )
          })}
        </motion.g>
      </svg>

      {/* Callout for the hovered or selected marker */}
      <AnimatePresence>
        {activeEntry ? (
          <motion.div
            key={activeEntry.marker.id}
            initial={{ opacity: 0, y: 6, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            className="pointer-events-none absolute z-10 min-w-[9rem] -translate-x-1/2 -translate-y-full rounded-xl border border-line/70 bg-surface-raised/95 px-3 py-2 shadow-glass-lg backdrop-blur-xl"
            style={{
              left: `${(activeEntry.point[0] / SIZE) * 100}%`,
              top: `${((activeEntry.point[1] - 16) / SIZE) * 100}%`,
            }}
          >
            <p className="text-xs font-semibold tracking-tight text-ink">{activeEntry.marker.name}</p>
            <p className="mt-0.5 text-2xs tabular-nums text-ink-muted">{activeEntry.marker.primary}</p>
            <p className="text-2xs text-ink-subtle">{activeEntry.marker.secondary}</p>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  )
}
