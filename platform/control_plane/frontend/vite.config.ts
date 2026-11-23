import { dirname, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { isArmModule, leakMessage, leaks, swapTable } from './src/auth/devauth/arm.ts'

const here = dirname(fileURLToPath(import.meta.url))

function authArmPlugin(mode: string): Plugin {
  const table = swapTable(mode)
  const swapping = Object.keys(table).length > 0
  let building = false
  return {
    name: 'agp-auth-arm',
    enforce: 'pre',
    configResolved(config) {
      building = config.command === 'build'
    },
    async resolveId(source, importer) {
      if (!swapping) return null
      const resolved = await this.resolve(source, importer, { skipSelf: true })
      if (!resolved) return null
      const twin = table[relative(here, resolved.id).split(sep).join('/')]
      return twin ? join(here, twin) : null
    },
    load(id) {
      if (swapping || !building) return null
      const rel = relative(here, id.split('?')[0]).split(sep).join('/')
      if (isArmModule(rel)) this.error(leakMessage(mode, rel))
      return null
    },
    generateBundle(_, bundle) {
      if (swapping) return
      for (const [fileName, output] of Object.entries(bundle)) {
        if (output.type === 'chunk' && leaks(output.code)) this.error(leakMessage(mode, fileName))
      }
    },
  }
}

export default defineConfig(({ mode }) => ({
  plugins: [react(), authArmPlugin(mode)],
}))
