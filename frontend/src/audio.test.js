// Блок «Звук»: чистые правила — подписи режимов, состояние устройств, порядок
// списка, текст подтверждения при эфире, доступность элементов. Всё проверяется
// на данных, без DOM: так же устроены остальные помощники пульта.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  MODE_STREAM,
  MODE_NORMAL,
  MODE_CONFIRM_TEXT,
  DEVICES_EMPTY_TEXT,
  modeLabel,
  isModeActive,
  shouldRequestMode,
  confirmText,
  needsConfirm,
  modeRequest,
  deviceRequest,
  deviceLabel,
  deviceStateText,
  isSelectableDevice,
  sortDevices,
  statusLine,
  serviceText,
  issuesList,
  audioAvailability,
} from './audio.js'

// Снимок как его отдаёт GET /api/audio (ТЗ, раздел 3). В списке есть все три
// вида устройств: виртуальные и мониторы в выборе не участвуют.
const AUDIO = {
  mode: 'stream',
  service: { enabled: true, active: true },
  default_sink: 'rz_mix',
  chosen: 'alsa_output.a',
  devices: [
    { name: 'rz_mix', label: 'rz_mix', kind: 'virtual', state: 'RUNNING', is_default: true, is_chosen: false },
    { name: 'alsa_output.a', label: 'Гарнитура A20', kind: 'physical', state: 'SUSPENDED', is_default: false, is_chosen: true },
    { name: 'alsa_output.b', label: 'Колонки', kind: 'physical', state: 'RUNNING', is_default: false, is_chosen: false },
    { name: 'rz_mix.monitor', label: 'rz_mix monitor', kind: 'monitor', state: 'RUNNING', is_default: false, is_chosen: false },
  ],
  mixes: { present: true, loops: [] },
  obs: { online: true, streaming: false, recording: false },
  issues: ['петля rz_mix.monitor играет в два устройства: колонки, гарнитура'],
}

// ── режимы ────────────────────────────────────────────────────────────────

test('подписи режимов — по-русски, неизвестный режим не выдумывает название', () => {
  assert.equal(modeLabel(MODE_STREAM), 'Стрим')
  assert.equal(modeLabel(MODE_NORMAL), 'Обычный')
  assert.equal(modeLabel('weird'), 'неизвестно')
  assert.equal(modeLabel(null), 'неизвестно')
})

test('активный режим определяется по снимку сервера (не по памяти клиента)', () => {
  assert.equal(isModeActive(AUDIO, MODE_STREAM), true)
  assert.equal(isModeActive(AUDIO, MODE_NORMAL), false)
  assert.equal(isModeActive({ ...AUDIO, mode: 'normal' }, MODE_NORMAL), true)
  assert.equal(isModeActive(null, MODE_STREAM), false)
})

test('повторный тап по активному режиму ничего не отправляет', () => {
  assert.equal(shouldRequestMode(AUDIO, MODE_STREAM), false) // он и так включён
  assert.equal(shouldRequestMode(AUDIO, MODE_NORMAL), true)
  // Режим неизвестен (например, служба остановлена вручную) — вслепую не шлём.
  assert.equal(shouldRequestMode({ ...AUDIO, mode: 'unknown' }, MODE_NORMAL), false)
  assert.equal(shouldRequestMode(null, MODE_NORMAL), false)
})

test('решение о подтверждении: эфир или запись — спрашиваем', () => {
  assert.equal(needsConfirm({ obs: { streaming: true, recording: false } }), true)
  assert.equal(needsConfirm({ obs: { streaming: false, recording: true } }), true)
  assert.equal(needsConfirm(AUDIO), false)
  assert.equal(needsConfirm(null), false)
})

test('текст подтверждения называет эфир и предупреждает про обрыв звука', () => {
  const live = confirmText({ obs: { streaming: true } })
  assert.equal(live, MODE_CONFIRM_TEXT)
  assert.ok(live.includes('эфир'))
  assert.ok(live.includes('звук прервётся'))
  assert.ok(live.includes('переключить'))
})

test('идёт запись (без эфира) — подтверждение говорит про запись', () => {
  const rec = confirmText({ obs: { streaming: false, recording: true } })
  assert.ok(rec.includes('запись'))
  assert.ok(rec.includes('звук прервётся'))
})

test('подтверждение не нужно — текста нет', () => {
  assert.equal(confirmText(AUDIO), null)
  assert.equal(confirmText(null), null)
})

test('тело запроса режима несёт явный признак подтверждения', () => {
  assert.deepEqual(modeRequest(MODE_STREAM, true), { mode: 'stream', confirm: true })
  assert.deepEqual(modeRequest(MODE_NORMAL), { mode: 'normal', confirm: false })
})

test('тело запроса устройства — только имя', () => {
  assert.deepEqual(deviceRequest('alsa_output.b'), { name: 'alsa_output.b' })
})

// ── устройства ────────────────────────────────────────────────────────────

test('подпись устройства: человекочитаемая метка, иначе имя', () => {
  assert.equal(deviceLabel({ name: 'alsa_output.a', label: 'Гарнитура A20' }), 'Гарнитура A20')
  assert.equal(deviceLabel({ name: 'alsa_output.a' }), 'alsa_output.a')
  assert.equal(deviceLabel({ name: 'alsa_output.a', label: '' }), 'alsa_output.a')
  assert.equal(deviceLabel(null), '—')
})

test('состояние устройства — словами владельца', () => {
  assert.equal(deviceStateText('RUNNING'), 'работает')
  assert.equal(deviceStateText('SUSPENDED'), 'спит')
  assert.equal(deviceStateText('IDLE'), 'спит')
  assert.equal(deviceStateText(''), 'состояние неизвестно')
  assert.equal(deviceStateText(null), 'состояние неизвестно')
  assert.equal(deviceStateText('WOBBLE'), 'состояние неизвестно')
})

test('в выборе участвуют только физические выходы', () => {
  assert.equal(isSelectableDevice({ kind: 'physical' }), true)
  assert.equal(isSelectableDevice({ kind: 'virtual' }), false)
  assert.equal(isSelectableDevice({ kind: 'monitor' }), false)
  assert.equal(isSelectableDevice(null), false)
})

test('список устройств: только физические, выбранное сверху', () => {
  const rows = sortDevices(AUDIO.devices)
  assert.deepEqual(rows.map((d) => d.name), ['alsa_output.a', 'alsa_output.b'])
  // Исходный массив не портим.
  assert.equal(AUDIO.devices.length, 4)
})

test('порядок внутри списка: выбранное → по умолчанию → работающее → имя', () => {
  const rows = sortDevices([
    { name: 'c', label: 'Ц', kind: 'physical', state: 'SUSPENDED', is_default: false, is_chosen: false },
    { name: 'b', label: 'Б', kind: 'physical', state: 'RUNNING', is_default: false, is_chosen: false },
    { name: 'a', label: 'А', kind: 'physical', state: 'SUSPENDED', is_default: false, is_chosen: true },
    { name: 'd', label: 'Д', kind: 'physical', state: 'RUNNING', is_default: true, is_chosen: false },
  ])
  assert.deepEqual(rows.map((d) => d.name), ['a', 'd', 'b', 'c'])
})

test('пустой или битый список устройств не роняет разбор', () => {
  assert.deepEqual(sortDevices([]), [])
  assert.deepEqual(sortDevices(null), [])
  assert.deepEqual(sortDevices([{ kind: 'virtual' }]), [])
})

// ── состояние и проблемы ──────────────────────────────────────────────────

test('строка состояния: режим, выход по умолчанию, состояние службы', () => {
  const line = statusLine(AUDIO)
  assert.ok(line.includes('Режим: Стрим'))
  assert.ok(line.includes('rz_mix'))
  assert.ok(line.includes('работает'))
  assert.equal(statusLine(null), 'состояние звука неизвестно')
  assert.equal(statusLine({ mode: 'nope' }), 'состояние звука неизвестно')
})

test('состояние службы словами во всех четырёх сочетаниях', () => {
  assert.equal(serviceText({ enabled: true, active: true }), 'работает')
  assert.equal(serviceText({ enabled: true, active: false }), 'включена, но не работает')
  assert.equal(serviceText({ enabled: false, active: false }), 'выключена')
  // Сторож можно поднять руками — режим всё равно «Стрим», и это надо назвать.
  assert.equal(serviceText({ enabled: false, active: true }), 'запущена вручную')
  assert.equal(serviceText(null), 'выключена')
})

test('issues — обычный текст, мусор и пустое отбрасываем', () => {
  assert.deepEqual(issuesList(AUDIO), ['петля rz_mix.monitor играет в два устройства: колонки, гарнитура'])
  assert.deepEqual(issuesList({ issues: ['  пробелы  ', '', 42, null, 'норм'] }), ['пробелы', 'норм'])
  assert.deepEqual(issuesList({}), [])
  assert.deepEqual(issuesList(null), [])
})

// ── доступность элементов ─────────────────────────────────────────────────

test('сервис не отвечает — режим и выбор устройства недоступны', () => {
  assert.deepEqual(audioAvailability(null, false), { mode: false, device: false })
  assert.deepEqual(audioAvailability(AUDIO, false), { mode: false, device: false })
  // Первый опрос ещё не вернулся — элементы ждут.
  assert.deepEqual(audioAvailability(null, null), { mode: false, device: false })
})

test('сервис на связи — выбор устройства доступен', () => {
  assert.deepEqual(audioAvailability(AUDIO, true), { mode: true, device: true })
})

test('OBS офлайн — переключение режима недоступно, выбор устройства остаётся', () => {
  const offline = { ...AUDIO, obs: { online: false } }
  assert.deepEqual(audioAvailability(offline, true), { mode: false, device: true })
})

test('текст пустого списка устройств — непустая строка', () => {
  assert.equal(typeof DEVICES_EMPTY_TEXT, 'string')
  assert.ok(DEVICES_EMPTY_TEXT.length > 0)
})
