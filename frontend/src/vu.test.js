// Приведение уровня VU для показа: множитель может быть больше 1 — это перегруз.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { vuLevel } from './vu.js'

test('обычный сигнал — доля как есть', () => {
  assert.deepEqual(vuLevel(0.5), { ratio: 0.5, overload: false })
  assert.deepEqual(vuLevel(1), { ratio: 1, overload: false })
  assert.deepEqual(vuLevel(0), { ratio: 0, overload: false })
})

test('больше 1 — перегруз, полоса зажата единицей', () => {
  assert.deepEqual(vuLevel(1.4), { ratio: 1, overload: true })
  assert.deepEqual(vuLevel(3), { ratio: 1, overload: true })
})

test('пустое значение — тишина, без исключений', () => {
  assert.deepEqual(vuLevel(null), { ratio: 0, overload: false })
  assert.deepEqual(vuLevel(undefined), { ratio: 0, overload: false })
  assert.deepEqual(vuLevel(NaN), { ratio: 0, overload: false })
})

test('отрицательное значение не выбивается за 0', () => {
  assert.deepEqual(vuLevel(-0.3), { ratio: 0, overload: false })
})
