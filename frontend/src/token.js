// Токен в localStorage: чтение, запись, очистка. Хранилище подменяется
// (в тестах — объект в памяти), поэтому модуль не зависит от браузера.
// Ответ 401 означает единственное: сохранённый токен больше не подходит — его
// нужно стереть и показать экран ввода.

export const TOKEN_STORAGE_KEY = 'stream-panel-token'

function usable(storage) {
  return storage && typeof storage.getItem === 'function' ? storage : null
}

export function readToken(storage) {
  const store = usable(storage)
  if (!store) return null
  try {
    const value = store.getItem(TOKEN_STORAGE_KEY)
    return typeof value === 'string' && value ? value : null
  } catch {
    return null
  }
}

export function writeToken(storage, token) {
  const store = usable(storage)
  if (!store) return null
  const value = typeof token === 'string' ? token.trim() : ''
  try {
    if (!value) {
      store.removeItem(TOKEN_STORAGE_KEY)
      return null
    }
    store.setItem(TOKEN_STORAGE_KEY, value)
    return value
  } catch {
    return null
  }
}

export function clearToken(storage) {
  const store = usable(storage)
  if (!store) return
  try {
    store.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    /* хранилище недоступно — чистить нечего */
  }
}

// Автоочистка по ответу: 401 -> токен стёрт, приложение просит новый.
export function applyAuthStatus(status, storage) {
  if (status !== 401) return false
  clearToken(storage)
  return true
}
