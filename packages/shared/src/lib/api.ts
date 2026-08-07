/**
 * Typed API client.
 *
 * Handles bearer auth, transparent refresh on 401, RFC-7807 style error unwrapping and
 * Server-Sent Event subscriptions. Every response shape here mirrors the FastAPI schema.
 */

const BASE = import.meta.env.VITE_API_BASE_URL ?? ''
const API = `${BASE}/api/v1`

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string = 'error',
    readonly details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type Tokens = { access: string | null; refresh: string | null }

const STORAGE_KEY = 'finops.tokens'

export const tokenStore = {
  read(): Tokens {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      return raw ? (JSON.parse(raw) as Tokens) : { access: null, refresh: null }
    } catch {
      return { access: null, refresh: null }
    }
  },
  write(tokens: Tokens) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
  },
  clear() {
    localStorage.removeItem(STORAGE_KEY)
  },
}

let refreshInFlight: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  const { refresh } = tokenStore.read()
  if (!refresh) return null
  if (!refreshInFlight) {
    refreshInFlight = fetch(`${API}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    })
      .then(async (response) => {
        if (!response.ok) {
          tokenStore.clear()
          return null
        }
        const body = await response.json()
        tokenStore.write({ access: body.access_token, refresh: body.refresh_token })
        return body.access_token as string
      })
      .catch(() => null)
      .finally(() => {
        refreshInFlight = null
      })
  }
  return refreshInFlight
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  raw?: boolean
  skipAuth?: boolean
}

export async function request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, raw, skipAuth, headers, ...rest } = options
  const url = new URL(`${API}${path}`, window.location.origin)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value))
    }
  }

  const send = async (token: string | null): Promise<Response> => {
    const finalHeaders = new Headers(headers)
    if (!(body instanceof FormData) && body !== undefined) {
      finalHeaders.set('Content-Type', 'application/json')
    }
    if (token && !skipAuth) finalHeaders.set('Authorization', `Bearer ${token}`)
    return fetch(url.toString(), {
      ...rest,
      headers: finalHeaders,
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
    })
  }

  let response = await send(tokenStore.read().access)
  if (response.status === 401 && !skipAuth) {
    const refreshed = await refreshAccessToken()
    if (refreshed) response = await send(refreshed)
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let code = 'error'
    let details: Record<string, unknown> = {}
    try {
      const payload = await response.json()
      const error = payload?.error ?? payload?.detail ?? payload
      message = error?.message ?? error?.detail ?? message
      code = error?.code ?? code
      details = error?.details ?? {}
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(message, response.status, code, details)
  }

  if (raw) return response as unknown as T
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string, query?: RequestOptions['query']) => request<T>(path, { query }),
  post: <T>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'POST', body, query }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
}

/**
 * Split a Server-Sent Events buffer into complete execution events.
 *
 * Line endings are CRLF or LF depending on the server; frames arrive split across TCP
 * reads, so whatever follows the last blank line is returned as `rest` to be prepended to
 * the next chunk. Keep-alives — both comment pings and heartbeat frames — carry no event
 * type and are dropped.
 */
export function parseSseFrames(buffer: string): { events: ExecutionEvent[]; rest: string } {
  const frames = buffer.split(/\r?\n\r?\n/)
  const rest = frames.pop() ?? ''
  const events: ExecutionEvent[] = []
  for (const frame of frames) {
    const dataLine = frame.split(/\r?\n/).find((line) => line.startsWith('data:'))
    if (!dataLine) continue
    try {
      const payload = JSON.parse(dataLine.slice(5).trim())
      if (payload && typeof payload.type === 'string' && payload.type !== 'heartbeat') {
        events.push(payload as ExecutionEvent)
      }
    } catch {
      /* malformed frame — the stream continues */
    }
  }
  return { events, rest }
}

/**
 * Subscribe to an execution's SSE stream. EventSource cannot set headers, so the request
 * is made with `fetch` and the response body is parsed as it arrives.
 */
export function subscribeToExecution(
  executionId: string,
  handlers: {
    onEvent: (event: ExecutionEvent) => void
    onError?: (error: Event) => void
    onOpen?: () => void
  },
  afterSequence = 0,
): () => void {
  const controller = new AbortController()

  const run = async () => {
    const { access } = tokenStore.read()
    const url = new URL(`${API}/executions/${executionId}/stream`, window.location.origin)
    url.searchParams.set('after', String(afterSequence))
    try {
      const response = await fetch(url.toString(), {
        headers: access ? { Authorization: `Bearer ${access}` } : {},
        signal: controller.signal,
      })
      if (!response.ok || !response.body) {
        handlers.onError?.(new Event('error'))
        return
      }
      handlers.onOpen?.()
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const { events, rest } = parseSseFrames(buffer)
        buffer = rest
        for (const event of events) handlers.onEvent(event)
      }
    } catch (error) {
      if ((error as Error).name !== 'AbortError') handlers.onError?.(new Event('error'))
    }
  }

  void run()
  return () => controller.abort()
}

/* ------------------------------------------------------------------ types */

export interface Principal {
  id: string
  email: string
  full_name: string
  roles: string[]
  department: string
  permissions: string[]
  auth_method: string
  mfa_enabled: boolean
}

export interface AgentMetrics {
  executions_total: number
  executions_24h: number
  succeeded_24h: number
  failed_24h: number
  running: number
  awaiting_approval: number
  avg_latency_ms: number
  cost_today_usd: number
  retry_count_24h: number
  error_rate_pct: number
  success_rate_pct: number
  open_incidents: number
  pending_approvals: number
  health: 'healthy' | 'degraded' | 'unhealthy'
  current_execution: { id: string; status: string; started_at: string | null } | null
  last_execution: {
    id: string
    status: string
    latency_ms: number | null
    cost_usd: number
    finished_at: string | null
  } | null
}

export interface Agent {
  id: string
  key: string
  name: string
  description: string
  category: string
  availability: 'implemented' | 'coming_soon'
  lifecycle_state: 'active' | 'paused' | 'disabled'
  owner: string
  owner_email: string | null
  department: string
  version: number
  is_builtin: boolean
  tags: string[]
  tools: string[]
  knowledge_sources: string[]
  model: string | null
  temperature: number | null
  memory_enabled: boolean | null
  requires_approval: boolean | null
  sla_latency_ms: number
  monthly_budget_usd: number
  input_schema: Record<string, { type: string; required?: boolean; label?: string }>
  example_input: Record<string, unknown>
  planned_quarter?: string
  metrics: AgentMetrics
  config?: Record<string, unknown>
  tool_details?: ToolDefinition[]
}

export interface ToolDefinition {
  name: string
  description: string
  category: string
  requires_approval: boolean
  approval_risk?: string
  writes_data: boolean
  idempotent?: boolean
  timeout_seconds: number
  max_retries?: number
  schema: Record<string, unknown>
  health?: {
    status: string
    total_calls: number
    failures: number
    timeouts: number
    retries: number
    p50_latency_ms: number
    p95_latency_ms: number
    success_rate_pct: number
    last_called_at: string | null
    last_error: string | null
  }
}

export interface Execution {
  id: string
  agent_key: string
  agent_version: number
  status: string
  trigger: string
  trace_id: string
  correlation_id: string
  thread_id: string | null
  user_email: string | null
  department: string | null
  model: string | null
  provider: string | null
  tokens: { input: number; output: number; cached: number; embedding: number; total: number }
  cost_usd: number
  latency_ms: number | null
  queue_ms: number | null
  retry_count: number
  tool_call_count: number
  llm_call_count: number
  approval_count: number
  node_path: string[]
  error: string | null
  error_type: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  input?: Record<string, unknown>
  output?: Record<string, unknown>
  final_response?: string | null
  plan?: Record<string, unknown> | null
  reasoning?: string | null
  artifacts?: Record<string, unknown>[]
}

export interface Span {
  span_id: string
  parent_span_id: string | null
  trace_id: string
  name: string
  kind: string
  status: 'running' | 'ok' | 'error' | 'skipped'
  start_time: string
  end_time: string | null
  duration_ms: number
  offset_ms: number
  offset_pct: number
  width_pct: number
  attributes: Record<string, unknown>
  events: Record<string, unknown>[]
  input: Record<string, unknown> | null
  output: Record<string, unknown> | null
  error: string | null
  retry_count: number
  tokens: { input: number; output: number }
  cost_usd: number
  request_id: string | null
}

export interface Trace {
  trace_id: string
  execution_id: string
  agent_key: string
  root_span_id: string | null
  start_time: string
  end_time: string
  total_duration_ms: number
  span_count: number
  error_count: number
  total_cost_usd: number
  spans: Span[]
}

export interface ExecutionEvent {
  sequence: number
  type: string
  node: string | null
  span_id: string | null
  timestamp: string
  payload: Record<string, any>
}

export interface GraphNode {
  id: string
  label: string
  kind: string
  description: string
  state: 'executed' | 'skipped' | 'error' | 'active'
  span_count: number
  duration_ms: number
  cost_usd: number
  tokens: number
  retries: number
  spans: { span_id: string; name: string; status: string; duration_ms: number; error: string | null }[]
}

export interface ExecutionGraph {
  execution_id: string
  status: string
  nodes: GraphNode[]
  edges: { source: string; target: string; label?: string }[]
}

export interface Approval {
  id: string
  execution_id: string
  agent_key: string
  node: string
  title: string
  summary: string
  payload: Record<string, any>
  risk_level: 'low' | 'medium' | 'high' | 'critical'
  required_role: string
  status: string
  requested_by: string | null
  reviewer_email: string | null
  comments: string | null
  timeline: { at: string; event: string; by?: string; comments?: string | null }[]
  created_at: string
  decided_at: string | null
  expires_at: string | null
  expired: boolean
  execution?: Record<string, unknown> | null
}

export interface Overview {
  generated_at: string
  agents: Record<string, number>
  executions: Record<string, number>
  tokens: Record<string, number>
  cost: Record<string, number>
  approvals: Record<string, number>
  errors_today: number
  resources: {
    cpu_percent: number
    cpu_count: number
    memory: { total_bytes: number; used_bytes: number; used_pct: number }
    network: { rx_bytes: number; tx_bytes: number }
    gpu: { index: number; name: string; utilisation_pct: number; memory_used_mb: number }[]
    queue_depth: number
    in_flight_executions: number
    concurrency_limit: number
  }
}

export interface LogRecord {
  id: string
  timestamp: string
  level: string
  logger: string
  message: string
  agent_key: string | null
  execution_id: string | null
  correlation_id: string | null
  trace_id: string | null
  span_id: string | null
  user_email: string | null
  attributes: Record<string, unknown>
}

export interface ServiceStatus {
  name: string
  category: string
  status: string
  latency_ms?: number
  error?: string
  configured?: boolean
  required?: string[]
  models?: string[]
  [key: string]: unknown
}
