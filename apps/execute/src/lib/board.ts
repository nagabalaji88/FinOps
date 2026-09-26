/**
 * The Execute board.
 *
 * This console runs exactly two agents at a time. All five implemented agents stay
 * registered and executable on the backend; the board decides which pair is on the
 * console right now. The default pair is a deployment setting; an operator can swap
 * either slot for another implemented agent, and the choice sticks per browser.
 */
import { useCallback, useSyncExternalStore } from 'react'

/** How many agents the Execute screen shows at once. Fixed by design, not a preference. */
export const BOARD_SIZE = 2

const STORAGE_KEY = 'finops.execute.board'

const DEFAULT_BOARD: string[] = (
  import.meta.env.VITE_EXECUTE_AGENTS ?? 'customer_service,aml_investigation'
)
  .split(',')
  .map((key: string) => key.trim())
  .filter(Boolean)
  .slice(0, BOARD_SIZE)

function read(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_BOARD
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return DEFAULT_BOARD
    const keys = parsed.filter((key): key is string => typeof key === 'string').slice(0, BOARD_SIZE)
    return keys.length === BOARD_SIZE ? keys : DEFAULT_BOARD
  } catch {
    return DEFAULT_BOARD
  }
}

let snapshot = read()
const listeners = new Set<() => void>()

function onStorage(event: StorageEvent) {
  if (event.key !== STORAGE_KEY) return
  snapshot = read()
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  // Re-read on wake-up, and follow the key while anyone is listening, so a second tab of
  // this console shows the same pair.
  if (listeners.size === 0) {
    snapshot = read()
    window.addEventListener('storage', onStorage)
  }
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) window.removeEventListener('storage', onStorage)
  }
}

function write(keys: string[]) {
  snapshot = keys.slice(0, BOARD_SIZE)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot))
  listeners.forEach((listener) => listener())
}

/**
 * The two agent keys on the board, and a setter that replaces one slot. Swapping in an
 * agent that already occupies the other slot exchanges the two rather than duplicating.
 */
export function useBoard(): [string[], (slot: number, agentKey: string) => void] {
  const keys = useSyncExternalStore(subscribe, () => snapshot, () => snapshot)

  const setSlot = useCallback((slot: number, agentKey: string) => {
    const next = [...snapshot]
    const existing = next.indexOf(agentKey)
    if (existing !== -1 && existing !== slot) next[existing] = next[slot]
    next[slot] = agentKey
    write(next)
  }, [])

  return [keys, setSlot]
}

/**
 * Resolve the board's agent keys against the agents the backend actually reports.
 *
 * A stored key can go stale — an agent renamed, retired or not yet seeded — so each slot
 * falls back to the next implemented agent rather than leaving a hole. The result never
 * repeats an agent and never exceeds the board size.
 */
export function resolveBoard<T extends { key: string }>(implemented: T[], keys: string[]): T[] {
  const resolved: T[] = []
  for (let slot = 0; slot < BOARD_SIZE; slot += 1) {
    const wanted = implemented.find(
      (agent) => agent.key === keys[slot] && !resolved.includes(agent),
    )
    const agent = wanted ?? implemented.find((candidate) => !resolved.includes(candidate))
    if (agent) resolved.push(agent)
  }
  return resolved
}
