// Состояние блока «Звук»: опрос GET /api/audio раз в 4 с (и сразу после
// действия) плюс команды POST /api/audio/mode и /api/audio/device. Отдельный
// WS-канал звуку не нужен — режим и устройства меняются редко, опроса хватает.
import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchAudio, sendAudio, storage } from './api.js'
import { autoRequest, modeRequest, needsConfirm, shouldRequestMode } from './audio.js'

const POLL_INTERVAL_MS = 4000

export function useAudio(token) {
  const [audio, setAudio] = useState(null)
  // true — сервис ответил; false — не отвечает; null — первый опрос ещё идёт.
  const [online, setOnline] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const [unauthorized, setUnauthorized] = useState(false)
  // Режим, который ждёт подтверждения при живом эфире.
  const [pendingMode, setPendingMode] = useState(null)

  const aliveRef = useRef(true)

  // Один запрос состояния. Возвращает снимок или null; ошибки не бросает.
  const load = useCallback(async () => {
    if (!token) return null
    const answer = await fetchAudio(token, storage())
    if (!aliveRef.current) return null
    if (answer.unauthorized) {
      setUnauthorized(true)
      return null
    }
    if (answer.audio) {
      setAudio(answer.audio)
      setOnline(true)
      return answer.audio
    }
    setAudio(null)
    setOnline(false)
    return null
  }, [token])

  useEffect(() => {
    if (!token) return undefined
    aliveRef.current = true
    setUnauthorized(false)
    setAudio(null)
    setOnline(null)
    let timer = null
    let stopped = false

    const tick = async () => {
      await load()
      if (!stopped) timer = setTimeout(tick, POLL_INTERVAL_MS)
    }
    tick()

    return () => {
      stopped = true
      aliveRef.current = false
      if (timer) clearTimeout(timer)
    }
  }, [token, load])

  const act = useCallback(async (action, body) => {
    if (!token) return
    setBusy(true)
    try {
      const answer = await sendAudio(token, action, body, storage())
      if (answer.unauthorized) {
        setUnauthorized(true)
        return
      }
      if (answer.ok) {
        setNotice(null)
        setPendingMode(null)
      } else if (answer.status === 409) {
        // Сервер отказал без подтверждения (эфир/запись) — показываем вопрос.
        setPendingMode(body?.mode ?? null)
        setNotice(null)
      } else {
        setNotice(answer.error || 'команда отклонена')
      }
      // Конверт ответа несёт свежее состояние — берём его, а не оптимизм;
      // иначе подтягиваем опросом сразу после действия.
      if (answer.state) {
        setAudio(answer.state)
        setOnline(true)
      } else {
        await load()
      }
    } finally {
      if (aliveRef.current) setBusy(false)
    }
  }, [token, load])

  // Тап по режиму: активный — ничего, при эфире — сначала подтверждение.
  const setMode = useCallback((mode) => {
    if (!shouldRequestMode(audio, mode)) return
    if (needsConfirm(audio)) {
      setPendingMode(mode)
      return
    }
    act('mode', modeRequest(mode, false))
  }, [audio, act])

  const confirmMode = useCallback(() => {
    if (!pendingMode) return
    act('mode', modeRequest(pendingMode, true))
  }, [pendingMode, act])

  const cancelMode = useCallback(() => setPendingMode(null), [])

  const setDevice = useCallback((name) => act('device', { name }), [act])

  // Тумблер автоматики: решение запоминает сервер, пульт только просит.
  const setAuto = useCallback((enabled) => act('auto', autoRequest(enabled)), [act])

  return {
    audio,
    online,
    busy,
    notice,
    pendingMode,
    unauthorized,
    setMode,
    confirmMode,
    cancelMode,
    setDevice,
    setAuto,
    dismissNotice: () => setNotice(null),
    refresh: load,
  }
}
