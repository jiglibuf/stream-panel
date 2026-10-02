// Иконки PWA: рисуем сами и собираем PNG вручную (байты + zlib), без внешних
// зависимостей. Скрипт же проверяет, что получился валидный PNG нужного размера.
import { deflateSync, inflateSync } from 'node:zlib'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT_DIR = resolve(HERE, '..', 'public', 'icons')

const BG = [15, 18, 22]
const RED = [229, 72, 77]
const WHITE = [242, 245, 248]

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

function crc32(buffer) {
  let c = 0xffffffff
  for (let i = 0; i < buffer.length; i += 1) c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length, 0)
  const body = Buffer.concat([Buffer.from(type, 'latin1'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body), 0)
  return Buffer.concat([length, body, crc])
}

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

function encodePng(size, rgba) {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0) // width
  header.writeUInt32BE(size, 4) // height
  header[8] = 8   // бит на канал
  header[9] = 6   // цветовая модель RGBA
  header[10] = 0  // сжатие
  header[11] = 0  // фильтрация
  header[12] = 0  // без чередования

  const stride = size * 4
  const raw = Buffer.alloc((stride + 1) * size)
  for (let y = 0; y < size; y += 1) {
    raw[y * (stride + 1)] = 0 // фильтр строки: none
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride)
  }

  return Buffer.concat([
    SIGNATURE,
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

// Покрытие пикселя кругом радиуса r: сглаженный край шириной около пикселя.
function coverage(distance, radius) {
  const edge = 1
  if (distance <= radius - edge) return 1
  if (distance >= radius + edge) return 0
  return (radius + edge - distance) / (2 * edge)
}

function mix(base, over, alpha) {
  return [
    Math.round(base[0] + (over[0] - base[0]) * alpha),
    Math.round(base[1] + (over[1] - base[1]) * alpha),
    Math.round(base[2] + (over[2] - base[2]) * alpha),
  ]
}

function drawIcon(size) {
  const rgba = Buffer.alloc(size * size * 4)
  const center = (size - 1) / 2
  const redRadius = size * 0.24
  const ringRadius = size * 0.33
  const ringWidth = Math.max(3, size * 0.03)

  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const distance = Math.hypot(x - center, y - center)
      let color = BG
      color = mix(color, RED, coverage(distance, redRadius))
      const ring = Math.max(0, coverage(distance, ringRadius + ringWidth) - coverage(distance, ringRadius))
      color = mix(color, WHITE, Math.min(1, ring))
      const offset = (y * size + x) * 4
      rgba[offset] = color[0]
      rgba[offset + 1] = color[1]
      rgba[offset + 2] = color[2]
      rgba[offset + 3] = 255
    }
  }
  return rgba
}

// Проверка результата: сигнатура, размеры из IHDR и распаковка IDAT.
function verify(path, expected) {
  const data = readFileSync(path)
  if (!data.subarray(0, 8).equals(SIGNATURE)) throw new Error(`${path}: плохая сигнатура PNG`)
  if (data.readUInt32BE(8) !== 13) throw new Error(`${path}: неожиданный IHDR`)
  const width = data.readUInt32BE(16)
  const height = data.readUInt32BE(20)
  if (width !== expected || height !== expected) {
    throw new Error(`${path}: размер ${width} на ${height}, ожидался ${expected}`)
  }
  let offset = 8
  const idat = []
  while (offset < data.length) {
    const length = data.readUInt32BE(offset)
    const type = data.subarray(offset + 4, offset + 8).toString('latin1')
    if (type === 'IDAT') idat.push(data.subarray(offset + 8, offset + 8 + length))
    offset += 12 + length
  }
  const raw = inflateSync(Buffer.concat(idat))
  if (raw.length !== (expected * 4 + 1) * expected) throw new Error(`${path}: распакованные данные неполные`)
  return { width, height, bytes: data.length }
}

mkdirSync(OUT_DIR, { recursive: true })

for (const size of [192, 512]) {
  const path = resolve(OUT_DIR, `icon-${size}.png`)
  writeFileSync(path, encodePng(size, drawIcon(size)))
  const info = verify(path, size)
  console.log(`${path}: PNG ${info.width}x${info.height}, ${info.bytes} байт — валиден`)
}
