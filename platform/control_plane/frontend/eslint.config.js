import comments from '@eslint-community/eslint-plugin-eslint-comments/configs'
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import suppressionTail from './eslint-rules/suppressionTail.js'

const agp = { rules: { 'suppression-tail': suppressionTail } }

const plainJs = {
  extends: [js.configs.recommended],
  plugins: { ...comments.recommended.plugins, agp },
  languageOptions: { globals: globals.node },
  linterOptions: {
    reportUnusedDisableDirectives: 'error',
  },
  rules: {
    '@eslint-community/eslint-comments/require-description': ['error', { ignore: [] }],
    'agp/suppression-tail': 'error',
    '@eslint-community/eslint-comments/no-restricted-disable': ['error', 'agp/*', '@eslint-community/eslint-comments/*'],
    '@eslint-community/eslint-comments/no-use': ['error', { allow: ['eslint-disable', 'eslint-disable-line', 'eslint-disable-next-line', 'eslint-enable'] }],
  },
}

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx,mts,cts}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
      comments.recommended,
    ],
    plugins: { agp },
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    linterOptions: {
      reportUnusedDisableDirectives: 'error',
    },
    rules: {
      'react-hooks/exhaustive-deps': ['error', { additionalHooks: '(useLoad)' }],
      '@eslint-community/eslint-comments/require-description': ['error', { ignore: [] }],
      'agp/suppression-tail': 'error',
      '@eslint-community/eslint-comments/no-restricted-disable': ['error', 'agp/*', '@eslint-community/eslint-comments/*'],
      '@eslint-community/eslint-comments/no-use': ['error', { allow: ['eslint-disable', 'eslint-disable-line', 'eslint-disable-next-line', 'eslint-enable'] }],
      '@typescript-eslint/switch-exhaustiveness-check': ['error', { allowDefaultCaseForExhaustiveSwitch: false, considerDefaultExhaustiveForUnions: false, requireDefaultForNonUnion: true }],
    },
  },
  { files: ['eslint-rules/**/*.js'], ...plainJs },
  { files: ['*.{js,mjs,cjs}'], ...plainJs },
])
