// Хранение токена через подменяемое хранилище и автоочистка по ответу 401.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  readToken, writeToken, clearToken, applyAuthStatus, TOKEN_STORAGE_KEY,
} from './token.js'

// Простейшее хранилище в памяти — замена localStorage в тестах.
function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)) },
    removeItem: (key) => { map.delete(key) },
    _map: map,
  }
}

test('запись, чтение и очистка токена', () => {
  const storage = fakeStorage()
  assert.equal(readToken(storage), null)

  writeToken(storage, 'секрет-123')
  assert.equal(readToken(storage), 'секрет-123')
  assert.equal(storage._map.get(TOKEN_STORAGE_KEY), 'секрет-123')

  clearToken(storage)
  assert.equal(readToken(storage), null)
})

test('пустой токен не сохраняется, а очищает прежний', () => {
  const storage = fakeStorage({ [TOKEN_STORAGE_KEY]: 'старый' })
  writeToken(storage, '')
  assert.equal(readToken(storage), null)
  writeToken(storage, '   ')
  assert.equal(readToken(storage), null)
})

test('битое хранилище не роняет чтение', () => {
  const broken = {
    getItem() { throw new Error('нет доступа') },
    setItem() { throw new Error('нет доступа') },
    removeItem() { throw new Error('нет доступа') },
  }
  assert.equal(readToken(broken), null)
  assert.doesNotThrow(() => writeToken(broken, 'токен'))
  assert.doesNotThrow(() => clearToken(broken))
})

test('только 401 очищает токен', () => {
  const storage = fakeStorage({ [TOKEN_STORAGE_KEY]: 'живой' })
  assert.equal(applyAuthStatus(200, storage), false)
  assert.equal(readToken(storage), 'живой')

  assert.equal(applyAuthStatus(401, storage), true)
  assert.equal(readToken(storage), null)

  const other = fakeStorage({ [TOKEN_STORAGE_KEY]: 'живой' })
  assert.equal(applyAuthStatus(500, other), false)
  assert.equal(readToken(other), 'живой')
  assert.equal(applyAuthStatus(403, other), false)
})
