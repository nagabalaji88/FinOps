/** A ring gauge that draws itself on mount — used for share-of-book percentages. */
import { motion, useReducedMotion } from 'framer-motion'
import { AnimatedNumber } from '@finops/shared'

export function ShareGauge({
  label,
  value,
  caption,
  tone = 'info',
}: {
  label: string
  /** Percentage, 0–100. */
  value: number
  caption?: string
  tone?: 'ok' | 'warn' | 'err' | 'info'
}) {
  const reduceMotion = useReducedMotion()
  const radius = 34
  const circumference = 2 * Math.PI * radius
  const pct = Math.max(0, Math.min(100, value))
  const colour = {
    ok: 'rgb(var(--state-ok))',
    warn: 'rgb(var(--state-warn))',
    err: 'rgb(var(--state-err))',
    info: 'rgb(var(--state-info))',
  }[tone]

  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative">
        <svg viewBox="0 0 88 88" className="h-[88px] w-[88px] -rotate-90">
          <circle
            cx="44"
            cy="44"
            r={radius}
            fill="none"
            stroke="rgb(var(--accent-soft))"
            strokeWidth="8"
          />
          <motion.circle
            cx="44"
            cy="44"
            r={radius}
            fill="none"
            stroke={colour}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={circumference}
            initial={{ strokeDashoffset: reduceMotion ? circumference * (1 - pct / 100) : circumference }}
            animate={{ strokeDashoffset: circumference * (1 - pct / 100) }}
            transition={{ duration: 1.1, ease: [0.22, 1, 0.36, 1] }}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <AnimatedNumber
            value={pct}
            format={(next) => `${next.toFixed(1)}%`}
            className="text-sm font-semibold tabular-nums text-ink"
          />
        </div>
      </div>
      <div className="text-center">
        <p className="text-2xs font-medium text-ink">{label}</p>
        {caption ? <p className="text-2xs text-ink-subtle">{caption}</p> : null}
      </div>
    </div>
  )
}
