// Отдаёт компонент пульта в строку HTML для проверок рендера в `node --test`.
// Node не умеет JSX, а браузерного DOM в тестах нет: собираем компонент через
// esbuild (он уже стоит как зависимость Vite) в самодостаточный модуль и зовём
// react-dom/server. Никаких новых зависимостей не добавляется.
//
// Сборка — в CommonJS: сборка react-dom/server в ESM-виде спотыкается о
// встроенные модули Node («Dynamic require of 'util' is not supported»),
// а CJS-бандл грузится через createRequire без нареканий.
import { build } from 'esbuild'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const SRC_DIR = dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)

// path — путь к .jsx относительно frontend/src (он же корень разрешения импортов).
export async function renderComponent({ path, exportName, props = {} }) {
  const source = [
    "const { renderToStaticMarkup } = require('react-dom/server')",
    `const { ${exportName}: Component } = require(${JSON.stringify(path)})`,
    'module.exports.render = (p) => renderToStaticMarkup(Component(p))',
  ].join('\n')

  const result = await build({
    stdin: { contents: source, resolveDir: SRC_DIR, sourcefile: 'render-entry.jsx', loader: 'jsx' },
    bundle: true,
    write: false,
    format: 'cjs',
    platform: 'node',
    jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' },
    logLevel: 'silent',
  })

  const dir = mkdtempSync(join(tmpdir(), 'obs-panel-render-'))
  const file = join(dir, 'bundle.cjs')
  writeFileSync(file, result.outputFiles[0].text)
  return require(file).render(props)
}

export function countAttribute(html, attribute) {
  return html.split(`${attribute}=`).length - 1
}
