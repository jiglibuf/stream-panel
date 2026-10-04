// Блок «Звук»: чистые правила поверх ответа GET /api/audio (ТЗ, раздел 3).
// Ничего не выдумываем: режим, выбранное устройство и проблемы приходят с
// сервера состоянием, здесь только перевод в понятные владельцу слова и
// решение о доступности элементов.

// Режим «Стрим» — три виртуальных выхода и петли, «Обычный» — прямой вывод.
export const MODE_STREAM = 'stream'
export const MODE_NORMAL = 'normal'

// Точная формулировка из ТЗ: переключение на живом эфире рвёт звук на несколько
// секунд, поэтому спрашиваем до отправки команды.
export const MODE_CONFIRM_TEXT = 'идёт эфир: звук прервётся на несколько секунд, переключить?'
const RECORD_CONFIRM_TEXT = 'идёт запись: звук прервётся на несколько секунд, переключить?'

export const DEVICES_EMPTY_TEXT = 'устройства вывода не найдены'

const MODE_LABELS = Object.freeze({
  [MODE_STREAM]: 'Стрим',
  [MODE_NORMAL]: 'Обычный',
})

export function modeLabel(mode) {
  return MODE_LABELS[mode] || 'неизвестно'
}

export function isKnownMode(mode) {
  return mode === MODE_STREAM || mode === MODE_NORMAL
}

// Текущий режим — только из снимка сервера; неизвестное значение честно null.
export function currentMode(audio) {
  const mode = audio && audio.mode
  return isKnownMode(mode) ? mode : null
}

export function isModeActive(audio, mode) {
  return currentMode(audio) === mode
}

// Тап по активному режиму (или при неизвестном состоянии) команду не шлёт:
// иначе получаем лишний вызов, который только дёргает звук.
export function shouldRequestMode(audio, mode) {
  const now = currentMode(audio)
  return isKnownMode(mode) && now !== null && now !== mode
}

export function needsConfirm(audio) {
  const obs = (audio && audio.obs) || {}
  return obs.streaming === true || obs.recording === true
}

// Текст подтверждения; null — подтверждение не требуется.
export function confirmText(audio) {
  const obs = (audio && audio.obs) || {}
  if (obs.streaming === true) return MODE_CONFIRM_TEXT
  if (obs.recording === true) return RECORD_CONFIRM_TEXT
  return null
}

export function modeRequest(mode, confirm = false) {
  return { mode, confirm: confirm === true }
}

export function deviceRequest(name) {
  return { name }
}

// ── устройства ────────────────────────────────────────────────────────────

export function deviceLabel(device) {
  if (!device) return '—'
  return device.label || device.name || '—'
}

export function deviceStateText(state) {
  if (state === 'RUNNING') return 'работает'
  if (state === 'SUSPENDED' || state === 'IDLE') return 'спит'
  return 'состояние неизвестно'
}

// В выборе участвуют только физические выходы: виртуальные наши выходы и
// мониторы — служебные, владельцу их выбирать нечего (ТЗ, 2.2).
export function isSelectableDevice(device) {
  return !!device && device.kind === 'physical'
}

function rank(device) {
  return [
    device.is_chosen ? 0 : 1,   // выбранное — наверх
    device.is_default ? 0 : 1,  // потом то, что сейчас по умолчанию
    device.state === 'RUNNING' ? 0 : 1, // живое раньше спящего
    String(device.label || device.name || ''),
  ]
}

export function sortDevices(devices) {
  const list = Array.isArray(devices) ? devices.filter(isSelectableDevice) : []
  return [...list].sort((a, b) => {
    const left = rank(a)
    const right = rank(b)
    for (let i = 0; i < 3; i += 1) {
      if (left[i] !== right[i]) return left[i] - right[i]
    }
    return left[3].localeCompare(right[3], 'ru')
  })
}

// ── состояние и проблемы ──────────────────────────────────────────────────

export function serviceText(service) {
  const value = service || {}
  if (value.active === true) return value.enabled === true ? 'работает' : 'запущена вручную'
  return value.enabled === true ? 'включена, но не работает' : 'выключена'
}

export function statusLine(audio) {
  if (!audio || !isKnownMode(audio.mode)) return 'состояние звука неизвестно'
  return `Режим: ${modeLabel(audio.mode)} · выход по умолчанию: ${audio.default_sink || '—'}`
    + ` · служба: ${serviceText(audio.service)}`
}

// problems приходят строками для человека; пустое и мусор отбрасываем, чтобы
// пустой список не выглядел тревожно.
export function issuesList(audio) {
  const raw = (audio && audio.issues) || []
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item) => typeof item === 'string' && item.trim() !== '')
    .map((item) => item.trim())
}

// online: true — сервис ответил, false — не отвечает, null — первый опрос ещё идёт.
// Переключение режима требует живого OBS (шаг obs в ТЗ), а выбор устройства
// пишется в файл и работает без него.
export function audioAvailability(audio, online) {
  if (online !== true || !audio) return { mode: false, device: false }
  const obsOnline = !!audio.obs && audio.obs.online === true
  return { mode: obsOnline, device: true }
}
