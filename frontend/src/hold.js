// Решение по удержанию кнопки. Чистая функция: получает прошедшее время и порог,
// возвращает состояние и долю прогресса. Никаких таймеров и DOM.

export const HOLD_MS = 1500

export const HOLD_IDLE = 'idle'   // ещё не держат (или отпустили раньше)
export const HOLD_HOLDING = 'holding'  // держат, порог не достигнут
export const HOLD_DONE = 'done'   // порог пройден — команду можно отправлять

export function holdResult(elapsedMs, thresholdMs = HOLD_MS) {
  const threshold = Number.isFinite(thresholdMs) && thresholdMs > 0 ? thresholdMs : HOLD_MS
  const elapsed = Number(elapsedMs)
  if (!Number.isFinite(elapsed) || elapsed <= 0) {
    return { status: HOLD_IDLE, progress: 0 }
  }
  if (elapsed >= threshold) {
    return { status: HOLD_DONE, progress: 1 }
  }
  const progress = Math.min(1, Math.max(0, elapsed / threshold))
  return { status: HOLD_HOLDING, progress }
}
