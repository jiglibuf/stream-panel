// Связь с сервисом по HTTP. Заголовок Authorization добавляется только здесь,
// а 401 — единственный ответ, который стирает токен и просит новый ввод.
import { applyAuthStatus } from './token.js'

export function storage() {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

// Короткая вибрация на тап (15-25 мс). Нет поддержки — молча пропускаем.
export function vibrate(ms = 18) {
  try {
    navigator.vibrate?.(ms)
  } catch {
    /* вибрация не критична */
  }
}

function authHeaders(token) {
  return { Authorization: `Bearer ${token}` }
}

// GET /api/state — резервный источник состояния, пока нет WebSocket.
export async function fetchState(token, store) {
  let response
  try {
    response = await fetch('/api/state', { headers: authHeaders(token) })
  } catch {
    return { offline: true }
  }
  if (applyAuthStatus(response.status, store)) return { unauthorized: true }
  if (!response.ok) return { error: `HTTP ${response.status}` }
  try {
    return { state: await response.json() }
  } catch {
    return { error: 'ответ не является JSON' }
  }
}

// POST /api/cmd — конверт {ok, name, changed, state, error} (+ steps у паники).
export async function sendCommand(token, name, args, store) {
  let response
  try {
    response = await fetch('/api/cmd', {
      method: 'POST',
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, args: args ?? null }),
    })
  } catch {
    return { ok: false, error: 'нет связи с сервисом' }
  }
  if (applyAuthStatus(response.status, store)) return { unauthorized: true }

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok) {
    return { ok: false, error: payload?.error ?? `HTTP ${response.status}`, status: response.status }
  }
  return payload ?? { ok: false, error: 'пустой ответ сервиса' }
}
