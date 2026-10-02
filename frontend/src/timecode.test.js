// Показ таймкода из строки OBS: ничего не считаем сами, только выводим то,
// что прислал сервис. Доли секунды отбрасываются лишь для показа.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { formatTimecode, EMPTY_TIMECODE } from './timecode.js'

test('показывает часы:минуты:секунды без долей', () => {
  assert.equal(formatTimecode('00:01:23.456'), '00:01:23')
  assert.equal(formatTimecode('01:02:03'), '01:02:03')
})

test('обрезает пробелы вокруг строки', () => {
  assert.equal(formatTimecode('  00:00:05.000  '), '00:00:05')
})

test('пустое значение — заглушка', () => {
  assert.equal(formatTimecode(''), EMPTY_TIMECODE)
  assert.equal(formatTimecode('   '), EMPTY_TIMECODE)
  assert.equal(formatTimecode(null), EMPTY_TIMECODE)
  assert.equal(formatTimecode(undefined), EMPTY_TIMECODE)
})

test('мусор вместо таймкода — заглушка, без исключений', () => {
  assert.equal(formatTimecode('мусор'), EMPTY_TIMECODE)
  assert.equal(formatTimecode('12 минут'), EMPTY_TIMECODE)
  assert.equal(formatTimecode(42), EMPTY_TIMECODE)
})

test('длинный эфир не ломает формат', () => {
  assert.equal(formatTimecode('101:00:00.000'), '101:00:00')
})
