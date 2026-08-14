/**
 * Section = which top-level platform you're in, derived purely from the URL
 * (Epic 17). No state/context — the Sidebar calls sectionFromPath(location.pathname)
 * to choose the nav config (OPERATIONS_NAV vs GOVERNANCE_NAV) and the theme.
 * The governance theme reproduces Sidebar.tsx's existing literals so Governance
 * is visually unchanged; operations is a lighter slate sidebar with emerald
 * accents and a matching emerald/teal-tinted page background.
 */
export type Section = 'governance' | 'operations';

export function sectionFromPath(pathname: string): Section {
  return pathname === '/ops' || pathname.startsWith('/ops/') ? 'operations' : 'governance';
}

export interface SectionTheme {
  asideClassName: string;       // extra classes on <aside> (borders)
  asideBackground: string;      // inline style `background`
  pageBackground: string;       // <main> ombre gradient (inline style `background`)
  logoBorder: string;           // logo container bottom border
  logoBadge: string;            // AGP square badge bg+text
  logoText: string;             // wordmark text color
  logoTitle: string;            // wordmark text + aria-label (per-section product name)
  navActive: string;            // active leaf link bg+text
  navInactive: string;          // inactive leaf link text+hover
  navActiveBar: string;         // active left accent bar color (before:bg-*)
  navIconActive: string;        // active leaf icon color
  navIconInactive: string;      // inactive leaf icon color
  groupHeader: string;          // collapsible group header label text
  subActive: string;            // active child link
  subInactive: string;          // inactive child link
  divider: string;              // inter-zone divider border color
  collapseBg: string;           // collapse-button bg (expanded state)
  collapseBgCollapsed: string;  // collapse-button bg (collapsed rail state)
  collapseBorder: string;       // collapse-button border
  collapseChevron: string;      // collapse-button chevron color+hover
  switchTo: string;             // reciprocal switch-link target
  switchLabel: string;          // switch-link text. Governance's trailing → is added in JSX (Task 6); operations bakes its leading ← in here.
  switchClassName: string;      // reciprocal switch-link styling
  userBorder: string;           // border above the user box
  userButtonHover: string;      // user button hover bg
  userAvatar: string;           // user avatar circle bg+text
  userName: string;             // user display-name text
  userSub: string;              // user role/sub text
}

export const SECTION_THEME: Record<Section, SectionTheme> = {
  governance: {
    asideClassName: 'border-r border-slate-200/40',
    asideBackground:
      'linear-gradient(180deg, rgba(239,246,255,0.95) 0%, rgba(238,242,255,0.9) 40%, rgba(245,243,255,0.9) 70%, rgba(252,244,255,0.85) 100%)',
    pageBackground:
      'radial-gradient(ellipse 80% 70% at 20% 50%, rgba(219,234,254,0.8) 0%, transparent 60%), radial-gradient(ellipse 60% 80% at 80% 40%, rgba(221,214,254,0.6) 0%, transparent 55%), radial-gradient(ellipse 50% 60% at 50% 80%, rgba(252,231,243,0.5) 0%, transparent 50%)',
    logoBorder: 'border-slate-100',
    logoBadge: 'bg-blue-600 text-white',
    logoText: 'text-slate-900',
    logoTitle: 'Agentic Governance Platform',
    navActive: 'bg-blue-50 text-blue-700',
    navInactive: 'text-slate-700 hover:text-slate-900 hover:bg-slate-100/60',
    navActiveBar: 'before:bg-blue-600',
    navIconActive: 'text-blue-600',
    navIconInactive: 'text-slate-500',
    groupHeader: 'text-slate-400 group-hover:text-slate-500',
    subActive: 'text-blue-700 font-medium bg-blue-50/60',
    subInactive: 'text-slate-600 hover:text-slate-800 hover:bg-slate-100/40',
    divider: 'border-slate-200/50',
    collapseBg: 'linear-gradient(90deg, rgba(229,239,255,0.5) 0%, rgba(239,246,255,0.95) 100%)',
    collapseBgCollapsed: 'linear-gradient(90deg, rgba(239,246,255,0.95) 0%, rgba(239,246,255,1) 100%)',
    collapseBorder: 'border-slate-200/40',
    collapseChevron: 'text-slate-400 group-hover:text-blue-500',
    switchTo: '/ops',
    switchLabel: 'Operations Platform',
    switchClassName: 'bg-blue-600 text-white hover:bg-blue-700 shadow-sm',
    userBorder: 'border-slate-100',
    userButtonHover: 'hover:bg-slate-100/60',
    userAvatar: 'bg-blue-50 text-blue-700',
    userName: 'text-slate-900',
    userSub: 'text-slate-500',
  },
  operations: {
    asideClassName: 'border-r border-slate-500/30',
    asideBackground:
      'linear-gradient(180deg, rgba(51,65,85,0.96) 0%, rgba(45,61,78,0.95) 45%, rgba(28,66,58,0.95) 100%)',
    pageBackground:
      'radial-gradient(ellipse 80% 70% at 20% 50%, rgba(209,250,229,0.75) 0%, transparent 60%), radial-gradient(ellipse 60% 80% at 80% 40%, rgba(204,251,241,0.6) 0%, transparent 55%), radial-gradient(ellipse 50% 60% at 50% 80%, rgba(236,253,245,0.55) 0%, transparent 50%)',
    logoBorder: 'border-slate-500/30',
    logoBadge: 'bg-emerald-500 text-white',
    logoText: 'text-white',
    logoTitle: 'Agentic Ops Platform',
    navActive: 'bg-emerald-400/20 text-emerald-200',
    navInactive: 'text-slate-200 hover:text-white hover:bg-white/10',
    navActiveBar: 'before:bg-emerald-300',
    navIconActive: 'text-emerald-300',
    navIconInactive: 'text-slate-300',
    groupHeader: 'text-slate-400 group-hover:text-slate-200',
    subActive: 'text-emerald-200 font-medium bg-emerald-400/15',
    subInactive: 'text-slate-300 hover:text-white hover:bg-white/10',
    divider: 'border-slate-500/30',
    collapseBg: 'linear-gradient(90deg, rgba(71,85,105,0.5) 0%, rgba(51,65,85,0.95) 100%)',
    collapseBgCollapsed: 'linear-gradient(90deg, rgba(51,65,85,0.95) 0%, rgba(51,65,85,1) 100%)',
    collapseBorder: 'border-slate-500/30',
    collapseChevron: 'text-slate-300 group-hover:text-emerald-300',
    switchTo: '/',
    switchLabel: '← Governance Platform',
    switchClassName: 'bg-emerald-400/20 text-emerald-100 hover:bg-emerald-400/30 border border-emerald-300/30',
    userBorder: 'border-slate-500/30',
    userButtonHover: 'hover:bg-white/10',
    userAvatar: 'bg-emerald-400/20 text-emerald-200',
    userName: 'text-white',
    userSub: 'text-slate-300',
  },
};
