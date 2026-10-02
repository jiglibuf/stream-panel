// Приведение уровня VU для показа. level — множитель из OBS: 1.0 это 0 dBFS,
// всё, что больше 1, клиппинг. Полосу зажимаем в 0..1, а перегруз показываем
// отдельным признаком, чтобы красить индикатор.

export function vuLevel(level) {
  const value = Number(level)
  if (!Number.isFinite(value) || value <= 0) {
    return { ratio: 0, overload: false }
  }
  const overload = value > 1
  return { ratio: overload ? 1 : value, overload }
}
