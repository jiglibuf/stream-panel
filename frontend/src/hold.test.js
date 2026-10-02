// Решение по удержанию кнопки: по прошедшему времени и порогу 1.5 с.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { holdResult, HOLD_MS } from './hold.js'

test('порог по умолчанию — 1500 мс', () => {
  assert.equal(HOLD_MS, 1500)
})

test('до начала удержания — idle, прогресс 0', () => {
  assert.deepEqual(holdResult(0), { status: 'idle', progress: 0 })
  assert.deepEqual(holdResult(-50), { status: 'idle', progress: 0 })
  assert.deepEqual(holdResult(NaN), { status: 'idle', progress: 0 })
  assert.deepEqual(holdResult(undefined), { status: 'idle', progress: 0 })
})

test('в процессе — holding, прогресс растёт', () => {
  assert.deepEqual(holdResult(750), { status: 'holding', progress: 0.5 })
  const late = holdResult(1499)
  assert.equal(late.status, 'holding')
  assert.ok(late.progress > 0.99 && late.progress < 1)
})

test('порог достигнут — done, прогресс 1', () => {
  assert.deepEqual(holdResult(1500), { status: 'done', progress: 1 })
  assert.deepEqual(holdResult(5000), { status: 'done', progress: 1 })
})

test('порог можно задать иным', () => {
  assert.deepEqual(holdResult(750, 1000), { status: 'holding', progress: 0.75 })
  assert.deepEqual(holdResult(1000, 1000), { status: 'done', progress: 1 })
})

test('прогресс всегда в диапазоне 0..1', () => {
  for (const value of [-1000, 0, 1, 999, 1500, 100000]) {
    const { progress } = holdResult(value)
    assert.ok(progress >= 0 && progress <= 1, `прогресс вне диапазона: ${progress}`)
  }
})
