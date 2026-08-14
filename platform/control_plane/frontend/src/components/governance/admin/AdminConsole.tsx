// AdminConsole — the standalone admin hub at /admin (Epic 15). It owns the page
// chrome (heading + tab bar) and an internal tab registry (ADMIN_CONSOLE_TABS);
// today the single tab is "Marketplace Approvals", whose body is the existing
// MarketplaceAdmin panel (wrapper-trimmed, behavior unchanged). Adding a future
// admin tool = append a registry entry + a body case below. The console
// self-gates (role_level >= 2 via useUser) — the route is convenience, not a
// security boundary; the backend admin routes are the real ADMIN gate.

import { useState } from 'react';
import { useUser } from '../../../contexts/UserContext';
import { ADMIN_CONSOLE_TABS } from './adminConsoleTabs';
import MarketplaceAdmin from '../marketplace/MarketplaceAdmin';
import UsersAdmin from './UsersAdmin';
import TenantsAdmin from './TenantsAdmin';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

export default function AdminConsole() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const [activeKey, setActiveKey] = useState<string>(ADMIN_CONSOLE_TABS[0].key);

  // -- non-admin cosmetic gate ----------------------------------------------
  if (!isAdmin) {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-3xl mx-auto px-6 py-10">
          <div className={`${CARD} p-8 text-center`}>
            <h1 className="text-lg font-semibold text-slate-800">Not authorized</h1>
            <p className="text-sm text-slate-500 mt-1.5">
              The Admin console is available to administrators only.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-6">
        <h1 className="text-xl font-semibold text-slate-900">Admin</h1>

        {/* Pill tab bar (Marketplace.tsx idiom: active tint + shadow-sm pill,
            transition-colors only, no scale/translate motion). */}
        <div className="flex items-center gap-1 p-1 mt-4 bg-slate-100/80 rounded-xl w-fit overflow-x-auto">
          {ADMIN_CONSOLE_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveKey(tab.key)}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${activeKey === tab.key ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Active tab body. */}
        <div className="mt-4">
          {activeKey === 'marketplace-approvals' && <MarketplaceAdmin />}
          {activeKey === 'users' && <UsersAdmin />}
          {activeKey === 'tenants' && <TenantsAdmin />}
        </div>
      </div>
    </div>
  );
}
