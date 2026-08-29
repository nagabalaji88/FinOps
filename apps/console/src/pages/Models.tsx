/**
 * Model administration.
 *
 * Three questions, three tabs: which models can this deployment call, which one do the
 * agents run on, and are the credentials good. Every answer here describes the running
 * process rather than the environment it started with, and every change applies to the next
 * model call rather than the next restart.
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  PageHeader,
  SkeletonCard,
  Tabs,
  TabPanel,
  cn,
} from '@finops/shared'

interface CatalogueModel {
  id: string
  display_name: string
  provider: string
  tier: string
  context_window: number
  max_output_tokens: number
  input_price_per_mtok: number
  output_price_per_mtok: number
  supports_tools: boolean
  supports_vision: boolean
  supports_json_mode: boolean
  is_embedding: boolean
  credential_env_var: string | null
  credential_available: boolean
  rejected_by_provider: string | null
  is_local_runtime: boolean
}

interface Catalogue {
  selection: {
    default_model: string | null
    guardrails_model: string | null
    effective_guardrails_model: string | null
    aligned: boolean
  }
  readiness: { ready: boolean; configured: string[]; usable: string[]; reason: string | null }
  models: CatalogueModel[]
}

interface LiveModels {
  providers: {
    provider: string
    status: string
    latency_ms?: number
    error?: string
    models: { id: string; in_catalogue: boolean }[]
  }[]
  total: number
  note: string
}

interface KeyRow {
  provider: string
  env_var: string | null
  source: 'stored' | 'environment' | 'unset'
  set: boolean
  usable: boolean
  catalogued_models: number
}

type TestResult = {
  ok: boolean
  error?: string
  served_by?: string
  latency_ms?: number
  cost_usd?: number
  reply?: string
  reachable_models?: number
}

const money = (value: number) => (value === 0 ? 'free' : `$${value.toFixed(2)}`)

export default function Models() {
  const [tab, setTab] = useState('registry')
  const queryClient = useQueryClient()

  const catalogue = useQuery({
    queryKey: ['models', 'catalogue'],
    queryFn: () => api.get<Catalogue>('/models'),
  })
  const keys = useQuery({ queryKey: ['models', 'keys'], queryFn: () => api.get<KeyRow[]>('/models/keys') })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['models'] })
    void queryClient.invalidateQueries({ queryKey: ['services'] })
  }

  if (catalogue.isError) return <ErrorState error={catalogue.error} retry={() => catalogue.refetch()} />

  const selection = catalogue.data?.selection
  const readiness = catalogue.data?.readiness

  return (
    <div className="space-y-5">
      <PageHeader
        title="Models"
        description="Choose the model your agents run on, prove it works, and manage the keys behind it."
        actions={
          readiness ? (
            <div className="flex flex-wrap gap-2">
              <Badge tone={readiness.ready ? 'ok' : 'err'}>
                {readiness.ready ? `${readiness.usable.length} provider(s) usable` : 'no usable provider'}
              </Badge>
              {selection?.default_model ? (
                <Badge tone="info">agents: {selection.default_model}</Badge>
              ) : (
                <Badge tone="idle">agents: router chooses</Badge>
              )}
            </div>
          ) : null
        }
      />

      {readiness && !readiness.ready ? (
        <Card className="border-state-err/30 bg-state-err/5">
          <p className="text-sm text-ink">
            No model can be called right now — {readiness.reason}. Agents will refuse to start until a
            provider key is set on the API keys tab.
          </p>
        </Card>
      ) : null}

      <Tabs
        value={tab}
        onValueChange={setTab}
        tabs={[
          { value: 'registry', label: 'Registry', count: catalogue.data?.models.length },
          { value: 'available', label: 'Available models' },
          { value: 'keys', label: 'API keys', count: keys.data?.filter((k) => k.set).length },
        ]}
      >
        <TabPanel value="registry">
          {catalogue.isLoading ? (
            <SkeletonCard rows={6} />
          ) : (
            <Registry data={catalogue.data!} onChanged={invalidate} />
          )}
        </TabPanel>
        <TabPanel value="available">
          <Available active={tab === 'available'} onChanged={invalidate} />
        </TabPanel>
        <TabPanel value="keys">
          {keys.isLoading ? <SkeletonCard rows={5} /> : <Keys rows={keys.data ?? []} onChanged={invalidate} />}
        </TabPanel>
      </Tabs>
    </div>
  )
}

/* ------------------------------------------------------------------ registry */

function Registry({ data, onChanged }: { data: Catalogue; onChanged: () => void }) {
  const [filter, setFilter] = useState('')
  // A hand-maintained price list goes stale, and a row for a model the provider has
  // already refused is worse than no row: it is an offer that cannot be accepted. Hidden
  // by default, counted so it is not a silent disappearance, and recoverable because a
  // refusal can be reversed by granting the entitlement.
  const [showRefused, setShowRefused] = useState(false)
  const [results, setResults] = useState<Record<string, TestResult>>({})
  const [testing, setTesting] = useState<string | null>(null)

  // A mutation with no error branch fails invisibly: the select snaps back on the next
  // refetch and the page looks like it simply ignored the click, which is indistinguishable
  // from a broken control. Whatever the server said belongs on screen.
  const [saveError, setSaveError] = useState<string | null>(null)
  const select = useMutation({
    mutationFn: (body: { default_model?: string; guardrails_model?: string }) =>
      api.put('/models/selection', body),
    onMutate: () => setSaveError(null),
    onSuccess: () => {
      setSaveError(null)
      onChanged()
    },
    onError: (error: unknown) =>
      setSaveError(error instanceof Error ? error.message : String(error)),
  })

  const test = async (id: string) => {
    setTesting(id)
    try {
      const result = await api.post<TestResult>('/models/test', { model_id: id })
      setResults((previous) => ({ ...previous, [id]: result }))
    } catch (error) {
      setResults((previous) => ({ ...previous, [id]: { ok: false, error: String(error) } }))
    } finally {
      setTesting(null)
    }
  }

  const refusedCount = useMemo(
    () => data.models.filter((m) => !m.is_embedding && m.rejected_by_provider).length,
    [data.models],
  )

  const models = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    let chat = data.models.filter((m) => !m.is_embedding)
    if (!showRefused) chat = chat.filter((m) => !m.rejected_by_provider)
    if (!needle) return chat
    return chat.filter(
      (m) => m.id.toLowerCase().includes(needle) || m.display_name.toLowerCase().includes(needle),
    )
  }, [data.models, filter, showRefused])

  // Grouped by provider, because that is the unit an operator reasons in: a credential is
  // per provider, so is an outage, and so is the decision to ignore one entirely.
  const groups = useMemo(() => {
    const byProvider = new Map<string, CatalogueModel[]>()
    for (const model of models) {
      byProvider.set(model.provider, [...(byProvider.get(model.provider) ?? []), model])
    }
    return [...byProvider.entries()]
      .map(([provider, rows]) => ({
        provider,
        rows,
        // A provider you cannot call is the one you are least likely to be reading, so it
        // starts closed -- but it stays listed, because "missing" and "unusable" are
        // different problems and hiding one as the other is how an operator loses an hour.
        usable: rows.some((r) => r.credential_available),
      }))
      .sort((a, b) => Number(b.usable) - Number(a.usable) || a.provider.localeCompare(b.provider))
  }, [models])

  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const isOpen = (group: { provider: string; usable: boolean }) =>
    collapsed[group.provider] === undefined ? group.usable : !collapsed[group.provider]

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="In use"
          subtitle="What the agents run on, and what the guardrails judge with."
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectionRow
            label="Agents"
            hint="Every agent uses this unless its own spec names a model."
            value={data.selection.default_model ?? ''}
            models={data.models.filter((m) => !m.is_embedding)}
            onChange={(value) => select.mutate({ default_model: value })}
            pending={select.isPending}
          />
          <SelectionRow
            label="Guardrails"
            hint="Leave as follow-agents unless you want rails on something smaller."
            value={data.selection.guardrails_model ?? ''}
            models={data.models.filter((m) => !m.is_embedding)}
            emptyLabel={`Follow agents (${data.selection.effective_guardrails_model ?? 'router'})`}
            onChange={(value) => select.mutate({ guardrails_model: value })}
            pending={select.isPending}
          />
        </div>
        {saveError ? (
          <p className="mt-3 rounded-md border border-state-err/30 bg-state-err/10 px-3 py-2 text-xs text-state-err">
            Could not save: {saveError}
          </p>
        ) : null}
        {!data.selection.aligned ? (
          <p className="mt-3 text-xs text-state-warn">
            Guardrails run on {data.selection.effective_guardrails_model} while agents run on{' '}
            {data.selection.default_model}. That is a valid choice — rails classify rather than write —
            but both models need a working credential.
          </p>
        ) : null}
      </Card>

      <Card>
        <CardHeader
          title="Catalogue"
          subtitle="Priced models this platform knows how to call. Test one before selecting it."
          action={
            <div className="flex items-center gap-2">
              {refusedCount ? (
                <Button size="sm" variant="ghost" onClick={() => setShowRefused((v) => !v)}>
                  {showRefused ? 'Hide' : 'Show'} {refusedCount} refused
                </Button>
              ) : null}
              <input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder={`Filter ${models.length} models...`}
                className="input w-56 text-xs"
              />
            </div>
          }
        />
        <div className="space-y-2">
          {groups.map((group) => {
            const open = isOpen(group)
            return (
              <div key={group.provider} className="overflow-hidden rounded-lg border border-line/70">
                <button
                  type="button"
                  onClick={() =>
                    setCollapsed((previous) => ({ ...previous, [group.provider]: open }))
                  }
                  className="flex w-full items-center gap-2 bg-surface-muted/50 px-3 py-2 text-left hover:bg-surface-muted"
                  aria-expanded={open}
                >
                  <span className={cn('text-ink-subtle transition-transform', open && 'rotate-90')}>
                    ›
                  </span>
                  <span className="text-sm font-medium text-ink">{group.provider}</span>
                  <Badge tone={group.usable ? 'ok' : 'idle'}>
                    {group.usable ? 'key set' : 'no key'}
                  </Badge>
                  <span className="text-2xs text-ink-subtle">
                    {group.rows.length} model{group.rows.length === 1 ? '' : 's'}
                  </span>
                  {group.rows.some((r) => r.id === data.selection.default_model) ? (
                    <Badge tone="info">in use</Badge>
                  ) : null}
                </button>

                {open ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-line/70 text-left">
                          {['Model', 'Tier', 'Context', '$ / 1M in', '$ / 1M out', 'Credential', ''].map(
                            (column, index) => (
                              <th
                                key={column || index}
                                className={cn(
                                  'metric-label px-3 py-2 font-medium',
                                  index > 1 && 'text-right',
                                )}
                              >
                                {column}
                              </th>
                            ),
                          )}
                        </tr>
                      </thead>
                      <tbody>
                        {group.rows.map((model) => {
                          const result = results[model.id]
                          const isDefault = data.selection.default_model === model.id
                          return (
                            <tr
                              key={model.id}
                              className="table-row border-b border-line/40 last:border-0"
                            >
                              <td className="px-3 py-2.5">
                                <div className="flex items-center gap-2">
                                  <span className="font-medium text-ink">{model.display_name}</span>
                                  {isDefault ? <Badge tone="ok">in use</Badge> : null}
                                </div>
                                <code className="text-2xs text-ink-subtle">{model.id}</code>
                                {result ? (
                                  <p
                                    className={cn(
                                      'mt-1 text-2xs',
                                      result.ok ? 'text-state-ok' : 'text-state-err',
                                    )}
                                  >
                                    {result.ok
                                      ? `replied in ${result.latency_ms}ms${
                                          result.served_by && result.served_by !== model.id
                                            ? ` — served by ${result.served_by}`
                                            : ''
                                        }`
                                      : result.error}
                                  </p>
                                ) : null}
                              </td>
                              <td className="px-3 py-2.5 text-ink-muted">{model.tier}</td>
                              <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                                {(model.context_window / 1000).toFixed(0)}k
                              </td>
                              <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                                {money(model.input_price_per_mtok)}
                              </td>
                              <td className="px-3 py-2.5 text-right tabular-nums text-ink-muted">
                                {money(model.output_price_per_mtok)}
                              </td>
                              <td className="px-3 py-2.5 text-right">
                                <CredentialBadge model={model} />
                              </td>
                              <td className="px-3 py-2.5 text-right">
                                <div className="flex justify-end gap-1.5">
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    loading={testing === model.id}
                                    onClick={() => void test(model.id)}
                                  >
                                    Test
                                  </Button>
                                  <Button
                                    size="sm"
                                    variant={isDefault ? 'outline' : 'primary'}
                                    disabled={isDefault || select.isPending}
                                    onClick={() => select.mutate({ default_model: model.id })}
                                  >
                                    {isDefault ? 'Selected' : 'Use'}
                                  </Button>
                                </div>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      </Card>
    </div>
  )
}

function CredentialBadge({ model }: { model: CatalogueModel }) {
  // Three distinct situations with three different remedies. "Unavailable" for all of them
  // would leave an operator guessing which of nine keys to go and set.
  if (model.rejected_by_provider) return <Badge tone="err">no access</Badge>
  if (model.is_local_runtime) return <Badge tone="info">local</Badge>
  if (model.credential_available) return <Badge tone="ok">ready</Badge>
  return <Badge tone="idle">{model.credential_env_var ?? 'no key'}</Badge>
}

function SelectionRow({
  label,
  hint,
  value,
  models,
  onChange,
  pending,
  emptyLabel = 'Router chooses',
}: {
  label: string
  hint: string
  value: string
  models: CatalogueModel[]
  onChange: (value: string) => void
  pending: boolean
  emptyLabel?: string
}) {
  return (
    <div className="rounded-lg border border-line/70 bg-surface-muted/40 p-3">
      <p className="metric-label mb-1">{label}</p>
      <select
        value={value}
        disabled={pending}
        onChange={(event) => onChange(event.target.value)}
        className="input w-full text-sm"
      >
        <option value="">{emptyLabel}</option>
        {models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.display_name} ({model.id})
          </option>
        ))}
      </select>
      <p className="mt-1.5 text-2xs text-ink-subtle">{hint}</p>
    </div>
  )
}

/* ----------------------------------------------------------------- available */

function Available({ active, onChanged }: { active: boolean; onChanged: () => void }) {
  const [results, setResults] = useState<Record<string, TestResult>>({})
  const [busy, setBusy] = useState<string | null>(null)

  const test = async (id: string, provider: string) => {
    setBusy(id)
    try {
      const result = await api.post<TestResult>('/models/test', { model_id: id, provider })
      setResults((previous) => ({ ...previous, [id]: result }))
    } catch (error) {
      setResults((previous) => ({ ...previous, [id]: { ok: false, error: String(error) } }))
    } finally {
      setBusy(null)
    }
  }

  const use = async (id: string) => {
    setBusy(id)
    try {
      await api.put('/models/selection', { default_model: id })
      onChanged()
    } catch (error) {
      setResults((previous) => ({ ...previous, [id]: { ok: false, error: String(error) } }))
    } finally {
      setBusy(null)
    }
  }

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['models', 'available'],
    queryFn: () => api.get<LiveModels>('/models/available'),
    enabled: active, // a network round trip per provider; do not pay for it on other tabs
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />
  if (isLoading) return <SkeletonCard rows={5} />
  if (!data?.providers.length) {
    return <EmptyState title="No provider configured" description="Set a key on the API keys tab." />
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Available models"
          subtitle={data.note}
          action={
            <Button size="sm" variant="ghost" loading={isFetching} onClick={() => void refetch()}>
              Refresh
            </Button>
          }
        />
        <div className="space-y-4">
          {data.providers.map((provider) => (
            <div key={provider.provider}>
              <div className="mb-2 flex items-center gap-2">
                <span className="text-sm font-medium text-ink">{provider.provider}</span>
                <Badge tone={provider.status === 'ok' ? 'ok' : 'idle'}>
                  {provider.status === 'ok' ? `${provider.models.length} models` : provider.status}
                </Badge>
                {provider.latency_ms ? (
                  <span className="text-2xs text-ink-subtle">{provider.latency_ms}ms</span>
                ) : null}
              </div>
              {provider.error ? (
                <p className="text-xs text-state-err">{provider.error}</p>
              ) : (
                <div className="space-y-1">
                  {provider.models.map((model) => {
                    const result = results[model.id]
                    return (
                      <div
                        key={model.id}
                        className="flex flex-wrap items-center gap-2 rounded-md border border-line/60 px-2 py-1.5"
                      >
                        <code className="font-mono text-2xs text-ink">{model.id}</code>
                        {model.in_catalogue ? (
                          <Badge tone="ok">priced</Badge>
                        ) : (
                          <Badge tone="idle">unpriced</Badge>
                        )}
                        {result ? (
                          <span
                            className={cn('text-2xs', result.ok ? 'text-state-ok' : 'text-state-err')}
                          >
                            {result.ok ? `replied in ${result.latency_ms}ms` : result.error}
                          </span>
                        ) : null}
                        <div className="ml-auto flex gap-1.5">
                          <Button
                            size="sm"
                            variant="ghost"
                            loading={busy === model.id}
                            onClick={() => void test(model.id, provider.provider)}
                          >
                            Test
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy === model.id}
                            onClick={() => void use(model.id)}
                          >
                            Use
                          </Button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

/* ---------------------------------------------------------------------- keys */

function Keys({ rows, onChanged }: { rows: KeyRow[]; onChanged: () => void }) {
  const [editing, setEditing] = useState<string | null>(null)
  const [value, setValue] = useState('')
  const [results, setResults] = useState<Record<string, TestResult>>({})
  const [testing, setTesting] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: ({ provider, key }: { provider: string; key: string }) =>
      api.put(`/models/keys/${provider}`, { value: key }),
    onSuccess: () => {
      setEditing(null)
      setValue('')
      onChanged()
    },
  })
  const clear = useMutation({
    mutationFn: (provider: string) => api.del(`/models/keys/${provider}`),
    onSuccess: onChanged,
  })

  const test = async (provider: string) => {
    setTesting(provider)
    try {
      const result = await api.post<TestResult>(`/models/keys/${provider}/test`)
      setResults((p) => ({ ...p, [provider]: result }))
    } catch (error) {
      setResults((p) => ({ ...p, [provider]: { ok: false, error: String(error) } }))
    } finally {
      setTesting(null)
    }
  }

  return (
    <Card>
      <CardHeader
        title="Provider API keys"
        subtitle="Stored encrypted and applied to the next model call — no restart. Keys already in .env keep working."
      />
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.provider} className="rounded-lg border border-line/70 bg-surface-muted/40 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-ink">{row.provider}</span>
              <Badge tone={row.set ? 'ok' : 'idle'}>{row.set ? 'set' : 'not set'}</Badge>
              {row.source === 'stored' ? <Badge tone="info">stored here</Badge> : null}
              {row.source === 'environment' ? <Badge tone="info">from {row.env_var}</Badge> : null}
              <span className="text-2xs text-ink-subtle">
                {row.catalogued_models} catalogued model{row.catalogued_models === 1 ? '' : 's'}
              </span>
              <div className="ml-auto flex gap-1.5">
                {row.set ? (
                  <Button size="sm" variant="ghost" loading={testing === row.provider} onClick={() => void test(row.provider)}>
                    Test
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setEditing(editing === row.provider ? null : row.provider)
                    setValue('')
                  }}
                >
                  {row.set ? 'Replace' : 'Add key'}
                </Button>
                {row.source === 'stored' ? (
                  <Button size="sm" variant="danger" onClick={() => clear.mutate(row.provider)}>
                    Remove
                  </Button>
                ) : null}
              </div>
            </div>

            {editing === row.provider ? (
              <form
                className="mt-2 flex gap-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  if (value.trim()) save.mutate({ provider: row.provider, key: value.trim() })
                }}
              >
                <input
                  autoFocus
                  type="password"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder={`${row.env_var ?? 'API key'} value`}
                  className="input flex-1 font-mono text-xs"
                />
                <Button type="submit" size="sm" variant="primary" loading={save.isPending} disabled={!value.trim()}>
                  Save
                </Button>
              </form>
            ) : null}

            {results[row.provider] ? (
              <p className={cn('mt-2 text-xs', results[row.provider].ok ? 'text-state-ok' : 'text-state-err')}>
                {results[row.provider].ok
                  ? `key is valid — ${results[row.provider].reachable_models} models reachable (does not check remaining quota)`
                  : results[row.provider].error}
              </p>
            ) : null}
          </div>
        ))}
      </div>
      <p className="mt-3 text-2xs text-ink-subtle">
        A key saved here takes precedence over the matching environment variable. The value is never sent
        back to the browser — only whether one is present and where it came from.
      </p>
    </Card>
  )
}
