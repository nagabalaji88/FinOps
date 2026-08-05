import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { useEffect } from 'react'
import { Command } from 'cmdk'
import {
  ArrowRightOnRectangleIcon,
  BanknotesIcon,
  BeakerIcon,
  BellAlertIcon,
  ChartBarSquareIcon,
  ChevronLeftIcon,
  CircleStackIcon,
  Cog6ToothIcon,
  CommandLineIcon,
  CpuChipIcon,
  DocumentTextIcon,
  MagnifyingGlassIcon,
  MoonIcon,
  ShieldCheckIcon,
  Squares2X2Icon,
  SunIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline'
import { useQuery } from '@tanstack/react-query'
import { api, type Overview } from '@/lib/api'
import { useAuth, useToasts, useUi } from '@/store'
import { cn } from '@/lib/utils'
import { Badge, StatusDot, Tooltip } from '@/components/ui'

const NAV = [
  { to: '/', label: 'Overview', icon: Squares2X2Icon, permission: 'metric:read', end: true },
  { to: '/agents', label: 'Agents', icon: CpuChipIcon, permission: 'agent:read' },
  { to: '/executions', label: 'Executions', icon: CommandLineIcon, permission: 'execution:read' },
  { to: '/approvals', label: 'Approvals', icon: BellAlertIcon, permission: 'approval:read', badge: 'approvals' },
  { to: '/costs', label: 'Cost', icon: BanknotesIcon, permission: 'cost:read' },
  { to: '/knowledge', label: 'Knowledge', icon: CircleStackIcon, permission: 'knowledge:read' },
  { to: '/tools', label: 'Tools & APIs', icon: WrenchScrewdriverIcon, permission: 'tool:read' },
  { to: '/services', label: 'Services', icon: ChartBarSquareIcon, permission: 'service:read' },
  { to: '/evaluations', label: 'Evaluation', icon: BeakerIcon, permission: 'eval:read' },
  { to: '/playground', label: 'Playground', icon: BeakerIcon, permission: 'playground:use' },
  { to: '/builder', label: 'Agent Builder', icon: Cog6ToothIcon, permission: 'agent:write' },
  { to: '/logs', label: 'Logs', icon: DocumentTextIcon, permission: 'log:read' },
  { to: '/security', label: 'Security', icon: ShieldCheckIcon, permission: 'security:read' },
]

export function AppShell() {
  const { user, logout, can } = useAuth()
  const { sidebarCollapsed, toggleSidebar, theme, toggleTheme, commandOpen, setCommandOpen } = useUi()
  const navigate = useNavigate()
  const { toasts, dismiss } = useToasts()

  const { data: overview } = useQuery({
    queryKey: ['overview-nav'],
    queryFn: () => api.get<Overview>('/dashboard/overview'),
    refetchInterval: 20_000,
    enabled: can('metric:read'),
  })

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setCommandOpen(!commandOpen)
      }
      if (event.key === 'Escape') setCommandOpen(false)
      if (event.key === '?' && event.shiftKey && !commandOpen) navigate('/settings')
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [commandOpen, navigate, setCommandOpen])

  const pendingApprovals = overview?.approvals?.pending ?? 0
  const visibleNav = NAV.filter((item) => can(item.permission))

  return (
    <div className="flex h-full">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-surface-raised focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>

      {/* Sidebar */}
      <motion.aside
        animate={{ width: sidebarCollapsed ? 68 : 232 }}
        transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
        className="relative z-20 hidden shrink-0 flex-col border-r border-line/70 bg-surface-raised/50 backdrop-blur-xl lg:flex"
      >
        <div className="flex h-16 items-center gap-2.5 px-4">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-ink text-surface">
            <span className="text-sm font-semibold">F</span>
          </div>
          {!sidebarCollapsed && (
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold tracking-tight">FinOps</p>
              <p className="truncate text-2xs text-ink-subtle">AI Command Center</p>
            </div>
          )}
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 py-2 no-scrollbar" aria-label="Primary">
          {visibleNav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'group flex items-center gap-3 rounded-xl px-3 py-2 text-sm transition-all duration-200',
                  isActive
                    ? 'bg-accent-soft font-medium text-ink shadow-inset'
                    : 'text-ink-muted hover:bg-accent-soft/60 hover:text-ink',
                )
              }
            >
              <item.icon className="h-[18px] w-[18px] shrink-0" />
              {!sidebarCollapsed && <span className="truncate">{item.label}</span>}
              {!sidebarCollapsed && item.badge === 'approvals' && pendingApprovals > 0 && (
                <Badge tone="warn" className="ml-auto">
                  {pendingApprovals}
                </Badge>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-line/70 p-2">
          <button
            onClick={toggleSidebar}
            className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm text-ink-muted transition-colors hover:bg-accent-soft hover:text-ink"
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <ChevronLeftIcon className={cn('h-[18px] w-[18px] transition-transform', sidebarCollapsed && 'rotate-180')} />
            {!sidebarCollapsed && <span>Collapse</span>}
          </button>
        </div>
      </motion.aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line/70 bg-surface/70 px-4 backdrop-blur-xl sm:px-6">
          <button
            onClick={() => setCommandOpen(true)}
            className="group flex flex-1 items-center gap-2.5 rounded-xl border border-line/80 bg-surface-raised/60 px-3 py-2 text-sm text-ink-subtle transition-colors hover:border-line hover:text-ink-muted sm:max-w-md"
          >
            <MagnifyingGlassIcon className="h-4 w-4" />
            <span className="flex-1 text-left">Search agents, executions, docs…</span>
            <kbd className="hidden rounded border border-line px-1.5 py-0.5 font-mono text-2xs sm:inline">⌘K</kbd>
          </button>

          <div className="ml-auto flex items-center gap-2">
            {overview ? (
              <Tooltip
                content={`${overview.agents.running ?? 0} running · ${overview.agents.queued ?? 0} queued · ${
                  overview.executions.failed_24h ?? 0
                } failed in 24h`}
              >
                <div className="hidden items-center gap-2 rounded-xl border border-line/70 px-2.5 py-1.5 text-2xs text-ink-muted md:flex">
                  <StatusDot status={(overview.agents.running ?? 0) > 0 ? 'running' : 'ok'} pulse={(overview.agents.running ?? 0) > 0} />
                  <span className="tabular-nums">{overview.agents.running ?? 0} running</span>
                  <span className="text-line">|</span>
                  <span className="tabular-nums">{overview.agents.queued ?? 0} queued</span>
                </div>
              </Tooltip>
            ) : null}

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
          <div className="mx-auto w-full max-w-[1600px]">
            <Outlet />
          </div>
        </main>
      </div>

      {/* Command palette */}
      <AnimatePresence>
        {commandOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh] backdrop-blur-sm"
            onClick={() => setCommandOpen(false)}
          >
            <motion.div
              initial={{ opacity: 0, y: -8, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -8, scale: 0.98 }}
              transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
              onClick={(event) => event.stopPropagation()}
              className="w-full max-w-xl overflow-hidden rounded-2xl border border-line bg-surface-raised shadow-glass-lg"
            >
              <Command label="Command palette">
                <Command.Input
                  autoFocus
                  placeholder="Jump to…"
                  className="w-full border-b border-line/70 bg-transparent px-4 py-3.5 text-sm outline-none placeholder:text-ink-subtle"
                />
                <Command.List className="max-h-80 overflow-y-auto p-2">
                  <Command.Empty className="px-3 py-6 text-center text-xs text-ink-subtle">
                    No matches
                  </Command.Empty>
                  <Command.Group heading="Navigate" className="text-2xs uppercase tracking-wider text-ink-subtle">
                    {visibleNav.map((item) => (
                      <Command.Item
                        key={item.to}
                        value={item.label}
                        onSelect={() => {
                          navigate(item.to)
                          setCommandOpen(false)
                        }}
                        className="flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm text-ink-muted data-[selected=true]:bg-accent-soft data-[selected=true]:text-ink"
                      >
                        <item.icon className="h-4 w-4" />
                        {item.label}
                      </Command.Item>
                    ))}
                  </Command.Group>
                </Command.List>
              </Command>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Toasts */}
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
