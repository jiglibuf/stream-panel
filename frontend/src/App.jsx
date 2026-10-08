// Пульт: связывает снапшот сервиса с элементами управления. Единственный
// источник истины — снапшот; после команды состояние берём из подтверждения
// сервиса (envelope.state), а не меняем интерфейс «на опережение».
import { useCallback, useEffect, useState } from 'react'

import { sendCommand, storage, vibrate } from './api.js'
import { availability } from './availability.js'
import { clearToken, readToken, writeToken } from './token.js'
import {
  AudioMixPanel,
  AudioPanel,
  Banners,
  Header,
  SceneGrid,
  TokenScreen,
  Transport,
  WaitingScreen,
} from './parts.jsx'
import { useAudio } from './useAudio.js'
import { usePanel } from './usePanel.js'

export default function App() {
  const store = storage()
  const [token, setToken] = useState(() => readToken(store))
  const { state, link, rtt, unauthorized, applyState } = usePanel(token)
  const mix = useAudio(token)
  const [notice, setNotice] = useState(null)

  // Wake Lock: экран не гаснет, пока страница видима. После возврата на вкладку
  // блокировка запрашивается заново — прежняя к тому моменту уже снята системой.
  useEffect(() => {
    let sentinel = null
    let cancelled = false

    const release = () => {
      try {
        sentinel?.release?.()
      } catch {
        /* уже отпущено системой */
      }
      sentinel = null
    }

    const acquire = async () => {
      if (cancelled || document.visibilityState !== 'visible') return
      if (!navigator.wakeLock?.request) return
      try {
        const next = await navigator.wakeLock.request('screen')
        if (cancelled) {
          next.release?.()
          return
        }
        sentinel = next
      } catch {
        /* Wake Lock недоступен (нет HTTPS или запрет) — не критично */
      }
    }

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        release()
        acquire()
      }
    }

    acquire()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisibility)
      release()
    }
  }, [])

  const forget = useCallback(() => {
    clearToken(store)
    setNotice(null)
    setToken(null)
  }, [store])

  // Общий путь любой команды: вибрация, отправка, реакция на 401 и на отказ OBS.
  const run = useCallback(async (name, args) => {
    vibrate()
    const answer = await sendCommand(token, name, args, store)
    if (answer.unauthorized) {
      setNotice('токен отклонён — введите новый')
      setToken(null)
      return
    }
    if (answer.state) applyState(answer.state) // подтверждение сервиса, не оптимизм
    if (answer.ok) setNotice(null)
    else setNotice(answer.error || 'команда отклонена')
  }, [token, store, applyState])

  // Токена нет или сервер ответил 401 (в том числе на звуковых ручках) — экран ввода.
  if (!token || unauthorized || mix.unauthorized) {
    return (
      <TokenScreen
        onSubmit={(value) => {
          writeToken(store, value)
          setNotice(null)
          setToken(value)
        }}
      />
    )
  }

  // Первый снапшот ещё не пришёл: показываем состояние связи, не пустой экран.
  if (!state) {
    return <WaitingScreen link={link} onForget={forget} />
  }

  const avail = availability(state)

  return (
    <div
      className="app"
      onContextMenu={(event) => {
        // Длинный тап по панели не поднимает меню браузера; в поле ввода
        // оставляем как есть — там через него вставляют токен.
        const tag = (event.target.tagName || '').toLowerCase()
        if (tag !== 'input' && tag !== 'textarea') event.preventDefault()
      }}
    >
      <Header
        snap={state}
        link={link}
        rtt={rtt}
      />
      <Banners snap={state} notice={notice} onDismiss={() => setNotice(null)} />
      <main className="board">
        <div className="column column--left">
          <Transport snap={state} avail={avail} onCommand={run} />
          <AudioMixPanel
            audio={mix.audio}
            online={mix.online}
            busy={mix.busy}
            notice={mix.notice}
            pendingMode={mix.pendingMode}
            onSetMode={mix.setMode}
            onConfirmMode={mix.confirmMode}
            onCancelMode={mix.cancelMode}
            onSetDevice={mix.setDevice}
            onSetAuto={mix.setAuto}
            onDismissNotice={mix.dismissNotice}
          />
        </div>
        <SceneGrid
          scenes={state.scenes}
          active={state.scene}
          disabled={!avail.scenes}
          onSelect={(name) => run('scene.set', { name })}
        />
        <AudioPanel snap={state} avail={avail} onCommand={run} />
      </main>
    </div>
  )
}
