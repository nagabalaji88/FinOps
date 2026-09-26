import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type ToolDefinition, Badge, Button, Card, CardHeader, PageHeader, useToasts } from '@finops/shared'

export default function Builder() {
  const navigate = useNavigate()
  const push = useToasts((state) => state.push)
  const [form, setForm] = useState({
    key: '',
    name: '',
    description: '',
    category: 'Custom',
    system_prompt:
      'You are an enterprise agent for a regulated bank. Use tools for every factual claim and never invent data.',
    model: '',
    temperature: 0.2,
    memory_enabled: false,
    final_approval_required: false,
    cost_cap_usd: 2,
  })
  const [tools, setTools] = useState<string[]>([])
  const [sources, setSources] = useState<string[]>([])

  const toolList = useQuery({ queryKey: ['tools'], queryFn: () => api.get<ToolDefinition[]>('/tools') })
  const sourceList = useQuery({
    queryKey: ['knowledge-sources'],
    queryFn: () => api.get<{ key: string; name: string }[]>('/knowledge/sources'),
  })

  const create = useMutation({
    mutationFn: () =>
      api.post<{ key: string }>('/agents', {
        ...form,
        model: form.model || undefined,
        tools,
        knowledge_sources: sources,
        input_schema: { query: { type: 'string', required: true, label: 'Request' } },
      }),
    onSuccess: (response) => {
      push({ title: 'Agent created', description: response.key, tone: 'ok' })
      navigate(`/agents/${response.key}`)
    },
    onError: (error) => push({ title: 'Creation failed', description: (error as Error).message, tone: 'err' }),
  })

  const valid = /^[a-z][a-z0-9_]{2,63}$/.test(form.key) && form.name.length > 2 && form.system_prompt.length > 20

  return (
    <div className="space-y-5">
      <PageHeader
        title="Agent builder"
        description="Compose a new agent from the shared execution graph, tool registry and knowledge corpus."
        actions={
          <Button variant="primary" disabled={!valid} loading={create.isPending} onClick={() => create.mutate()}>
            Create and publish
          </Button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[1.3fr_1fr]">
        <Card>
          <CardHeader title="Identity" subtitle="How the agent appears in the catalogue" />
          <div className="grid gap-4 px-5 pb-5 pt-3 sm:grid-cols-2">
            <div>
              <label htmlFor="key" className="metric-label mb-1.5 block">
                Key (lowercase, underscore)
              </label>
              <input
                id="key"
                className="input font-mono"
                value={form.key}
                onChange={(event) => setForm({ ...form, key: event.target.value })}
                placeholder="treasury_liquidity"
              />
            </div>
            <div>
              <label htmlFor="name" className="metric-label mb-1.5 block">
                Display name
              </label>
              <input
                id="name"
                className="input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="description" className="metric-label mb-1.5 block">
                Description
              </label>
              <input
                id="description"
                className="input"
                value={form.description}
                onChange={(event) => setForm({ ...form, description: event.target.value })}
              />
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="system" className="metric-label mb-1.5 block">
                System prompt
              </label>
              <textarea
                id="system"
                rows={10}
                className="input font-mono text-2xs"
                value={form.system_prompt}
                onChange={(event) => setForm({ ...form, system_prompt: event.target.value })}
              />
            </div>
            <div>
              <label htmlFor="model" className="metric-label mb-1.5 block">
                Model (optional)
              </label>
              <input
                id="model"
                className="input font-mono"
                value={form.model}
                onChange={(event) => setForm({ ...form, model: event.target.value })}
                placeholder="router default"
              />
            </div>
            <div>
              <label htmlFor="temp" className="metric-label mb-1.5 block">
                Temperature
              </label>
              <input
                id="temp"
                type="number"
                step={0.05}
                min={0}
                max={2}
                className="input"
                value={form.temperature}
                onChange={(event) => setForm({ ...form, temperature: Number(event.target.value) })}
              />
            </div>
            <div className="sm:col-span-2 flex flex-wrap gap-4">
              <label className="flex items-center gap-2 text-xs text-ink-muted">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 rounded border-line"
                  checked={form.memory_enabled}
                  onChange={(event) => setForm({ ...form, memory_enabled: event.target.checked })}
                />
                Conversation memory
              </label>
              <label className="flex items-center gap-2 text-xs text-ink-muted">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 rounded border-line"
                  checked={form.final_approval_required}
                  onChange={(event) => setForm({ ...form, final_approval_required: event.target.checked })}
                />
                Require human approval
              </label>
            </div>
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader title="Tools" subtitle={`${tools.length} selected`} />
            <div className="max-h-72 space-y-1.5 overflow-y-auto px-5 py-3">
              {toolList.data?.map((tool) => (
                <label
                  key={tool.name}
                  className={`flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2 ${
                    tools.includes(tool.name) ? 'border-ink/25 bg-accent-soft' : 'border-line/70'
                  }`}
                >
                  <input
                    type="checkbox"
                    className="mt-0.5 h-3.5 w-3.5 rounded border-line"
                    checked={tools.includes(tool.name)}
                    onChange={(event) =>
                      setTools((current) =>
                        event.target.checked ? [...current, tool.name] : current.filter((name) => name !== tool.name),
                      )
                    }
                  />
                  <div className="min-w-0">
                    <p className="font-mono text-2xs">{tool.name}</p>
                    <p className="line-clamp-1 text-2xs text-ink-subtle">{tool.description}</p>
                  </div>
                  {tool.requires_approval ? <Badge tone="warn">HITL</Badge> : null}
                </label>
              ))}
            </div>
          </Card>

          <Card>
            <CardHeader title="Knowledge sources" subtitle={`${sources.length} selected`} />
            <div className="max-h-52 space-y-1.5 overflow-y-auto px-5 py-3">
              {sourceList.data?.map((source) => (
                <label
                  key={source.key}
                  className={`flex cursor-pointer items-center gap-2.5 rounded-xl border px-3 py-2 ${
                    sources.includes(source.key) ? 'border-ink/25 bg-accent-soft' : 'border-line/70'
                  }`}
                >
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 rounded border-line"
                    checked={sources.includes(source.key)}
                    onChange={(event) =>
                      setSources((current) =>
                        event.target.checked ? [...current, source.key] : current.filter((key) => key !== source.key),
                      )
                    }
                  />
                  <span className="text-xs">{source.name}</span>
                </label>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
