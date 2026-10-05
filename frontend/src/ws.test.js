// Сборка адреса WebSocket и разбор входящих сообщений.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { wsUrl, parseMessage } from './ws.js'

test('http -> ws, https -> wss', () => {
  assert.equal(wsUrl({ protocol: 'http:', host: 'panel.local:8090' }, 'abc'),
    'ws://panel.local:8090/ws?token=abc')
  assert.equal(wsUrl({ protocol: 'https:', host: 'obs-host:8090' }, 'abc'),
    'wss://obs-host:8090/ws?token=abc')
})

test('токен кодируется как параметр запроса', () => {
  assert.equal(wsUrl({ protocol: 'http:', host: 'host:8090' }, 'a b&c=d'),
    'ws://host:8090/ws?token=a%20b%26c%3Dd')
})

test('разбор state', () => {
  const snap = { obs: true, scene: 'WoW' }
  assert.deepEqual(parseMessage(JSON.stringify({ type: 'state', state: snap })),
    { kind: 'state', state: snap })
})

test('разбор pong с тем же t', () => {
  assert.deepEqual(parseMessage('{"type":"pong","t":172777}'),
    { kind: 'pong', t: 172777 })
})

test('прочие типы — other, не падаем', () => {
  assert.deepEqual(parseMessage('{"type":"ping","t":1}'), { kind: 'other' })
  assert.deepEqual(parseMessage('{"type":"unknown"}'), { kind: 'other' })
})

test('мусор — invalid, не падаем', () => {
  for (const raw of ['', 'не json', '{', 'null', '42', '[]', '"строка"']) {
    assert.deepEqual(parseMessage(raw), { kind: 'invalid' }, `сырое: ${raw}`)
  }
})
