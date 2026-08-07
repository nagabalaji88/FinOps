import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, Badge, Button, Card, CardHeader, EmptyState, ErrorState, Meter, Modal, PageHeader, SkeletonCard, TabPanel, Tabs, useAuth, useToasts, formatDateTime, formatPercent, relativeTime, titleCase } from '@finops/shared'

interface SecurityOverview {
  authentication: Record<string, number | boolean>
  authorisation: { roles: number; permissions: number; role_distribution: Record<string, number> }
  api_keys: Record<string, number>
  secrets: { backend: string; vault_connected: boolean; stored: number; rotation_due: number }
  feature_flags: { key: string; enabled: boolean; rollout_percentage: number; description: string }[]
  posture: Record<string, unknown>
}

interface AuditRow {
  id: string
  timestamp: string
  actor_email: string | null
  actor_type: string
  action: string
  resource_type: string
  resource_id: string | null
  outcome: string
  severity: string
  ip_address: string | null
  details: Record<string, unknown>
}

export default function Security() {
  const queryClient = useQueryClient()
  const push = useToasts((state) => state.push)
  const { can } = useAuth()
  const [tab, setTab] = useState('posture')
  const [showKey, setShowKey] = useState<string | null>(null)

  const overview = useQuery({
    queryKey: ['security'],
    queryFn: () => api.get<SecurityOverview>('/security/overview'),
    refetchInterval: 60_000,
  })
  const audit = useQuery({
    queryKey: ['audit'],
    queryFn: () => api.get<{ total: number; items: AuditRow[] }>('/security/audit', { limit: 200 }),
    enabled: tab === 'audit' && can('audit:read'),
  })
  const roles = useQuery({
    queryKey: ['roles'],
    queryFn: () => api.get<{ role: string; description: string; permissions: string[]; permission_count: number }[]>('/auth/roles'),
    enabled: tab === 'rbac',
  })
  const apiKeys = useQuery({
    queryKey: ['api-keys'],
    queryFn: () => api.get<any[]>('/auth/api-keys'),
    enabled: tab === 'keys',
  })
  const users = useQuery({
    queryKey: ['users'],
    queryFn: () => api.get<any[]>('/auth/users'),
    enabled: tab === 'users' && can('user:admin'),
  })

  const createKey = useMutation({
    mutationFn: () => api.post<{ api_key: string }>('/auth/api-keys', { name: `console-${Date.now()}` }),
    onSuccess: (response) => {
      setShowKey(response.api_key)
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
    onError: (error) => push({ title: 'Could not create key', description: (error as Error).message, tone: 'err' }),
  })

  const revokeKey = useMutation({
    mutationFn: (id: string) => api.del(`/auth/api-keys/${id}`),
    onSuccess: () => {
      push({ title: 'API key revoked', tone: 'warn' })
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })

  const toggleFlag = useMutation({
    mutationFn: ({ key, enabled }: { key: string; enabled: boolean }) =>
      api.put(`/security/feature-flags/${key}`, { enabled, rollout_percentage: 100 }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['security'] }),
  })

  if (overview.isError) return <ErrorState error={overview.error} retry={() => overview.refetch()} />
  const data = overview.data

  return (
    <div className="space-y-5">
      <PageHeader
        title="Security"
        description="Authentication, RBAC, API keys, secrets, feature flags and the immutable audit trail."
      />

      {!data ? (
        <div className="grid gap-4 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <SkeletonCard key={index} rows={2} />
          ))}
        </div>
      ) : (
        <>
          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Card className="px-5 py-4">
              <p className="metric-label">Users</p>
              <p className="metric-value mt-2">{data.authentication.users_total as number}</p>
              <p className="mt-1.5 text-2xs text-ink-subtle">
                {data.authentication.active_users as number} active · {data.authentication.service_accounts as number} service
              </p>
            </Card>
            <Card className="px-5 py-4">
              <p className="metric-label">MFA coverage</p>
              <p className="metric-value mt-2">{formatPercent(data.authentication.mfa_coverage_pct as number)}</p>
              <Meter
                value={data.authentication.mfa_coverage_pct as number}
                tone={(data.authentication.mfa_coverage_pct as number) > 80 ? 'ok' : 'warn'}
                className="mt-2"
              />
            </Card>
            <Card className="px-5 py-4">
              <p className="metric-label">API keys</p>
              <p className="metric-value mt-2">{data.api_keys.active}</p>
              <p className="mt-1.5 text-2xs text-ink-subtle">
                {data.api_keys.revoked} revoked · {data.api_keys.expiring_30d} expiring in 30d
              </p>
            </Card>
            <Card className="px-5 py-4">
              <p className="metric-label">Failed logins (24h)</p>
              <p className="metric-value mt-2">{data.authentication.failed_logins_24h as number}</p>
              <p className="mt-1.5 text-2xs text-ink-subtle">
                {data.authentication.locked_accounts as number} locked accounts
              </p>
            </Card>
          </section>

          <Tabs
            value={tab}
            onValueChange={setTab}
            tabs={[
              { value: 'posture', label: 'Posture' },
              { value: 'rbac', label: 'RBAC' },
              { value: 'keys', label: 'API keys' },
              { value: 'users', label: 'Users' },
              { value: 'audit', label: 'Audit trail' },
            ]}
          >
            <TabPanel value="posture">
              <div className="grid gap-4 md:grid-cols-2">
                <Card>
                  <CardHeader title="Zero trust controls" subtitle="Enforced on every request" />
                  <div className="space-y-2 px-5 pb-5 pt-3">
                    {Object.entries((data.posture.zero_trust ?? {}) as Record<string, boolean>).map(([key, value]) => (
                      <div key={key} className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2">
                        <span className="text-xs text-ink-muted">{titleCase(key)}</span>
                        <Badge tone={value ? 'ok' : 'err'}>{value ? 'enforced' : 'off'}</Badge>
                      </div>
                    ))}
                    <div className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2">
                      <span className="text-xs text-ink-muted">Default JWT secret in use</span>
                      <Badge tone={data.posture.default_secret_in_use ? 'err' : 'ok'}>
                        {data.posture.default_secret_in_use ? 'rotate now' : 'rotated'}
                      </Badge>
                    </div>
                    <div className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2">
                      <span className="text-xs text-ink-muted">Secret backend</span>
                      <Badge tone={data.secrets.vault_connected ? 'ok' : 'warn'}>{data.secrets.backend}</Badge>
                    </div>
                    <div className="flex items-center justify-between rounded-xl border border-line/70 px-3 py-2">
                      <span className="text-xs text-ink-muted">SSO</span>
                      <Badge tone={data.authentication.sso_configured ? 'ok' : 'idle'}>
                        {data.authentication.sso_configured ? 'configured' : 'not configured'}
                      </Badge>
                    </div>
                  </div>
                </Card>

                <Card>
                  <CardHeader title="Feature flags" subtitle="Runtime capability switches" />
                  <div className="space-y-2 px-5 pb-5 pt-3">
                    {data.feature_flags.map((flag) => (
                      <div key={flag.key} className="rounded-xl border border-line/70 px-3 py-2">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-2xs">{flag.key}</span>
                          <button
                            disabled={!can('flag:admin')}
                            onClick={() => toggleFlag.mutate({ key: flag.key, enabled: !flag.enabled })}
                            className="disabled:opacity-50"
                          >
                            <Badge tone={flag.enabled ? 'ok' : 'idle'}>{flag.enabled ? 'on' : 'off'}</Badge>
                          </button>
                        </div>
                        <p className="mt-1 text-2xs text-ink-subtle">{flag.description}</p>
                      </div>
                    ))}
                  </div>
                </Card>
              </div>
            </TabPanel>

            <TabPanel value="rbac">
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {roles.data?.map((role) => (
                  <Card key={role.role}>
                    <CardHeader
                      title={titleCase(role.role)}
                      subtitle={role.description}
                      action={<Badge tone="idle">{role.permission_count}</Badge>}
                    />
                    <div className="flex flex-wrap gap-1 px-5 pb-5 pt-3">
                      {role.permissions.slice(0, 14).map((permission) => (
                        <span key={permission} className="chip font-mono">
                          {permission}
                        </span>
                      ))}
                      {role.permissions.length > 14 ? (
                        <span className="chip">+{role.permissions.length - 14} more</span>
                      ) : null}
                    </div>
                  </Card>
                ))}
              </div>
            </TabPanel>

            <TabPanel value="keys">
              <Card className="overflow-hidden">
                <CardHeader
                  title="API keys"
                  subtitle="Machine-to-machine credentials"
                  action={
                    can('security:admin') ? (
                      <Button size="sm" loading={createKey.isPending} onClick={() => createKey.mutate()}>
                        Create key
                      </Button>
                    ) : null
                  }
                />
                <div className="scroll-x">
                  <table className="w-full min-w-[720px] text-xs">
                    <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
                      <tr>
                        {['Name', 'Prefix', 'Rate limit', 'Usage', 'Last used', 'Status', ''].map((header) => (
                          <th key={header} className="px-4 py-2.5 text-left font-medium">
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {apiKeys.data?.map((key) => (
                        <tr key={key.id} className="table-row">
                          <td className="px-4 py-2.5">{key.name}</td>
                          <td className="px-4 py-2.5 font-mono text-2xs">{key.prefix}</td>
                          <td className="px-4 py-2.5 tabular-nums">{key.rate_limit_per_minute}/min</td>
                          <td className="px-4 py-2.5 tabular-nums">{key.usage_count}</td>
                          <td className="px-4 py-2.5 text-ink-muted">{relativeTime(key.last_used_at)}</td>
                          <td className="px-4 py-2.5">
                            <Badge tone={key.revoked ? 'err' : 'ok'}>{key.revoked ? 'revoked' : 'active'}</Badge>
                          </td>
                          <td className="px-4 py-2.5 text-right">
                            {!key.revoked && can('security:admin') ? (
                              <Button size="sm" variant="ghost" onClick={() => revokeKey.mutate(key.id)}>
                                Revoke
                              </Button>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {apiKeys.data?.length === 0 ? <EmptyState title="No API keys" /> : null}
              </Card>
            </TabPanel>

            <TabPanel value="users">
              <Card className="overflow-hidden">
                <CardHeader title="Users" subtitle="Platform identities and role bindings" />
                <div className="scroll-x">
                  <table className="w-full min-w-[720px] text-xs">
                    <thead className="border-b border-line/70 bg-surface-muted/50 text-2xs uppercase tracking-wider text-ink-subtle">
                      <tr>
                        {['User', 'Roles', 'Department', 'MFA', 'Last login', 'Status'].map((header) => (
                          <th key={header} className="px-4 py-2.5 text-left font-medium">
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {users.data?.map((user) => (
                        <tr key={user.id} className="table-row">
                          <td className="px-4 py-2.5">
                            <p className="font-medium">{user.full_name}</p>
                            <p className="text-2xs text-ink-subtle">{user.email}</p>
                          </td>
                          <td className="px-4 py-2.5">
                            <div className="flex flex-wrap gap-1">
                              {user.roles.map((role: string) => (
                                <span key={role} className="chip">
                                  {role}
                                </span>
                              ))}
                            </div>
                          </td>
                          <td className="px-4 py-2.5 text-ink-muted">{user.department}</td>
                          <td className="px-4 py-2.5">
                            <Badge tone={user.mfa_enabled ? 'ok' : 'warn'}>{user.mfa_enabled ? 'on' : 'off'}</Badge>
                          </td>
                          <td className="px-4 py-2.5 text-ink-muted">{relativeTime(user.last_login_at)}</td>
                          <td className="px-4 py-2.5">
                            <Badge tone={user.is_active ? 'ok' : 'err'}>{user.is_active ? 'active' : 'disabled'}</Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </TabPanel>

            <TabPanel value="audit">
              <Card className="overflow-hidden">
                <CardHeader title="Audit trail" subtitle={`${audit.data?.total ?? 0} recorded events`} />
                <div className="max-h-[560px] overflow-y-auto">
                  <table className="w-full min-w-[840px] text-xs">
                    <thead className="sticky top-0 border-b border-line/70 bg-surface-raised/95 text-2xs uppercase tracking-wider text-ink-subtle backdrop-blur">
                      <tr>
                        {['Time', 'Actor', 'Action', 'Resource', 'Outcome', 'IP'].map((header) => (
                          <th key={header} className="px-4 py-2.5 text-left font-medium">
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {audit.data?.items.map((row) => (
                        <tr key={row.id} className="table-row">
                          <td className="whitespace-nowrap px-4 py-2 text-ink-muted">{formatDateTime(row.timestamp)}</td>
                          <td className="px-4 py-2">
                            <p>{row.actor_email ?? 'system'}</p>
                            <p className="text-2xs text-ink-subtle">{row.actor_type}</p>
                          </td>
                          <td className="px-4 py-2 font-mono text-2xs">{row.action}</td>
                          <td className="px-4 py-2 text-ink-muted">
                            {row.resource_type}
                            {row.resource_id ? `:${row.resource_id.slice(0, 12)}` : ''}
                          </td>
                          <td className="px-4 py-2">
                            <Badge tone={row.outcome === 'success' ? 'ok' : 'err'}>{row.outcome}</Badge>
                          </td>
                          <td className="px-4 py-2 font-mono text-2xs text-ink-subtle">{row.ip_address ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </TabPanel>
          </Tabs>
        </>
      )}

      {showKey ? (
        <Modal open onOpenChange={() => setShowKey(null)} title="API key created" description="Copy it now — it is never shown again.">
          <code className="block break-all rounded-xl border border-line bg-surface-muted/60 p-3 font-mono text-2xs">
            {showKey}
          </code>
        </Modal>
      ) : null}
    </div>
  )
}
