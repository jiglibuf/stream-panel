// Доступность кнопок по снапшоту. Единственный источник истины — снапшот.
// Логика повторяет серверный слой команд: те же разрушительные команды, те же
// условия отказа, чтобы кнопка не была активной там, где сервер ответит 409.

// Разрушительные команды: при устаревших данных сервер отказывает (409).
export const DESTRUCTIVE = Object.freeze(
  new Set(['stream.start', 'stream.stop', 'record.toggle', 'replay.save']))

function offlineResult() {
  return {
    scenes: false, streamStart: false, streamStop: false, record: false,
    replaySave: false, panic: false, micMute: false, micVolume: false,
    sysMute: false, sysVolume: false,
  }
}

export function availability(state) {
  const snap = state || {}
  // OBS офлайн — недоступно ВСЁ: ни одна команда не буферизуется.
  if (!snap.obs) return offlineResult()

  // Разрушительные действия запрещены на устаревших данных; остальные живут.
  const destructive = !snap.stale
  const streamActive = !!(snap.stream && snap.stream.active)
  const replay = !!snap.replay

  return {
    scenes: true,
    streamStart: destructive && !streamActive,
    streamStop: destructive && streamActive,
    record: destructive,
    replaySave: destructive && replay,
    panic: true,
    micMute: true,
    micVolume: true,
    sysMute: true,
    sysVolume: true,
  }
}
