import { Link, useLocation } from 'react-router-dom';
import { useRef, useEffect, useState, type ReactNode } from 'react';
import { useAuth } from '../auth/useAuth';
import { useUser } from '../contexts/useUser';
import { GOVERNANCE_NAV, visibleNavItems } from './governance/agentsNav';
import { emailAlias } from './governance/agentBadges';
import { initialsFor } from './shared/initials';
import { tenantNames } from './governance/tenantUi';
import { OPERATIONS_NAV } from './operations/operationsNav';
import { sectionFromPath, SECTION_THEME } from './operations/operationsTheme';
import { SoonTag } from './shared/comingSoon';
import { COMING_SOON_TITLE } from './shared/comingSoonCopy';
import { marketplaceApi } from '../api/client';
import { safeLocalStorage } from '../api/safeLocalStorage';
import { getUiFlavor, setUiFlavor } from '../ui/uiPreference';
import { GITHUB_CONNECTION_SECTION_ID, ROLE_LEVEL_ADMIN } from './operations/settingsSections';

export default function Sidebar() {
  const location = useLocation();
  const { signOut, user } = useAuth();
  const { user: currentUser } = useUser();

  const section = sectionFromPath(location.pathname);
  const theme = SECTION_THEME[section];
  const nav = section === 'operations' ? OPERATIONS_NAV : GOVERNANCE_NAV;

  const isAdmin = (currentUser?.role_level ?? 0) >= ROLE_LEVEL_ADMIN;

  const displayName = currentUser?.name?.trim() || emailAlias(currentUser?.email);
  const displayRole = currentUser?.role ?? '';
  const displayTenants = tenantNames(currentUser?.tenants ?? []);
  const [profileOpen, setProfileOpen] = useState(false);
  const [isCollapsed, setIsCollapsed] = useState(false);

  // Rationale: frontend/README.md "The UI is Classic, and that is deliberate" (parked Cloudscape arm)
  const [uiFlavor, setUiFlavorState] = useState(getUiFlavor);

  const [pendingApprovals, setPendingApprovals] = useState<number | null>(null);
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    marketplaceApi
      .metrics()
      .then((m) => {
        if (!cancelled) setPendingApprovals(m.pending);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [isAdmin]);

  const [governanceExpanded, setGovernanceExpanded] = useState<Record<string, boolean>>(() => {
    const defaults: Record<string, boolean> = {};
    for (const sec of GOVERNANCE_NAV) {
      if (sec.collapsible) defaults[sec.id] = true;
    }
    const raw = safeLocalStorage.getItem('sidebar.governance.expanded');
    if (!raw) return defaults;
    try {
      return { ...defaults, ...JSON.parse(raw) };
    } catch {
      return defaults;
    }
  });
  useEffect(() => {
    safeLocalStorage.setItem('sidebar.governance.expanded', JSON.stringify(governanceExpanded));
  }, [governanceExpanded]);

  const toggleGovernanceSection = (id: string) => {
    setGovernanceExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const [operationsExpanded, setOperationsExpanded] = useState<Record<string, boolean>>(() => {
    const defaults: Record<string, boolean> = {};
    for (const sec of OPERATIONS_NAV) {
      if (sec.collapsible) defaults[sec.id] = true;
    }
    const raw = safeLocalStorage.getItem('sidebar.operations.expanded');
    if (!raw) return defaults;
    try {
      return { ...defaults, ...JSON.parse(raw) };
    } catch {
      return defaults;
    }
  });
  useEffect(() => {
    safeLocalStorage.setItem('sidebar.operations.expanded', JSON.stringify(operationsExpanded));
  }, [operationsExpanded]);

  const toggleOperationsSection = (id: string) => {
    setOperationsExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const expanded = section === 'operations' ? operationsExpanded : governanceExpanded;
  const toggleSection = section === 'operations' ? toggleOperationsSection : toggleGovernanceSection;

  const profileRef = useRef<HTMLDivElement>(null);

  const isActive = (path: string) => location.pathname === path;
  const isActivePrefix = (path: string) => location.pathname.startsWith(path);
  const isSectionRoot = (to: string) => to === '/' || to === '/ops';

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setProfileOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const navLink = (to: string, label: string, icon: string, active: boolean, badge?: ReactNode, comingSoon?: boolean) => (
    <Link
      to={to}
      className={`relative flex items-center gap-2.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${active ? theme.navActive : theme.navInactive} ${active && !isCollapsed ? `before:absolute before:left-0 before:top-1 before:bottom-1 before:w-0.5 before:rounded-full ${theme.navActiveBar}` : ''}`}
      title={isCollapsed ? label : ''}
    >
      <svg className={`w-4 h-4 flex-shrink-0 ${active ? theme.navIconActive : theme.navIconInactive}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d={icon} />
      </svg>
      {!isCollapsed && <span className="truncate">{label}</span>}
      {!isCollapsed && comingSoon && <SoonTag tone={section === 'operations' ? 'dark' : 'light'} />}
      {!isCollapsed && badge}
    </Link>
  );

  const subLink = (to: string, label: string, active: boolean) => (
    !isCollapsed && (
      <Link to={to} className={`block pl-9 pr-3 py-1.5 rounded-lg text-sm transition-colors ${active ? theme.subActive : theme.subInactive}`}>
        {label}
      </Link>
    )
  );

  return (
    <>
      <aside className={`flex-shrink-0 h-screen sticky top-0 flex flex-col ${theme.asideClassName} z-10 transition-all duration-300 relative ${isCollapsed ? 'w-16' : 'w-60'}`} style={{
        background: theme.asideBackground,
      }}>
        <button
          onClick={() => setIsCollapsed(!isCollapsed)}
          className={`absolute z-20 h-10 flex items-center justify-center group border-l ${theme.collapseBorder} transition-all duration-300 ${isCollapsed ? 'top-20 -right-6 rounded-r-lg shadow-md' : 'top-5 right-0'}`}
          style={{
            width: '32px',
            background: isCollapsed ? theme.collapseBgCollapsed : theme.collapseBg,
            borderTopLeftRadius: isCollapsed ? '0' : '8px',
            borderBottomLeftRadius: isCollapsed ? '0' : '8px',
          }}
          title={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <svg className={`w-3.5 h-3.5 ${theme.collapseChevron} transition-all duration-300 ${isCollapsed ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
        </button>

        <div className={`px-5 pt-6 pb-2 border-b ${theme.logoBorder} flex items-center ${isCollapsed ? 'justify-center' : 'justify-start'}`}>
          <Link to={section === 'operations' ? '/ops' : '/'} className="flex items-center gap-2.5" aria-label={theme.logoTitle}>
            <span className={`flex-shrink-0 h-8 w-8 rounded-lg ${theme.logoBadge} flex items-center justify-center text-[11px] font-bold tracking-tight`}>
              AGP
            </span>
            {!isCollapsed && (
              <span className={`text-sm font-semibold ${theme.logoText} leading-tight tracking-tight`}>
                {theme.logoTitle}
              </span>
            )}
          </Link>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-1">
          {nav.map((sec, i) => {
                const isOpen = !sec.collapsible || expanded[sec.id];
                const prev = nav[i - 1];
                const showDivider = i > 0 && prev.collapsible !== sec.collapsible;
                return (
                  <div
                    key={sec.id}
                    className={[
                      sec.label ? 'pt-3' : '',
                      showDivider ? `border-t ${theme.divider} mt-2 pt-2` : '',
                    ].filter(Boolean).join(' ')}
                  >
                    {!isCollapsed && sec.label && (
                      sec.collapsible ? (
                        <button
                          onClick={() => toggleSection(sec.id)}
                          className="w-full flex items-center px-3 pb-1 group"
                          aria-expanded={isOpen}
                          aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${sec.label}`}
                        >
                          <span className={`text-[10px] font-semibold uppercase tracking-wider ${theme.groupHeader}`}>{sec.label}</span>
                        </button>
                      ) : (
                        <div className={`px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider ${theme.groupHeader}`}>{sec.label}</div>
                      )
                    )}
                    {(isCollapsed || isOpen) && visibleNavItems(sec.items, isAdmin).map((item) => {
                      const adminBadge =
                        item.to === '/admin' && pendingApprovals && pendingApprovals > 0 ? (
                          <span className="ml-auto inline-flex items-center rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">
                            {pendingApprovals > 99 ? '99+' : String(pendingApprovals)}
                          </span>
                        ) : undefined;
                      return (
                        <div key={item.to}>
                          {navLink(item.to, item.label, item.icon, isSectionRoot(item.to) ? isActive(item.to) : isActivePrefix(item.to), adminBadge, item.comingSoon)}
                          {!isCollapsed && (item.children ?? []).map((c) => subLink(c.to, c.label, isActivePrefix(c.to)))}
                        </div>
                      );
                    })}
                  </div>
                );
              })}
        </nav>

        <div className={`px-3 pt-2 pb-1 border-t ${theme.divider}`}>
          <Link
            to={theme.switchTo}
            title={theme.switchLabel}
            className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${theme.switchClassName} ${isCollapsed ? 'justify-center' : ''}`}
          >
            <svg className="w-4 h-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5" />
            </svg>
            {!isCollapsed && <span className="truncate">{section === 'governance' ? `${theme.switchLabel} →` : theme.switchLabel}</span>}
          </Link>
        </div>

        {!isCollapsed && (
          <div className={`px-3 pt-2 pb-2 border-t ${theme.divider}`}>
            <div className={`px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider ${theme.groupHeader}`}>
              Interface
            </div>
            <div className="grid grid-cols-2 gap-1" role="group" aria-label="Interface">
              <button
                type="button"
                onClick={() => {
                  setUiFlavor('classic');
                  setUiFlavorState('classic');
                }}
                aria-pressed={uiFlavor === 'classic'}
                className={`flex flex-col items-center justify-center px-2 py-1.5 rounded-lg text-xs font-medium transition-colors ${uiFlavor === 'classic' ? theme.navActive : theme.navInactive}`}
              >
                Classic
              </button>
              <button
                type="button"
                disabled
                aria-pressed={false}
                title="Cloudscape UI returns in a future release"
                className={`flex flex-col items-center justify-center px-2 py-1.5 rounded-lg border border-dashed ${theme.divider} ${theme.userSub} text-xs font-medium opacity-70 cursor-not-allowed`}
              >
                <span className="truncate">Cloudscape</span>
                <span className="text-[9px] font-normal uppercase tracking-wide">{COMING_SOON_TITLE}</span>
              </button>
            </div>
          </div>
        )}

        {user && (
          <div className={`border-t ${theme.userBorder} px-3 py-3`} ref={profileRef}>
            <button onClick={() => setProfileOpen(!profileOpen)}
              className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg ${theme.userButtonHover} transition-colors ${isCollapsed ? 'justify-center' : ''}`}
              title={displayRole ? `${displayName} — ${displayRole}` : displayName}>
              <div className={`w-7 h-7 rounded-full ${theme.userAvatar} text-xs font-semibold flex items-center justify-center flex-shrink-0`}>
                {initialsFor(displayName)}
              </div>
              {!isCollapsed && (
                <span className="min-w-0 flex-1 text-left">
                  <span className={`block text-sm font-medium ${theme.userName} truncate`}>{displayName}</span>
                  {displayRole && <span className={`block text-xs ${theme.userSub} capitalize truncate`}>{displayRole}</span>}
                  {displayTenants && (
                    <span className={`block text-xs ${theme.userSub} truncate`} title={displayTenants}>
                      Tenants: {displayTenants}
                    </span>
                  )}
                </span>
              )}
            </button>

            {profileOpen && (
              <div className="mt-1 bg-white rounded-xl border border-slate-200 py-1.5 shadow-lg">
                {currentUser && (
                  <div className="px-4 py-2 border-b border-slate-100">
                    <div className="text-xs font-semibold text-slate-900">{currentUser.email}</div>
                    <div className="text-xs text-slate-500 capitalize mt-0.5">Role: {currentUser.role}</div>
                  </div>
                )}
                <Link to={`/ops/settings#${GITHUB_CONNECTION_SECTION_ID}`} onClick={() => setProfileOpen(false)}
                  className="w-full flex items-center px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 gap-2.5">
                  <svg className="w-4 h-4 text-slate-400" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.009-.868-.014-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.203 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.749 0 .268.18.58.688.482A10.02 10.02 0 0022 12.017C22 6.484 17.522 2 12 2z" />
                  </svg>
                  Your GitHub account
                </Link>
                <button onClick={signOut}
                  className="w-full flex items-center px-4 py-2 text-sm text-red-600 hover:bg-red-50 gap-2.5 border-t border-slate-100">
                  <svg className="w-4 h-4 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15m3 0l3-3m0 0l-3-3m3 3H9" />
                  </svg>
                  Sign Out
                </button>
              </div>
            )}
          </div>
        )}
      </aside>
    </>
  );
}
