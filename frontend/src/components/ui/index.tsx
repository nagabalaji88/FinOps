/** Minimal glass design-system primitives shared across the console. */
import { motion, useReducedMotion, useSpring, useTransform, type HTMLMotionProps } from 'framer-motion'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import * as TabsPrimitive from '@radix-ui/react-tabs'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { XMarkIcon } from '@heroicons/react/24/outline'
import { type ReactNode, forwardRef, useEffect } from 'react'
import { cn, statusTone } from '@/lib/utils'

/* ------------------------------------------------------------------ Card */
export function Card({
  className,
  children,
  interactive,
  ...props
}: HTMLMotionProps<'div'> & { interactive?: boolean }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      className={cn('card', interactive && 'card-hover', className)}
      {...props}
    >
      {children}
    </motion.div>
  )
}

export function CardHeader({
  title,
  subtitle,
  action,
  className,
}: {
  title: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex items-start justify-between gap-4 px-5 pt-5', className)}>
      <div className="min-w-0">
        <h3 className="text-sm font-semibold tracking-tight text-ink">{title}</h3>
        {subtitle ? <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}

/* ------------------------------------------------------------------ Stat */
export function Stat({
  label,
  value,
  hint,
  tone = 'idle',
  icon,
  className,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'ok' | 'warn' | 'err' | 'info' | 'idle'
  icon?: ReactNode
  className?: string
}) {
  const toneColor = {
    ok: 'text-state-ok',
    warn: 'text-state-warn',
    err: 'text-state-err',
    info: 'text-state-info',
    idle: 'text-ink',
  }[tone]
  return (
    <div className={cn('flex flex-col gap-2 px-5 py-4', className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="metric-label">{label}</span>
        {icon ? <span className="text-ink-subtle">{icon}</span> : null}
      </div>
      <span className={cn('metric-value', toneColor)}>{value}</span>
      {hint ? <span className="text-xs text-ink-muted">{hint}</span> : null}
    </div>
  )
}

/* ---------------------------------------------------------------- Badge */
export function Badge({
  children,
  tone,
  status,
  className,
  dot,
}: {
  children: ReactNode
  tone?: 'ok' | 'warn' | 'err' | 'info' | 'idle'
  status?: string
  className?: string
  dot?: boolean
}) {
  const resolved = tone ?? (status ? statusTone(status) : 'idle')
  const styles = {
    ok: 'text-state-ok border-state-ok/25 bg-state-ok/10',
    warn: 'text-state-warn border-state-warn/25 bg-state-warn/10',
    err: 'text-state-err border-state-err/25 bg-state-err/10',
    info: 'text-state-info border-state-info/25 bg-state-info/10',
    idle: 'text-ink-muted border-line bg-accent-soft/60',
  }[resolved]
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-2xs font-medium whitespace-nowrap',
        styles,
        className,
      )}
    >
      {dot ? <span className="h-1.5 w-1.5 rounded-full bg-current" /> : null}
      {children}
    </span>
  )
}

export function StatusDot({ status, pulse }: { status: string; pulse?: boolean }) {
  const tone = statusTone(status)
  const color = {
    ok: 'bg-state-ok',
    warn: 'bg-state-warn',
    err: 'bg-state-err',
    info: 'bg-state-info',
    idle: 'bg-state-idle',
  }[tone]
  return (
    <span className="relative inline-flex h-2 w-2" aria-label={status} role="img">
      {pulse ? (
        <span className={cn('absolute inline-flex h-full w-full animate-ping rounded-full opacity-60', color)} />
      ) : null}
      <span className={cn('relative inline-flex h-2 w-2 rounded-full', color)} />
    </span>
  )
}

/* --------------------------------------------------------------- Button */
export const Button = forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: 'primary' | 'ghost' | 'outline' | 'danger'
    size?: 'sm' | 'md'
    loading?: boolean
  }
>(function Button({ variant = 'outline', size = 'md', loading, className, children, ...props }, ref) {
  const base = {
    primary: 'btn-primary',
    ghost: 'btn-ghost',
    outline: 'btn-outline',
    danger: 'btn border border-state-err/30 bg-state-err/10 text-state-err hover:bg-state-err/20',
  }[variant]
  return (
    <button
      ref={ref}
      className={cn(base, size === 'sm' && 'px-2.5 py-1.5 text-xs', className)}
      disabled={loading || props.disabled}
      {...props}
    >
      {loading ? <Spinner className="h-3.5 w-3.5" /> : null}
      {children}
    </button>
  )
})

export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={cn('animate-spin', className)} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}

/* ------------------------------------------------------------- Skeleton */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton h-4 w-full', className)} />
}

export function SkeletonCard({ rows = 3 }: { rows?: number }) {
  return (
    <div className="card space-y-3 p-5">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="h-8 w-32" />
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className="h-3 w-full" />
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- Empty */
export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string
  description?: string
  action?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      {icon ? <div className="text-ink-subtle">{icon}</div> : null}
      <p className="text-sm font-medium text-ink">{title}</p>
      {description ? <p className="max-w-md text-xs text-ink-muted">{description}</p> : null}
      {action}
    </div>
  )
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : 'Something went wrong'
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-12 text-center">
      <Badge tone="err">Request failed</Badge>
      <p className="max-w-lg text-sm text-ink-muted">{message}</p>
      {retry ? (
        <Button size="sm" onClick={retry}>
          Retry
        </Button>
      ) : null}
    </div>
  )
}

/* -------------------------------------------------------------- Tooltip */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipPrimitive.Provider delayDuration={200}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            sideOffset={6}
            className="z-50 max-w-xs rounded-lg border border-line bg-surface-raised px-2.5 py-1.5 text-xs text-ink shadow-glass-lg"
          >
            {content}
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  )
}

/* ----------------------------------------------------------------- Tabs */
export function Tabs({
  tabs,
  value,
  onValueChange,
  children,
  className,
}: {
  tabs: { value: string; label: ReactNode; count?: number }[]
  value: string
  onValueChange: (value: string) => void
  children: ReactNode
  className?: string
}) {
  return (
    <TabsPrimitive.Root value={value} onValueChange={onValueChange} className={className}>
      <TabsPrimitive.List className="mb-4 flex gap-1 overflow-x-auto no-scrollbar rounded-xl border border-line/70 bg-surface-muted/60 p-1">
        {tabs.map((tab) => (
          <TabsPrimitive.Trigger
            key={tab.value}
            value={tab.value}
            className="flex items-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium text-ink-muted transition-colors data-[state=active]:bg-surface-raised data-[state=active]:text-ink data-[state=active]:shadow-glass"
          >
            {tab.label}
            {tab.count !== undefined ? (
              <span className="rounded-full bg-accent-soft px-1.5 text-2xs tabular-nums">{tab.count}</span>
            ) : null}
          </TabsPrimitive.Trigger>
        ))}
      </TabsPrimitive.List>
      {children}
    </TabsPrimitive.Root>
  )
}

export const TabPanel = TabsPrimitive.Content

/* ---------------------------------------------------------------- Modal */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  size = 'md',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  size?: 'md' | 'lg' | 'xl'
}) {
  const width = { md: 'max-w-lg', lg: 'max-w-3xl', xl: 'max-w-5xl' }[size]
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm data-[state=open]:animate-fade-up" />
        <DialogPrimitive.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2',
            'max-h-[88vh] overflow-y-auto rounded-2xl border border-line bg-surface-raised shadow-glass-lg',
            'data-[state=open]:animate-fade-up',
            width,
          )}
        >
          <div className="flex items-start justify-between gap-4 border-b border-line/70 px-5 py-4">
            <div>
              <DialogPrimitive.Title className="text-sm font-semibold text-ink">{title}</DialogPrimitive.Title>
              {description ? (
                <DialogPrimitive.Description className="mt-1 text-xs text-ink-muted">
                  {description}
                </DialogPrimitive.Description>
              ) : null}
            </div>
            <DialogPrimitive.Close className="rounded-lg p-1 text-ink-subtle transition-colors hover:bg-accent-soft hover:text-ink">
              <XMarkIcon className="h-4 w-4" />
            </DialogPrimitive.Close>
          </div>
          <div className="px-5 py-4">{children}</div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/* ------------------------------------------------------------- JSON view */
export function JsonView({ data, className, maxHeight = 'max-h-96' }: { data: unknown; className?: string; maxHeight?: string }) {
  return (
    <pre
      className={cn(
        'overflow-auto rounded-xl border border-line/70 bg-surface-muted/60 p-3 font-mono text-2xs leading-relaxed text-ink-muted',
        maxHeight,
        className,
      )}
    >
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}

/* --------------------------------------------------------------- Progress */
export function Meter({
  value,
  max = 100,
  tone = 'info',
  className,
}: {
  value: number
  max?: number
  tone?: 'ok' | 'warn' | 'err' | 'info'
  className?: string
}) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100))
  const color = {
    ok: 'bg-state-ok',
    warn: 'bg-state-warn',
    err: 'bg-state-err',
    info: 'bg-state-info',
  }[tone]
  return (
    <div
      className={cn('h-1.5 w-full overflow-hidden rounded-full bg-accent-soft', className)}
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <motion.div
        className={cn('h-full rounded-full', color)}
        initial={{ width: 0 }}
        animate={{ width: `${pct}%` }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
      />
    </div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {description ? <p className="mt-1 max-w-2xl text-sm text-ink-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}

/* ------------------------------------------------------- Motion helpers */
/**
 * Count a number up when it first appears and whenever it changes, so a refreshed
 * figure is visibly a new figure. Falls back to the plain value under reduced motion.
 */
export function AnimatedNumber({
  value,
  format,
  duration = 0.9,
  className,
}: {
  value: number
  format?: (value: number) => string
  duration?: number
  className?: string
}) {
  const reduceMotion = useReducedMotion()
  const render = format ?? ((next: number) => next.toLocaleString('en-US'))
  const spring = useSpring(reduceMotion ? value : 0, {
    duration: duration * 1000,
    bounce: 0,
  })
  const text = useTransform(spring, (latest) => render(latest))

  useEffect(() => {
    if (reduceMotion) spring.jump(value)
    else spring.set(value)
  }, [value, reduceMotion, spring])

  if (reduceMotion) return <span className={className}>{render(value)}</span>
  return <motion.span className={className}>{text}</motion.span>
}

/** Staggered entrance for a list or grid. Children animate in sequence. */
export function Stagger({
  children,
  delay = 0,
  gap = 0.05,
  className,
}: {
  children: ReactNode
  delay?: number
  gap?: number
  className?: string
}) {
  return (
    <motion.div
      className={className}
      initial="hidden"
      animate="visible"
      variants={{
        hidden: {},
        visible: { transition: { delayChildren: delay, staggerChildren: gap } },
      }}
    >
      {children}
    </motion.div>
  )
}

/** A single item inside a `Stagger`, or a standalone fade-up on mount. */
export function Reveal({
  children,
  className,
  y = 10,
  ...props
}: HTMLMotionProps<'div'> & { y?: number }) {
  return (
    <motion.div
      className={className}
      variants={{
        hidden: { opacity: 0, y },
        visible: { opacity: 1, y: 0, transition: { duration: 0.4, ease: [0.22, 1, 0.36, 1] } },
      }}
      initial="hidden"
      animate="visible"
      {...props}
    >
      {children}
    </motion.div>
  )
}
