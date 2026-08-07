/**
 * Execute console shell.
 *
 * Two destinations, so navigation is a pair of tabs in the header rather than a sidebar.
 */
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import {
  ArrowRightOnRectangleIcon,
  ChartBarIcon,
  MoonIcon,
  PlayCircleIcon,
  SunIcon,
} from '@heroicons/react/24/outline'
import { StatusDot, Tooltip, cn, useAuth, useToasts, useUi } from '@finops/shared'

const TABS = [
  { to: '/', label: 'Execute', icon: PlayCircleIcon, end: true },
  { to: '/analytics', label: 'Analytics', icon: ChartBarIcon, end: false },
]

export function Shell() {
  const { user, logout } = useAuth()
  const { theme, toggleTheme } = useUi()
  const { toasts, dismiss } = useToasts()
  const navigate = useNavigate()
  const location = useLocation()

  return (
    <div className="flex h-full flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-surface-raised focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-4 border-b border-line/70 bg-surface/70 px-4 backdrop-blur-xl sm:px-6">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-ink text-surface">
            <span className="text-sm font-semibold">F</span>
          </div>
          <div className="hidden min-w-0 sm:block">
            <p className="truncate text-sm font-semibold tracking-tight">FinOps Execute</p>
            <p className="truncate text-2xs text-ink-subtle">Agent operations</p>
          </div>
        </div>

        <nav
          className="flex items-center gap-1 rounded-xl border border-line/70 bg-surface-muted/60 p-1"
          aria-label="Primary"
        >
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              end={tab.end}
              className={({ isActive }) =>
                cn(
                  'relative flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                  isActive ? 'text-ink' : 'text-ink-muted hover:text-ink',
                )
              }
            >
              {({ isActive }) => (
                <>
                  {isActive && (
                    <motion.span
                      layoutId="execute-tab"
                      className="absolute inset-0 rounded-lg bg-surface-raised shadow-glass"
                      transition={{ type: 'spring', stiffness: 420, damping: 34 }}
                    />
                  )}
                  <tab.icon className="relative h-4 w-4" />
                  <span className="relative">{tab.label}</span>
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <Tooltip content={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}>
            <button
              onClick={toggleTheme}
              className="rounded-xl p-2 text-ink-muted transition-colors hover:bg-accent-soft hover:text-ink"
              aria-label="Toggle colour theme"
            >
              {theme === 'dark' ? <SunIcon className="h-[18px] w-[18px]" /> : <MoonIcon className="h-[18px] w-[18px]" />}
            </button>
          </Tooltip>

          <div className="flex items-center gap-2.5 rounded-xl border border-line/70 py-1 pl-2.5 pr-1">
            <div className="hidden text-right sm:block">
              <p className="text-xs font-medium leading-tight">{user?.full_name}</p>
              <p className="text-2xs leading-tight text-ink-subtle">{user?.roles.join(', ')}</p>
            </div>
            <Tooltip content="Sign out">
              <button
                onClick={() => {
                  logout()
                  navigate('/login')
                }}
                className="rounded-lg p-1.5 text-ink-muted transition-colors hover:bg-accent-soft hover:text-ink"
                aria-label="Sign out"
              >
                <ArrowRightOnRectangleIcon className="h-[18px] w-[18px]" />
              </button>
            </Tooltip>
          </div>
        </div>
      </header>

      <main id="main" className="flex-1 overflow-y-auto px-4 py-6 sm:px-6 lg:px-8">
        <div className="mx-auto w-full max-w-[1500px]">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            >
              <Outlet />
            </motion.div>
          </AnimatePresence>
        </div>
      </main>

      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col gap-2">
        <AnimatePresence>
          {toasts.map((toast) => (
            <motion.div
              key={toast.id}
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, x: 20 }}
              className="pointer-events-auto rounded-xl border border-line bg-surface-raised p-3.5 shadow-glass-lg"
              role="status"
            >
              <div className="flex items-start gap-2.5">
                <StatusDot status={toast.tone === 'ok' ? 'succeeded' : toast.tone === 'err' ? 'failed' : 'running'} />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium">{toast.title}</p>
                  {toast.description ? <p className="mt-0.5 text-2xs text-ink-muted">{toast.description}</p> : null}
                </div>
                <button onClick={() => dismiss(toast.id)} className="text-2xs text-ink-subtle hover:text-ink">
                  Dismiss
                </button>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}
