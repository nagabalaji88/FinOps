import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { api, tokenStore, type Principal } from '@/lib/api'

interface AuthState {
  user: Principal | null
  status: 'idle' | 'loading' | 'authenticated' | 'anonymous'
  error: string | null
  login: (email: string, password: string, mfaCode?: string) => Promise<void>
  logout: () => void
  restore: () => Promise<void>
  can: (permission: string) => boolean
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  status: 'idle',
  error: null,
  async login(email, password, mfaCode) {
    set({ status: 'loading', error: null })
    try {
      const response = await api.post<{
        access_token: string
        refresh_token: string
        user: Principal
      }>('/auth/login', { email, password, mfa_code: mfaCode })
      tokenStore.write({ access: response.access_token, refresh: response.refresh_token })
      set({ user: response.user, status: 'authenticated', error: null })
    } catch (error) {
      set({ status: 'anonymous', error: (error as Error).message })
      throw error
    }
  },
  logout() {
    tokenStore.clear()
    set({ user: null, status: 'anonymous' })
  },
  async restore() {
    if (!tokenStore.read().access) {
      set({ status: 'anonymous' })
      return
    }
    set({ status: 'loading' })
    try {
      const user = await api.get<Principal>('/auth/me')
      set({ user, status: 'authenticated' })
    } catch {
      tokenStore.clear()
      set({ user: null, status: 'anonymous' })
    }
  },
  can(permission) {
    const user = get().user
    if (!user) return false
    return user.roles.includes('admin') || user.permissions.includes(permission)
  },
}))

interface UiState {
  theme: 'light' | 'dark'
  sidebarCollapsed: boolean
  commandOpen: boolean
  density: 'comfortable' | 'compact'
  setTheme: (theme: 'light' | 'dark') => void
  toggleTheme: () => void
  toggleSidebar: () => void
  setCommandOpen: (open: boolean) => void
  setDensity: (density: 'comfortable' | 'compact') => void
}

export const useUi = create<UiState>()(
  persist(
    (set, get) => ({
      theme:
        (localStorage.getItem('finops.theme') as 'light' | 'dark' | null) ??
        (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
      sidebarCollapsed: false,
      commandOpen: false,
      density: 'comfortable',
      setTheme(theme) {
        document.documentElement.classList.toggle('dark', theme === 'dark')
        localStorage.setItem('finops.theme', theme)
        set({ theme })
      },
      toggleTheme() {
        get().setTheme(get().theme === 'dark' ? 'light' : 'dark')
      },
      toggleSidebar() {
        set({ sidebarCollapsed: !get().sidebarCollapsed })
      },
      setCommandOpen(commandOpen) {
        set({ commandOpen })
      },
      setDensity(density) {
        set({ density })
      },
    }),
    { name: 'finops.ui', partialize: (state) => ({ sidebarCollapsed: state.sidebarCollapsed, density: state.density }) },
  ),
)

interface ToastMessage {
  id: string
  title: string
  description?: string
  tone: 'ok' | 'err' | 'info' | 'warn'
}

interface ToastState {
  toasts: ToastMessage[]
  push: (toast: Omit<ToastMessage, 'id'>) => void
  dismiss: (id: string) => void
}

export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push(toast) {
    const id = crypto.randomUUID()
    set((state) => ({ toasts: [...state.toasts, { ...toast, id }] }))
    setTimeout(() => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })), 5200)
  },
  dismiss(id) {
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }))
  },
}))
