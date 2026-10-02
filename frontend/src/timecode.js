// Показ таймкода. Время считает только OBS: сюда приходит готовая строка
// вида «ЧЧ:ММ:СС.мс», и мы её показываем, не пересчитывая ничего на клиенте.
// Доли секунды отбрасываются исключительно для читаемости бейджа.

export const EMPTY_TIMECODE = '--:--:--'

// Часы могут перевалить за 99 в очень долгом эфире — ограничение сверху не нужно.
const TIMECODE = /^\d+:\d{2}:\d{2}(?:\.\d+)?$/

export function formatTimecode(value) {
  if (typeof value !== 'string') return EMPTY_TIMECODE
  const text = value.trim()
  if (!TIMECODE.test(text)) return EMPTY_TIMECODE
  const dot = text.indexOf('.')
  return dot === -1 ? text : text.slice(0, dot)
}
