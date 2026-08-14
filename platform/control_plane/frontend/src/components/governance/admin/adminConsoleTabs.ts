// Admin console tab registry (Epic 15). Pure metadata so it's unit-testable
// under the repo's `src/**/*.test.ts`-only vitest rule; AdminConsole.tsx maps
// each key to its rendered body. Append an entry here (+ a body case in
// AdminConsole.tsx) to add a future admin tool — the sidebar never changes.
export interface AdminConsoleTab {
  key: string;
  label: string;
}

export const ADMIN_CONSOLE_TABS: readonly AdminConsoleTab[] = [
  { key: 'marketplace-approvals', label: 'Marketplace Approvals' },
  { key: 'users', label: 'Users' },
  { key: 'tenants', label: 'Tenants' },
] as const;
