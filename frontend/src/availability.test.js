// Доступность кнопок по снапшоту. Правила:
//   obs=false  -> недоступно всё (требование F1);
//   stale=true -> недоступны только разрушительные;
//   replay=false -> «Сохранить повтор» недоступна;
//   эфир идёт -> «Начать эфир» недоступна, эфира нет -> недоступлен «Стоп эфира».
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { availability, DESTRUCTIVE } from './availability.js'

const FRESH = {
  obs: true, stale: false, replay: true,
  stream: { active: false },
}

const KEYS = ['scenes', 'streamStart', 'streamStop', 'record', 'replaySave',
  'panic', 'micMute', 'micVolume', 'sysMute', 'sysVolume']

test('набор разрушительных команд совпадает с контрактом', () => {
  assert.deepEqual([...DESTRUCTIVE].sort(),
    ['record.toggle', 'replay.save', 'stream.start', 'stream.stop'])
})

test('obs=false — недоступно всё', () => {
  const result = availability({ ...FRESH, obs: false, stream: { active: true } })
  for (const key of KEYS) assert.equal(result[key], false, `${key} должно быть false`)
})

test('obs=false перебивает свежие данные', () => {
  const result = availability({ obs: false, stale: false, replay: true, stream: { active: false } })
  assert.equal(result.scenes, false)
  assert.equal(result.panic, false)
})

test('свежее состояние, эфира нет — доступно всё, кроме «Стоп эфира»', () => {
  const result = availability(FRESH)
  assert.equal(result.streamStop, false)
  for (const key of KEYS.filter((k) => k !== 'streamStop')) {
    assert.equal(result[key], true, `${key} должно быть true`)
  }
})

test('свежее состояние, эфир идёт — доступно всё, кроме «Начать эфир»', () => {
  const result = availability({ ...FRESH, stream: { active: true } })
  assert.equal(result.streamStart, false)
  for (const key of KEYS.filter((k) => k !== 'streamStart')) {
    assert.equal(result[key], true, `${key} должно быть true`)
  }
})

test('stale=true блокирует только разрушительные', () => {
  const result = availability({ ...FRESH, stale: true })
  assert.equal(result.streamStart, false)
  assert.equal(result.streamStop, false)
  assert.equal(result.record, false)
  assert.equal(result.replaySave, false)
  assert.equal(result.scenes, true)
  assert.equal(result.panic, true)
  assert.equal(result.micMute, true)
  assert.equal(result.sysVolume, true)
})

test('эфир идёт — старт недоступен, стоп доступен', () => {
  const result = availability({ ...FRESH, stream: { active: true } })
  assert.equal(result.streamStart, false)
  assert.equal(result.streamStop, true)
})

test('эфира нет — старт доступен, стоп недоступен', () => {
  const result = availability({ ...FRESH, stream: { active: false } })
  assert.equal(result.streamStart, true)
  assert.equal(result.streamStop, false)
})

test('буфер повтора выключен — сохранение недоступно', () => {
  assert.equal(availability({ ...FRESH, replay: false }).replaySave, false)
  assert.equal(availability({ ...FRESH, replay: true }).replaySave, true)
})

test('пустой или битый снапшот не роняет разбор', () => {
  for (const value of [null, undefined, {}, { stream: null }]) {
    const result = availability(value)
    for (const key of KEYS) assert.equal(result[key], false, `${key} при ${JSON.stringify(value)}`)
  }
})
