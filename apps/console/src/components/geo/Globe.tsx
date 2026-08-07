/**
 * Orthographic globe.
 *
 * Markers are placed at their true latitude and longitude, corridors follow great-circle
 * paths, and the far hemisphere is clipped. The globe rotates on its own, can be dragged,
 * and flies to a marker when one is selected elsewhere on the page.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import {
  type LatLon,
  type Rotation,
  graticule,
  greatCircle,
  markerRadius,
  project,
  shortestDelta,
  toPath,
  visibleRuns,
} from '@/lib/geo'
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

export interface GlobeArc {
  id: string
  from: LatLon
  to: LatLon
  risk: RiskLevel
  weight: number
  label: string
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

export function Globe({
  markers,
  arcs,
  selectedId,
  onSelect,
  initialCentre,
  className,
}: {
  markers: GlobeMarker[]
  arcs: GlobeArc[]
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

  const graticulePaths = useMemo(() => {
    const lines = graticule(30)
    return lines
      .map((line) => visibleRuns(line.map((point) => ({ point, lift: 1 })), rotation, RADIUS, CENTRE))
      .flat()
      .map(toPath)
  }, [rotation])

  const arcPaths = useMemo(
    () =>
      arcs.map((arc) => ({
        arc,
        runs: visibleRuns(greatCircle(arc.from, arc.to), rotation, RADIUS, CENTRE).map(toPath),
      })),
    [arcs, rotation],
  )

  const placed = useMemo(
    () =>
      markers
        .map((marker) => ({ marker, position: project(marker, rotation, RADIUS, CENTRE) }))
        .filter((entry) => entry.position.visible)
        .sort((a, b) => a.position.depth - b.position.depth),
    [markers, rotation],
  )

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
          <radialGradient id="globe-face" cx="34%" cy="26%" r="82%">
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
          <clipPath id="globe-clip">
            <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS} />
          </clipPath>
        </defs>

        <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS + 34} fill="url(#globe-halo)" />

        <motion.g
          initial={{ opacity: 0, scale: 0.94 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
          style={{ transformOrigin: `${CENTRE.x}px ${CENTRE.y}px` }}
        >
          <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS} fill="url(#globe-face)" />
          <g clipPath="url(#globe-clip)" opacity={0.55}>
            {graticulePaths.map((path, index) => (
              <path
                key={index}
                d={path}
                fill="none"
                stroke="rgb(var(--line))"
                strokeWidth={0.8}
                vectorEffect="non-scaling-stroke"
              />
            ))}
          </g>
          <circle cx={CENTRE.x} cy={CENTRE.y} r={RADIUS} fill="url(#globe-limb)" />
          <circle
            cx={CENTRE.x}
            cy={CENTRE.y}
            r={RADIUS}
            fill="none"
            stroke="rgb(var(--line))"
            strokeWidth={1}
          />

          {/* Corridors */}
          <g fill="none" strokeLinecap="round">
            {arcPaths.map(({ arc, runs }) =>
              runs.map((path, index) => (
                <g key={`${arc.id}-${index}`}>
                  <path
                    d={path}
                    stroke={RISK_COLOR[arc.risk]}
                    strokeOpacity={active && active !== arc.id ? 0.12 : 0.3}
                    strokeWidth={0.8 + arc.weight * 1.8}
                  />
                  {!reduceMotion && (
                    <path
                      d={path}
                      stroke={RISK_COLOR[arc.risk]}
                      strokeOpacity={0.9}
                      strokeWidth={0.8 + arc.weight * 1.6}
                      strokeDasharray="6 92"
                      className="globe-arc-flow"
                      style={{ animationDelay: `${(arc.weight * 1.7) % 2.4}s` }}
                    />
                  )}
                </g>
              )),
            )}
          </g>

          {/* Markers */}
          {placed.map(({ marker, position }, index) => {
            const radius = markerRadius(marker.share, marker.kind === 'city' ? 2.8 : 4, marker.kind === 'city' ? 6 : 11)
            const isActive = marker.id === active
            const fade = 0.35 + 0.65 * Math.min(1, position.depth * 1.6)
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
                style={{ transformOrigin: `${position.x}px ${position.y}px`, cursor: 'pointer' }}
                onMouseEnter={() => setHovered(marker.id)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => onSelect(marker.id === selectedId ? null : marker.id)}
                role="button"
                aria-label={`${marker.name}: ${marker.primary}`}
              >
                {(isActive || marker.risk === 'high') && !reduceMotion && (
                  <circle
                    cx={position.x}
                    cy={position.y}
                    r={radius}
                    fill="none"
                    stroke={RISK_COLOR[marker.risk]}
                    strokeWidth={1.2}
                    className="globe-ping"
                  />
                )}
                <circle
                  cx={position.x}
                  cy={position.y}
                  r={radius + 4}
                  fill={RISK_COLOR[marker.risk]}
                  fillOpacity={isActive ? 0.22 : 0.12}
                />
                <circle
                  cx={position.x}
                  cy={position.y}
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
              left: `${(activeEntry.position.x / SIZE) * 100}%`,
              top: `${((activeEntry.position.y - 16) / SIZE) * 100}%`,
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
