export interface AdminConsoleTab {
  key: string;
  label: string;
}

export const ADMIN_CONSOLE_TABS: readonly AdminConsoleTab[] = [
  { key: 'marketplace-approvals', label: 'Marketplace Approvals' },
  { key: 'users', label: 'Users' },
  { key: 'tenants', label: 'Tenants' },
] as const;
