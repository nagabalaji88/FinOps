import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Badge, Card, CardHeader, PageHeader } from '@/components/ui'
import { useAuth, useUi } from '@/store'

const SHORTCUTS = [
  ['⌘K / Ctrl+K', 'Open the command palette'],
  ['Esc', 'Close the palette or dialog'],
  ['Shift+?', 'Open this settings page'],
]

export default function Settings() {
  const { user } = useAuth()
  const { theme, setTheme, density, setDensity } = useUi()
  const sso = useQuery({ queryKey: ['sso'], queryFn: () => api.get<Record<string, unknown>>('/auth/sso') })

  return (
    <div className="space-y-5">
      <PageHeader title="Settings" description="Console preferences, session identity and keyboard shortcuts." />

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader title="Appearance" subtitle="Theme and information density" />
          <div className="space-y-4 px-5 pb-5 pt-3">
            <div>
              <p className="metric-label mb-2">Theme</p>
              <div className="flex gap-2">
                {(['light', 'dark'] as const).map((value) => (
                  <button
                    key={value}
                    onClick={() => setTheme(value)}
                    className={`btn ${theme === value ? 'btn-primary' : 'btn-outline'} flex-1`}
                  >
                    {value === 'light' ? 'Light' : 'Dark'}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <p className="metric-label mb-2">Density</p>
              <div className="flex gap-2">
                {(['comfortable', 'compact'] as const).map((value) => (
                  <button
                    key={value}
                    onClick={() => setDensity(value)}
                    className={`btn ${density === value ? 'btn-primary' : 'btn-outline'} flex-1 capitalize`}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader title="Session" subtitle="Your identity and effective permissions" />
          <dl className="space-y-2 px-5 pb-5 pt-3 text-xs">
            <div className="flex justify-between">
              <dt className="text-ink-subtle">Name</dt>
              <dd className="font-medium">{user?.full_name}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-ink-subtle">Email</dt>
              <dd className="font-medium">{user?.email}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-ink-subtle">Department</dt>
              <dd className="font-medium">{user?.department}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-ink-subtle">MFA</dt>
              <dd>
                <Badge tone={user?.mfa_enabled ? 'ok' : 'warn'}>{user?.mfa_enabled ? 'enabled' : 'disabled'}</Badge>
              </dd>
            </div>
            <div>
              <dt className="mb-1.5 text-ink-subtle">Roles</dt>
              <dd className="flex flex-wrap gap-1">
                {user?.roles.map((role) => (
                  <span key={role} className="chip">
                    {role}
                  </span>
                ))}
              </dd>
            </div>
            <div>
              <dt className="mb-1.5 text-ink-subtle">Permissions ({user?.permissions.length})</dt>
              <dd className="flex max-h-32 flex-wrap gap-1 overflow-y-auto">
                {user?.permissions.map((permission) => (
                  <span key={permission} className="chip font-mono">
                    {permission}
                  </span>
                ))}
              </dd>
            </div>
          </dl>
        </Card>

        <Card>
          <CardHeader title="Keyboard shortcuts" />
          <div className="space-y-2 px-5 pb-5 pt-3">
            {SHORTCUTS.map(([keys, description]) => (
              <div key={keys} className="flex items-center justify-between text-xs">
                <span className="text-ink-muted">{description}</span>
                <kbd className="rounded border border-line px-1.5 py-0.5 font-mono text-2xs">{keys}</kbd>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <CardHeader title="Single sign-on" subtitle="Identity provider configuration" />
          <div className="px-5 pb-5 pt-3 text-xs">
            {sso.data?.configured ? (
              <div className="space-y-1.5">
                <Badge tone="ok">Keycloak configured</Badge>
                <p className="break-all font-mono text-2xs text-ink-muted">{String(sso.data.issuer)}</p>
              </div>
            ) : (
              <div className="space-y-1.5">
                <Badge tone="idle">Not configured</Badge>
                <p className="text-2xs text-ink-muted">
                  Set {(sso.data?.required_settings as string[] | undefined)?.join(', ')} to enable SSO.
                </p>
              </div>
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}
