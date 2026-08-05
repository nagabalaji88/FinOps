import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { api, type Agent } from '@/lib/api'
import { Button, Modal } from '@/components/ui'
import { useToasts } from '@/store'
import { titleCase } from '@/lib/utils'

/** Builds the input form from the agent's declared input schema and submits a real run. */
export function ExecuteDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const navigate = useNavigate()
  const push = useToasts((state) => state.push)
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const [field, value] of Object.entries(agent.example_input ?? {})) {
      initial[field] = typeof value === 'string' ? value : JSON.stringify(value)
    }
    return initial
  })

  const schema = agent.input_schema ?? {}
  const fields = Object.entries(schema)

  const execute = useMutation({
    mutationFn: async () => {
      const payload: Record<string, unknown> = {}
      for (const [field, definition] of fields) {
        const raw = values[field]
        if (raw === undefined || raw === '') continue
        payload[field] = definition.type === 'number' ? Number(raw) : raw
      }
      return api.post<{ execution_id: string }>(`/agents/${agent.key}/execute`, { input: payload })
    },
    onSuccess: (response) => {
      push({ title: 'Execution started', description: agent.name, tone: 'ok' })
      onClose()
      navigate(`/executions/${response.execution_id}`)
    },
    onError: (error) =>
      push({ title: 'Execution rejected', description: (error as Error).message, tone: 'err' }),
  })

  const missingRequired = fields
    .filter(([field, definition]) => definition.required && !values[field])
    .map(([field]) => field)

  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title={`Execute ${agent.name}`}
      description="The run starts immediately and streams its trace live."
      size="lg"
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault()
          execute.mutate()
        }}
      >
        {fields.length === 0 ? (
          <p className="text-xs text-ink-muted">This agent takes no structured input.</p>
        ) : (
          fields.map(([field, definition]) => (
            <div key={field} className="space-y-1.5">
              <label htmlFor={`field-${field}`} className="metric-label">
                {definition.label ?? titleCase(field)}
                {definition.required ? <span className="ml-1 text-state-err">*</span> : null}
              </label>
              {field === 'query' || field === 'question' || field === 'message' ? (
                <textarea
                  id={`field-${field}`}
                  rows={4}
                  className="input resize-y"
                  value={values[field] ?? ''}
                  onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))}
                  placeholder="Describe what the agent should do…"
                />
              ) : (
                <input
                  id={`field-${field}`}
                  type={definition.type === 'password' ? 'password' : definition.type === 'number' ? 'number' : 'text'}
                  className="input"
                  value={values[field] ?? ''}
                  onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))}
                />
              )}
            </div>
          ))
        )}

        <div className="rounded-xl border border-line/70 bg-surface-muted/50 px-3 py-2.5 text-2xs text-ink-muted">
          <p>
            Model <span className="font-mono text-ink">{agent.model ?? 'router default'}</span> ·{' '}
            {agent.tools.length} tools available
            {agent.requires_approval ? ' · human approval required before the final action' : ''}
          </p>
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={execute.isPending}
            disabled={missingRequired.length > 0}
          >
            Run agent
          </Button>
        </div>
      </form>
    </Modal>
  )
}
