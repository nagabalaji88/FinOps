import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Button } from '@/components/ui'
import { useAuth } from '@/store'
import { ApiError } from '@/lib/api'

const schema = z.object({
  email: z.string().min(3, 'Enter your work email'),
  password: z.string().min(1, 'Enter your password'),
  mfa_code: z.string().optional(),
})

type FormValues = z.infer<typeof schema>

export default function Login() {
  const { login, status } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [mfaRequired, setMfaRequired] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) })

  if (status === 'authenticated') {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  const onSubmit = handleSubmit(async (values) => {
    setServerError(null)
    try {
      await login(values.email, values.password, values.mfa_code || undefined)
      navigate((location.state as { from?: string } | null)?.from ?? '/', { replace: true })
    } catch (error) {
      if (error instanceof ApiError && error.details?.mfa_required) {
        setMfaRequired(true)
        setServerError('Enter the six digit code from your authenticator app.')
        return
      }
      setServerError(error instanceof Error ? error.message : 'Sign in failed')
    }
  })

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
        className="w-full max-w-sm"
      >
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-ink text-surface">
            <span className="text-base font-semibold">F</span>
          </div>
          <div>
            <h1 className="text-lg font-semibold tracking-tight">FinOps Command Center</h1>
            <p className="mt-1 text-xs text-ink-muted">Enterprise AI agent operations</p>
          </div>
        </div>

        <form onSubmit={onSubmit} className="card space-y-4 p-6" noValidate>
          <div className="space-y-1.5">
            <label htmlFor="email" className="metric-label">
              Work email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              className="input"
              placeholder="you@finops.local"
              {...register('email')}
            />
            {errors.email ? <p className="text-2xs text-state-err">{errors.email.message}</p> : null}
          </div>

          <div className="space-y-1.5">
            <label htmlFor="password" className="metric-label">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              className="input"
              placeholder="••••••••••••"
              {...register('password')}
            />
            {errors.password ? <p className="text-2xs text-state-err">{errors.password.message}</p> : null}
          </div>

          {mfaRequired ? (
            <div className="space-y-1.5">
              <label htmlFor="mfa" className="metric-label">
                Authenticator code
              </label>
              <input
                id="mfa"
                inputMode="numeric"
                autoComplete="one-time-code"
                className="input font-mono tracking-[0.3em]"
                placeholder="000000"
                maxLength={6}
                {...register('mfa_code')}
              />
            </div>
          ) : null}

          {serverError ? (
            <p className="rounded-lg border border-state-err/25 bg-state-err/10 px-3 py-2 text-2xs text-state-err">
              {serverError}
            </p>
          ) : null}

          <Button type="submit" variant="primary" className="w-full" loading={isSubmitting}>
            Sign in
          </Button>

          <p className="text-center text-2xs text-ink-subtle">
            Single sign-on is available when Keycloak is configured for this deployment.
          </p>
        </form>
      </motion.div>
    </div>
  )
}
