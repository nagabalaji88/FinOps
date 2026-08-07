import { Suspense, lazy, useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { Login, Spinner, useAuth, useUi } from '@finops/shared'
import { Shell } from '@/components/layout/Shell'

const Execute = lazy(() => import('@/pages/Execute'))
const Analytics = lazy(() => import('@/pages/Analytics'))

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
          element={<Login title="FinOps Execute" subtitle="Run production agents against the system of record" />}
        />
        <Route
          element={
            <Protected>
              <Shell />
            </Protected>
          }
        >
          <Route path="/" element={<Execute />} />
          <Route path="/analytics" element={<Analytics />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  )
}
