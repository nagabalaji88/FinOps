import { Suspense, lazy, useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { Login, Spinner, useAuth, useUi } from '@finops/shared'

const Overview = lazy(() => import('@/pages/Overview'))
const Agents = lazy(() => import('@/pages/Agents'))
const AgentDetail = lazy(() => import('@/pages/AgentDetail'))
const Geography = lazy(() => import('@/pages/Geography'))
const Executions = lazy(() => import('@/pages/Executions'))
const ExecutionDetail = lazy(() => import('@/pages/ExecutionDetail'))
const Approvals = lazy(() => import('@/pages/Approvals'))
const Costs = lazy(() => import('@/pages/Costs'))
const Knowledge = lazy(() => import('@/pages/Knowledge'))
const Tools = lazy(() => import('@/pages/Tools'))
const Services = lazy(() => import('@/pages/Services'))
const Evaluations = lazy(() => import('@/pages/Evaluations'))
const Playground = lazy(() => import('@/pages/Playground'))
const Builder = lazy(() => import('@/pages/Builder'))
const Logs = lazy(() => import('@/pages/Logs'))
const Security = lazy(() => import('@/pages/Security'))
const Settings = lazy(() => import('@/pages/Settings'))

function Loading() {
  return (
    <div className="flex h-full min-h-[50vh] items-center justify-center">
      <Spinner className="h-6 w-6 text-ink-subtle" />
    </div>
  )
}

function Protected({ children }: { children: React.ReactNode }) {
  const status = useAuth((state) => state.status)
  const location = useLocation()
  if (status === 'idle' || status === 'loading') return <Loading />
  if (status !== 'authenticated') return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return <>{children}</>
}

export function App() {
  const restore = useAuth((state) => state.restore)
  const setTheme = useUi((state) => state.setTheme)
  const theme = useUi((state) => state.theme)

  useEffect(() => {
    void restore()
    setTheme(theme)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <Suspense fallback={<Loading />}>
      <Routes>
        <Route
          path="/login"
          element={<Login title="FinOps Platform Console" subtitle="Agent operations, governance and observability" />}
        />
        <Route
          element={
            <Protected>
              <AppShell />
            </Protected>
          }
        >
          <Route path="/" element={<Overview />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:agentKey" element={<AgentDetail />} />
          <Route path="/geography" element={<Geography />} />
          <Route path="/executions" element={<Executions />} />
          <Route path="/executions/:executionId" element={<ExecutionDetail />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/costs" element={<Costs />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/tools" element={<Tools />} />
          <Route path="/services" element={<Services />} />
          <Route path="/evaluations" element={<Evaluations />} />
          <Route path="/playground" element={<Playground />} />
          <Route path="/builder" element={<Builder />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/security" element={<Security />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  )
}
