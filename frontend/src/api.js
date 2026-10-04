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

// GET /api/audio — состояние звука: режим, устройства, проблемы. Отдельного
// WS-канала для звука нет, поэтому это же состояние берётся опросом.
export async function fetchAudio(token, store) {
  let response
  try {
    response = await fetch('/api/audio', { headers: authHeaders(token) })
  } catch {
    return { offline: true }
  }
  if (applyAuthStatus(response.status, store)) return { unauthorized: true }
  if (!response.ok) return { offline: true, error: `HTTP ${response.status}` }
  try {
    return { audio: await response.json() }
  } catch {
    return { offline: true, error: 'ответ не является JSON' }
  }
}

// POST /api/audio/mode | /api/audio/device — конверт как у /api/cmd:
// {ok, name, changed, state, steps, error}. 409 (эфир без подтверждения) и
// 400 (неизвестный режим) приходят с сообщением сервера в error.
export async function sendAudio(token, action, body, store) {
  let response
  try {
    response = await fetch(`/api/audio/${action}`, {
      method: 'POST',
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    })
  } catch {
    return { ok: false, offline: true, error: 'нет связи с сервисом' }
  }
  if (applyAuthStatus(response.status, store)) return { unauthorized: true }

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok) {
    return { ok: false, status: response.status, error: payload?.error ?? `HTTP ${response.status}` }
  }
  return payload ?? { ok: false, error: 'пустой ответ сервиса' }
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
