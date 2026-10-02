// Транспорт до сервиса: адрес WebSocket и разбор входящих сообщений.
// Браузерный WebSocket не умеет заголовки, поэтому токен едет параметром token.

const WS_PATH = '/ws'

export function wsUrl(location, token) {
  const secure = location && location.protocol === 'https:'
  const host = (location && location.host) || ''
  const value = typeof token === 'string' ? token : ''
  return `${secure ? 'wss:' : 'ws:'}//${host}${WS_PATH}?token=${encodeURIComponent(value)}`
}

// Сообщения сервиса: {type:'state', state} и {type:'pong', t}. Всё прочее и любой
// мусор — не повод для исключения: соединение живое, сообщение просто игнорируем.
export function parseMessage(raw) {
  let data = raw
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw)
    } catch {
      return { kind: 'invalid' }
    }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return { kind: 'invalid' }
  }
  if (data.type === 'state') {
    return { kind: 'state', state: data.state ?? null }
  }
  if (data.type === 'pong') {
    return { kind: 'pong', t: data.t }
  }
  return { kind: 'other' }
}
