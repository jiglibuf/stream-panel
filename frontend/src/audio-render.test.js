// Рендер блока «Звук» в статическую разметку: важно проверить состояния,
// которые видит владелец (пустой список, недоступный сервис, проблемы).
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { DEVICES_EMPTY_TEXT } from './audio.js'
import { renderComponent, countAttribute } from './render-jsx.mjs'

const noop = () => {}

const AUDIO = {
  mode: 'stream',
  service: { enabled: true, active: true },
  default_sink: 'rz_mix',
  chosen: 'alsa_output.a',
  devices: [
    { name: 'rz_mix', label: 'rz_mix', kind: 'virtual', state: 'RUNNING', is_default: true, is_chosen: false },
    { name: 'alsa_output.a', label: 'Гарнитура A20', kind: 'physical', state: 'SUSPENDED', is_default: false, is_chosen: true },
    { name: 'alsa_output.b', label: 'Колонки', kind: 'physical', state: 'RUNNING', is_default: true, is_chosen: false },
  ],
  obs: { online: true, streaming: false, recording: false },
  issues: ['петля rz_mix.monitor играет в два устройства: колонки, гарнитура'],
}

function render(overrides = {}) {
  return renderComponent({
    path: './parts.jsx',
    exportName: 'AudioMixPanel',
    props: {
      audio: AUDIO,
      online: true,
      busy: false,
      notice: null,
      pendingMode: null,
      onSetMode: noop,
      onConfirmMode: noop,
      onCancelMode: noop,
      onSetDevice: noop,
      onDismissNotice: noop,
      ...overrides,
    },
  })
}

test('пустой список устройств — подсказка вместо строк', async () => {
  const html = await render({ audio: { ...AUDIO, devices: [] } })
  assert.equal(countAttribute(html, 'data-device'), 0)
  assert.ok(html.includes(DEVICES_EMPTY_TEXT))
})

test('список устройств — только физические, выбранное отмечено', async () => {
  const html = await render()
  assert.equal(countAttribute(html, 'data-device'), 2) // виртуальный выход скрыт
  assert.ok(html.includes('Гарнитура A20'))
  assert.ok(html.includes('Колонки'))
  assert.ok(!html.includes('data-device="rz_mix"'))
  assert.ok(html.includes('aria-checked="true"'))
  assert.ok(html.includes('по умолчанию'))
})

test('сервис не отвечает — баннер и пустой список без строк', async () => {
  const html = await render({ audio: null, online: false })
  assert.ok(html.includes('сервис не отвечает'))
  assert.equal(countAttribute(html, 'data-device'), 0)
  assert.ok(html.includes(DEVICES_EMPTY_TEXT))
})

test('проблемы выводятся обычным текстом, пустой список — без блока проблем', async () => {
  const withIssues = await render()
  assert.ok(withIssues.includes('петля rz_mix.monitor играет в два устройства'))
  const clean = await render({ audio: { ...AUDIO, issues: [] } })
  assert.ok(!clean.includes('issues__item'))
})

test('при эфире показывается подтверждение, а не сразу переключение', async () => {
  const html = await render({
    audio: { ...AUDIO, obs: { online: true, streaming: true, recording: false } },
    pendingMode: 'normal',
  })
  assert.ok(html.includes('звук прервётся'))
  assert.ok(html.includes('Переключить'))
  assert.ok(html.includes('Отмена'))
})
