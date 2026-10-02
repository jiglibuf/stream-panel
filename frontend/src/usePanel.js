// Живая связь с сервисом: WebSocket со снапшотами, пинг раз в 5 с (RTT),
// переподключение с backoff 0.5 -> 8 с и резервный опрос GET /api/state, пока
// WebSocket недоступен. 401 на любом запросе — сигнал «токен не подходит».
import { useEffect, useState } from 'react'

import { fetchState, storage } from './api.js'
import { parseMessage, wsUrl } from './ws.js'

export const LINK_CONNECTING = 'connecting'
export const LINK_LIVE = 'live'
export const LINK_POLLING = 'polling'

const PING_INTERVAL_MS = 5000
const POLL_INTERVAL_MS = 1000
const BACKOFF_MIN_MS = 500
const BACKOFF_MAX_MS = 8000

export function usePanel(token) {
  const [state, setState] = useState(null)
  const [link, setLink] = useState(LINK_CONNECTING)
  const [rtt, setRtt] = useState(null)
  const [unauthorized, setUnauthorized] = useState(false)

  useEffect(() => {
    if (!token) return undefined
    // Новый токен — снимаем прежний признак отказа, иначе экран ввода залипнет.
    setUnauthorized(false)
    const store = storage()
    let closed = false
    let socket = null
    let reconnectTimer = null
    let pingTimer = null
    let pollTimer = null
    let backoff = BACKOFF_MIN_MS

    const clearTimer = (kind) => {
      if (kind === 'reconnect' && reconnectTimer) clearTimeout(reconnectTimer)
      if (kind === 'ping' && pingTimer) clearInterval(pingTimer)
      if (kind === 'poll' && pollTimer) clearTimeout(pollTimer)
      if (kind === 'reconnect') reconnectTimer = null
      if (kind === 'ping') pingTimer = null
      if (kind === 'poll') pollTimer = null
    }

    const stop = () => {
      closed = true
      clearTimer('reconnect')
      clearTimer('ping')
      clearTimer('poll')
      if (socket) {
        try {
          socket.close()
        } catch {
          /* соединение уже мертво */
        }
        socket = null
      }
    }

    const poll = async () => {
      if (closed) return
      const answer = await fetchState(token, store)
      if (closed) return
      if (answer.unauthorized) {
        setUnauthorized(true)
        stop()
        return
      }
      if (answer.state) {
        setState(answer.state)
        if (socket?.readyState !== WebSocket.OPEN) setLink(LINK_POLLING)
      }
      pollTimer = setTimeout(poll, POLL_INTERVAL_MS)
    }

    const startPing = () => {
      clearTimer('ping')
      pingTimer = setInterval(() => {
        if (socket?.readyState !== WebSocket.OPEN) return
        try {
          socket.send(JSON.stringify({ type: 'ping', t: Date.now() }))
        } catch {
          /* отправка сорвалась — соединение закроется само */
        }
      }, PING_INTERVAL_MS)
    }

    const scheduleReconnect = () => {
      if (closed) return
      setLink(LINK_POLLING)
      reconnectTimer = setTimeout(connect, backoff)
      backoff = Math.min(BACKOFF_MAX_MS, backoff * 2)
      if (!pollTimer) poll() // пока нет WS — состояние берём через GET
    }

    function connect() {
      if (closed) return
      setLink(LINK_CONNECTING)
      try {
        socket = new WebSocket(wsUrl(window.location, token))
      } catch {
        scheduleReconnect()
        return
      }
      socket.onopen = () => {
        backoff = BACKOFF_MIN_MS
        setLink(LINK_LIVE)
        clearTimer('poll')
        startPing()
      }
      socket.onmessage = (event) => {
        // Разбор никогда не бросает: мусорное сообщение просто игнорируется.
        const message = parseMessage(event.data)
        if (message.kind === 'state' && message.state) {
          setState(message.state)
          setLink(LINK_LIVE)
        } else if (message.kind === 'pong' && typeof message.t === 'number') {
          setRtt(Math.max(0, Math.round(Date.now() - message.t)))
        }
      }
      socket.onclose = () => {
        clearTimer('ping')
        setRtt(null)
        scheduleReconnect()
      }
      socket.onerror = () => {
        try {
          socket.close()
        } catch {
          /* закрытие уже идёт */
        }
      }
    }

    connect()
    return stop
  }, [token])

  return { state, link, rtt, unauthorized, applyState: setState }
}
