// Элементы интерфейса пульта. Все данные приходят сверху (из снапшота), своими
// руками состояние OBS здесь никто не выдумывает.
import { useEffect, useRef, useState } from 'react'

import { vibrate } from './api.js'
import { holdResult, HOLD_DONE } from './hold.js'
import { formatTimecode } from './timecode.js'
import { vuLevel } from './vu.js'
import { LINK_CONNECTING, LINK_LIVE } from './usePanel.js'

// Кнопка с удержанием: отпустил раньше 1.5 с — команда не уходит.
export function HoldButton({ label, note, disabled, onConfirm }) {
  const [progress, setProgress] = useState(0)
  const frameRef = useRef(0)
  const startRef = useRef(0)
  const doneRef = useRef(false)
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  const cancelFrame = () => {
    if (frameRef.current) cancelAnimationFrame(frameRef.current)
    frameRef.current = 0
  }
  useEffect(() => cancelFrame, [])

  const tick = () => {
    if (disabledRef.current) {
      cancelFrame()
      setProgress(0)
      return
    }
    const { status, progress: value } = holdResult(performance.now() - startRef.current)
    if (status === HOLD_DONE) {
      cancelFrame()
      doneRef.current = true
      setProgress(1)
      onConfirm()
      return
    }
    setProgress(value)
    frameRef.current = requestAnimationFrame(tick)
  }

  const begin = () => {
    if (disabledRef.current) return
    doneRef.current = false
    startRef.current = performance.now()
    setProgress(0)
    vibrate()
    cancelFrame()
    frameRef.current = requestAnimationFrame(tick)
  }

  const release = () => {
    cancelFrame()
    if (!doneRef.current) setProgress(0) // порог не пройден — отмена, команды нет
  }

  return (
    <button
      type="button"
      className="btn btn--hold"
      disabled={disabled}
      aria-label={note ? `${label}. ${note}` : label}
      onPointerDown={(event) => {
        try {
          event.currentTarget.setPointerCapture(event.pointerId)
        } catch {
          /* захват указателя не поддерживается — не критично */
        }
        begin()
      }}
      onPointerUp={release}
      onPointerCancel={release}
      onLostPointerCapture={release}
      onKeyDown={(event) => {
        if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) begin()
      }}
      onKeyUp={(event) => {
        if (event.key === ' ' || event.key === 'Enter') release()
      }}
    >
      <span className="btn__progress" style={{ width: `${Math.round(progress * 100)}%` }} aria-hidden="true" />
      <span className="btn__label">{label}</span>
      <span className="btn__hint">{progress > 0 && progress < 1 ? 'держите' : note}</span>
    </button>
  )
}

export function SceneGrid({ scenes, active, disabled, onSelect }) {
  const list = Array.isArray(scenes) ? scenes : []
  return (
    <section className="panel panel--scenes" aria-label="Сцены">
      <h2 className="panel__title">
        Сцены
        <span className="panel__note">подсветка — по подтверждению от OBS</span>
      </h2>
      <div className="scenes">
        {list.map((scene) => (
          <button
            key={scene.name}
            type="button"
            className={`scene ${scene.name === active ? 'is-active' : ''}`}
            disabled={disabled}
            onClick={() => onSelect(scene.name)}
          >
            {scene.num != null && <span className="scene__num">{scene.num}</span>}
            <span className="scene__name">{scene.name}</span>
          </button>
        ))}
        {list.length === 0 && <p className="scenes__empty">список сцен пуст</p>}
      </div>
    </section>
  )
}

export function Transport({ snap, avail, onCommand }) {
  const recordActive = !!(snap.record && snap.record.active)
  return (
    <section className="panel panel--transport" aria-label="Транспорт">
      <h2 className="panel__title">Транспорт</h2>
      <div className="panel__body">
        <HoldButton label="Начать эфир" note="удержание 1.5 с" disabled={!avail.streamStart}
          onConfirm={() => onCommand('stream.start', { confirm: true })} />
        <HoldButton label="Стоп эфира" note="удержание 1.5 с" disabled={!avail.streamStop}
          onConfirm={() => onCommand('stream.stop', { confirm: true })} />
        <button type="button" className={`btn ${recordActive ? 'is-on' : ''}`} disabled={!avail.record}
          onClick={() => onCommand('record.toggle')}>
          {recordActive ? 'Остановить запись' : 'Запись'}
        </button>
        <button type="button" className="btn" disabled={!avail.replaySave}
          onClick={() => onCommand('replay.save')}>
          Сохранить повтор
        </button>
        <button type="button" className="btn btn--danger" disabled={!avail.panic}
          onClick={() => onCommand('panic')}>
          Паника
        </button>
      </div>
    </section>
  )
}

export function AudioTrack({ label, channel, muteDisabled, volumeDisabled, onToggleMute, onVolume }) {
  // draft — значение ползунка, пока его тянут. Входящее состояние в это время
  // игнорируется, иначе пришлось бы бороться с сервером за позицию бегунка.
  const [draft, setDraft] = useState(null)
  const draftRef = useRef(null)
  const volume = typeof channel?.vol === 'number' ? channel.vol : 0
  const muted = channel?.mute === true
  const shown = draft ?? volume
  const { ratio, overload } = vuLevel(channel?.level)

  const remember = (value) => {
    draftRef.current = value
    setDraft(value)
  }
  const commit = () => {
    if (draftRef.current === null) return
    const value = draftRef.current
    draftRef.current = null
    setDraft(null)
    if (value !== volume) onVolume(value)
  }

  return (
    <div className={`track ${muted ? 'is-muted' : ''}`}>
      <div className="track__head">
        <span className="track__label">{label}</span>
        <span className="track__value">{shown}%</span>
      </div>
      <button type="button" className={`btn btn--small ${muted ? 'is-on' : ''}`} disabled={muteDisabled}
        aria-pressed={muted} onClick={onToggleMute}>
        {muted ? 'Заглушено' : 'Звук включён'}
      </button>
      <input
        className="slider"
        type="range"
        min="0"
        max="100"
        step="1"
        value={shown}
        style={{ '--vol': `${shown}%` }}
        disabled={volumeDisabled}
        aria-label={`громкость: ${label}`}
        onPointerDown={() => remember(volume)}
        onChange={(event) => remember(Number(event.target.value))}
        onPointerUp={commit}
        onPointerCancel={commit}
        onKeyUp={commit}
        onBlur={commit}
      />
      <div className="vu" role="img"
        aria-label={`уровень ${label}: ${Math.round(ratio * 100)} процентов${overload ? ', перегруз' : ''}`}>
        <div className={`vu__fill ${overload ? 'is-overload' : ''}`}
          style={{ width: `${Math.round(ratio * 100)}%` }} />
      </div>
      <span className="track__flag">{muted ? 'заглушено' : 'сигнал'}</span>
    </div>
  )
}

export function AudioPanel({ snap, avail, onCommand }) {
  const audio = snap.audio || {}
  return (
    <section className="panel panel--audio" aria-label="Звук">
      <h2 className="panel__title">Звук</h2>
      <div className="panel__body">
        <AudioTrack
          label="Микрофон"
          channel={audio.mic}
          muteDisabled={!avail.micMute}
          volumeDisabled={!avail.micVolume}
          onToggleMute={() => onCommand('audio.toggleMute', { track: 'mic' })}
          onVolume={(value) => onCommand('audio.setVolume', { track: 'mic', value })}
        />
        <AudioTrack
          label="Звук системы"
          channel={audio.sys}
          muteDisabled={!avail.sysMute}
          volumeDisabled={!avail.sysVolume}
          onToggleMute={() => onCommand('audio.toggleMute', { track: 'sys' })}
          onVolume={(value) => onCommand('audio.setVolume', { track: 'sys', value })}
        />
      </div>
    </section>
  )
}

export function ConnectionBadge({ link, rtt }) {
  const text = link === LINK_LIVE ? 'на связи' : link === LINK_CONNECTING ? 'подключение' : 'опрос'
  return (
    <div className={`link link--${link}`} role="status" aria-live="polite">
      <span className="link__dot" aria-hidden="true" />
      <span className="link__text">{text}</span>
      <span className="link__rtt" title="задержка до сервиса">
        {typeof rtt === 'number' ? `${rtt} мс` : '— мс'}
      </span>
    </div>
  )
}

export function Header({ snap, link, rtt }) {
  const stream = snap.stream || {}
  const record = snap.record || {}
  return (
    <header className="header">
      <div className="header__block">
        <span className={`live ${stream.active ? 'is-on' : ''}`}>
          <span className="live__dot" aria-hidden="true" />
          {stream.active ? 'LIVE' : 'НЕ В ЭФИРЕ'}
        </span>
        <span className="timer" aria-label="таймкод эфира">{formatTimecode(stream.time)}</span>
        {stream.reconnect && <span className="header__warn">переподключение</span>}
      </div>
      <ConnectionBadge link={link} rtt={rtt} />
      <div className="header__block header__block--end">
        {/* Индикатор записи: команду отдаёт кнопка в блоке транспорта, чтобы
            не было двух точек входа в одно действие. */}
        <span className={`record ${record.active ? 'is-on' : ''}`} role="status"
          aria-label={record.active ? 'идёт запись' : 'запись остановлена'}>
          <span className="record__dot" aria-hidden="true" />
          <span className="record__label">{record.active ? 'ЗАПИСЬ' : 'НЕ ПИШЕТ'}</span>
          <span className="timer timer--small">{formatTimecode(record.time)}</span>
        </span>
      </div>
    </header>
  )
}

export function Banners({ snap, notice, onDismiss }) {
  const banners = []
  if (!snap.obs) {
    const detail = snap.error && snap.error !== 'OBS офлайн' ? `: ${snap.error}` : ''
    banners.push({ key: 'obs', kind: 'error', text: `OBS офлайн${detail} — управление недоступно` })
  }
  if (snap.stale) {
    banners.push({ key: 'stale', kind: 'warn', text: 'данные устарели — разрушительные действия заблокированы' })
  }
  if (notice) {
    banners.push({ key: 'notice', kind: 'warn', text: notice, dismiss: true })
  }
  if (banners.length === 0) return null
  return (
    <div className="banners" role="status" aria-live="polite">
      {banners.map((banner) => (
        <div key={banner.key} className={`banner banner--${banner.kind}`}>
          <span className="banner__text">{banner.text}</span>
          {banner.dismiss && (
            <button type="button" className="banner__close" onClick={onDismiss} aria-label="закрыть">×</button>
          )}
        </div>
      ))}
    </div>
  )
}

export function TokenScreen({ onSubmit }) {
  const [value, setValue] = useState('')
  const inputRef = useRef(null)
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  return (
    <div className="gate">
      <form className="gate__card" onSubmit={(event) => {
        event.preventDefault()
        const token = value.trim()
        if (token) onSubmit(token)
      }}>
        <h1 className="gate__title">Пульт стрима</h1>
        <p className="gate__text">Введите токен доступа — он лежит в config.json на компьютере со стримом.</p>
        <input
          ref={inputRef}
          className="gate__input"
          type="password"
          inputMode="text"
          autoComplete="off"
          spellCheck="false"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="токен доступа"
          aria-label="токен доступа"
        />
        <button type="submit" className="btn btn--primary gate__submit" disabled={!value.trim()}>
          Подключиться
        </button>
        <p className="gate__hint">Токен хранится только в этом браузере и уходит лишь на ваш сервис.</p>
      </form>
    </div>
  )
}

export function WaitingScreen({ link, onForget }) {
  return (
    <div className="gate">
      <div className="gate__card">
        <h1 className="gate__title">Пульт стрима</h1>
        <div className="link link--wide">
          <ConnectionBadge link={link} rtt={null} />
        </div>
        <p className="gate__text">Ждём первый снапшот от сервиса…</p>
        <button type="button" className="btn gate__submit" onClick={onForget}>Сменить токен</button>
      </div>
    </div>
  )
}
