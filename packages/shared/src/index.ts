/**
 * Everything both consoles share: the typed API client, the design system, the
 * auth/UI/toast stores, formatting helpers and the sign-in screen.
 *
 * The two applications are separate builds with separate deployments, but they talk to
 * one backend and must look and behave like one product — so this package is the single
 * definition of both the wire contract and the visual language.
 */
export * from './lib/api'
export * from './lib/utils'
export * from './store'
export * from './components/ui'
export { Login } from './pages/Login'
