import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import { api } from '@/lib/api'
import { Badge, Button, Card, CardHeader, EmptyState, PageHeader } from '@/components/ui'
import { useToasts, useUi } from '@/store'
import { formatCurrency, formatDuration, formatNumber } from '@/lib/utils'

interface ModelInfo {
  id: string
  display_name: string
  provider: string
  tier: string
  context_window: number
  input_price_per_mtok: number
  output_price_per_mtok: number
  supports_tools: boolean
  supports_vision: boolean
  is_embedding: boolean
  available: boolean
}

interface RunResult {
  model: string
  ok: boolean
  content?: string
  error?: string
  provider?: string
  latency_ms: number
  tokens?: { input: number; output: number }
  cost_usd?: number
  finish_reason?: string
  characters?: number
}

export default function Playground() {
  const push = useToasts((state) => state.push)
  const theme = useUi((state) => state.theme)
  const [prompt, setPrompt] = useState('Summarise our SAR filing obligations in three bullet points.')
  const [systemPrompt, setSystemPrompt] = useState('You are a precise financial-services compliance assistant.')
  const [selected, setSelected] = useState<string[]>([])
  const [temperature, setTemperature] = useState(0.2)
  const [maxTokens, setMaxTokens] = useState(800)
  const [results, setResults] = useState<RunResult[] | null>(null)

  const models = useQuery({
    queryKey: ['playground-models'],
    queryFn: () => api.get<{ configured_providers: string[]; models: ModelInfo[] }>('/playground/models'),
  })

  const run = useMutation({
    mutationFn: () =>
      api.post<{ results: RunResult[]; total_cost_usd: number; comparison: Record<string, string> }>(
        '/playground/run',
        {
          prompt,
          system_prompt: systemPrompt || undefined,
          models: selected,
          temperature,
          max_tokens: maxTokens,
        },
      ),
    onSuccess: (response) => setResults(response.results),
    onError: (error) => push({ title: 'Playground run failed', description: (error as Error).message, tone: 'err' }),
  })

  const available = (models.data?.models ?? []).filter((model) => !model.is_embedding)

  return (
    <div className="space-y-5">
      <PageHeader
        title="Prompt playground"
        description="Run the same prompt across models and compare response, latency, tokens and cost."
        actions={
          <Button
            variant="primary"
            loading={run.isPending}
            disabled={selected.length === 0 || !prompt.trim()}
            onClick={() => run.mutate()}
          >
            Run comparison
          </Button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card className="overflow-hidden">
          <CardHeader title="Prompt" subtitle="System instruction and user message" />
          <div className="space-y-3 px-5 pb-5 pt-3">
            <div>
              <label htmlFor="system" className="metric-label mb-1.5 block">
                System prompt
              </label>
              <textarea
                id="system"
                rows={2}
                className="input"
                value={systemPrompt}
                onChange={(event) => setSystemPrompt(event.target.value)}
              />
            </div>
            <div>
              <p className="metric-label mb-1.5">User prompt</p>
              <div className="overflow-hidden rounded-xl border border-line">
                <Editor
                  height="220px"
                  defaultLanguage="markdown"
                  value={prompt}
                  onChange={(value) => setPrompt(value ?? '')}
                  theme={theme === 'dark' ? 'vs-dark' : 'light'}
                  options={{
                    minimap: { enabled: false },
                    fontSize: 12,
                    lineNumbers: 'off',
                    scrollBeyondLastLine: false,
                    wordWrap: 'on',
                    padding: { top: 12, bottom: 12 },
                  }}
                />
              </div>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Models"
            subtitle={
              models.data?.configured_providers.length
                ? `Configured: ${models.data.configured_providers.join(', ')}`
                : 'No provider configured on this deployment'
            }
          />
          <div className="max-h-64 space-y-1.5 overflow-y-auto px-5 py-3">
            {available.map((model) => (
              <label
                key={model.id}
                className={`flex cursor-pointer items-center gap-2.5 rounded-xl border px-3 py-2 transition-colors ${
                  selected.includes(model.id) ? 'border-ink/25 bg-accent-soft' : 'border-line/70'
                } ${model.available ? '' : 'opacity-50'}`}
              >
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 rounded border-line"
                  checked={selected.includes(model.id)}
                  disabled={!model.available}
                  onChange={(event) =>
                    setSelected((current) =>
                      event.target.checked ? [...current, model.id] : current.filter((id) => id !== model.id),
                    )
                  }
                />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs font-medium">{model.display_name}</p>
                  <p className="text-2xs text-ink-subtle">
                    {model.provider} · ${model.input_price_per_mtok}/${model.output_price_per_mtok} per Mtok
                  </p>
                </div>
                {!model.available ? <Badge tone="idle">no key</Badge> : null}
              </label>
            ))}
          </div>
          <div className="space-y-3 border-t border-line/60 px-5 py-4">
            <div>
              <div className="flex justify-between text-2xs text-ink-muted">
                <span>Temperature</span>
                <span className="tabular-nums">{temperature.toFixed(2)}</span>
              </div>
              <input
                type="range"
                min={0}
                max={2}
                step={0.05}
                value={temperature}
                onChange={(event) => setTemperature(Number(event.target.value))}
                className="mt-1.5 w-full accent-current"
              />
            </div>
            <div>
              <div className="flex justify-between text-2xs text-ink-muted">
                <span>Max tokens</span>
                <span className="tabular-nums">{maxTokens}</span>
              </div>
              <input
                type="range"
                min={64}
                max={8000}
                step={64}
                value={maxTokens}
                onChange={(event) => setMaxTokens(Number(event.target.value))}
                className="mt-1.5 w-full accent-current"
              />
            </div>
          </div>
        </Card>
      </div>

      {results ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {results.map((result) => (
            <Card key={result.model} className="flex flex-col">
              <div className="flex items-start justify-between gap-2 px-5 pt-5">
                <p className="truncate font-mono text-xs font-medium">{result.model}</p>
                <Badge tone={result.ok ? 'ok' : 'err'}>{result.ok ? 'ok' : 'failed'}</Badge>
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5 px-5">
                <span className="chip">{formatDuration(result.latency_ms)}</span>
                {result.tokens ? (
                  <span className="chip">
                    {formatNumber(result.tokens.input)}/{formatNumber(result.tokens.output)} tok
                  </span>
                ) : null}
                {result.cost_usd !== undefined ? <span className="chip">{formatCurrency(result.cost_usd, 5)}</span> : null}
              </div>
              <div className="mt-3 flex-1 px-5 pb-5">
                {result.ok ? (
                  <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-muted">{result.content}</p>
                ) : (
                  <p className="text-xs text-state-err">{result.error}</p>
                )}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card>
          <EmptyState
            title="No comparison yet"
            description="Select one or more configured models and run the prompt to compare responses side by side."
          />
        </Card>
      )}
    </div>
  )
}
