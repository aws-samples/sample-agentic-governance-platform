/**
 * Declarative nav config for the governance IA (Epic 15 mental-model refinement).
 *
 * Sections render as group headers with leaf links beneath. The labelled groups
 * (Inventory · Security & Policy · Operations · Resources) are COLLAPSIBLE (state
 * keyed by `id`, persisted by the Sidebar under `sidebar.governance.expanded`).
 * Standalone items (Home, Marketplace, Admin) have an empty label and
 * `collapsible: false`. Marketplace collapses the old 3-item group to one tabbed
 * destination at /marketplace; Admin is its own console at /admin (adminOnly).
 * Prompts is hidden from the nav this epic (the /prompts route + page remain).
 *
 * Icon strings are inline SVG <path d="..."> values (Heroicons style); the
 * repo uses no icon library.
 */
export interface NavItem {
  to: string;
  label: string;
  icon: string;
  /** Optional child links shown indented beneath the item. */
  children?: { to: string; label: string }[];
  /** When true, the Sidebar shows this item only to admins (role_level >= 2). */
  adminOnly?: boolean;
  /**
   * When true, the destination is a mock-up, not a live surface (Epic 31F). The
   * expanded Sidebar renders a `SoonTag` after the label; the route still exists
   * and still navigates. Carried on the item — not inferred from a group header —
   * so the signal survives wherever the item is rendered.
   */
  comingSoon?: boolean;
}

export interface NavSection {
  /** Stable identifier used to key collapse state (independent of array order). */
  id: string;
  /** Group header text; empty string means "standalone, render no header". */
  label: string;
  /** Whether this group shows a collapse chevron. Standalone Home is false. */
  collapsible: boolean;
  items: NavItem[];
}

const ICON = {
  home: 'M2.25 12l8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75M8.25 21h8.25',
  agents: 'M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z',
  tools: 'M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085',
  shield: 'M12 9v3.75m0-10.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.75c0 5.592 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.75h-.152c-3.196 0-6.1-1.248-8.25-3.285z',
  audit: 'M9 12.75L11.25 15 15 9.75M21 12c0 1.268-.63 2.39-1.593 3.068a3.745 3.745 0 01-1.043 3.296 3.745 3.745 0 01-3.296 1.043A3.745 3.745 0 0112 21c-1.268 0-2.39-.63-3.068-1.593a3.746 3.746 0 01-3.296-1.043 3.745 3.745 0 01-1.043-3.296A3.745 3.745 0 013 12c0-1.268.63-2.39 1.593-3.068a3.745 3.745 0 011.043-3.296 3.746 3.746 0 013.296-1.043A3.746 3.746 0 0112 3c1.268 0 2.39.63 3.068 1.593a3.746 3.746 0 013.296 1.043 3.746 3.746 0 011.043 3.296A3.745 3.745 0 0121 12z',
  cost: 'M2.25 18.75a60.07 60.07 0 0115.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 013 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 00-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 01-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 003 15h-.75M15 10.5a3 3 0 11-6 0 3 3 0 016 0zm3 0h.008v.008H18V10.5zm-12 0h.008v.008H6V10.5z',
  models: 'M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23-.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5',
  eye: 'M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z M15 12a3 3 0 11-6 0 3 3 0 016 0z',
  plan: 'M9 6.75V15m6-6v8.25m.503 3.498l4.875-2.437c.381-.19.622-.58.622-1.006V4.82c0-.836-.88-1.38-1.628-1.006l-3.869 1.934c-.317.159-.69.159-1.006 0L9.503 3.252a1.125 1.125 0 00-1.006 0L3.622 5.689C3.24 5.88 3 6.27 3 6.695V19.18c0 .836.88 1.38 1.628 1.006l3.869-1.934c.317-.159.69-.159 1.006 0l4.994 2.497c.317.158.69.158 1.006 0z',
  docs: 'M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25',
  prompts: 'M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z',
  marketplace: 'M13.5 21v-7.5a.75.75 0 01.75-.75h3a.75.75 0 01.75.75V21m-4.5 0H2.36m11.14 0H18m0 0h3.64m-1.39 0V9.349M3.75 21V9.349m0 0a3.001 3.001 0 003.75-.615A2.993 2.993 0 009.75 9.75c.896 0 1.7-.393 2.25-1.016a2.993 2.993 0 002.25 1.016c.896 0 1.7-.393 2.25-1.016a3.001 3.001 0 003.75.614m-16.5 0a3.004 3.004 0 01-.621-4.72L4.318 3.44A1.5 1.5 0 015.378 3h13.243a1.5 1.5 0 011.06.44l1.19 1.189a3 3 0 01-.621 4.720M6.75 18h3.75a.75.75 0 00.75-.75V13.5a.75.75 0 00-.75-.75H6.75a.75.75 0 00-.75.75v3.75c0 .415.336.75.75.75z',
  // Heroicons "share" — three connected nodes; reads as a node-link/graph glyph.
  graph: 'M7.217 10.907a2.25 2.25 0 100 2.186m0-2.186c.18.324.283.696.283 1.093s-.103.77-.283 1.093m0-2.186l9.566-5.314m-9.566 7.5l9.566 5.314m0 0a2.25 2.25 0 103.935 2.186 2.25 2.25 0 00-3.935-2.186zm0-12.814a2.25 2.25 0 103.933-2.185 2.25 2.25 0 00-3.933 2.185z',
  admin: 'M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75',
} as const;

export const GOVERNANCE_NAV: NavSection[] = [
  {
    id: 'home',
    label: '', // standalone Home — not collapsible
    collapsible: false,
    items: [{ to: '/', label: 'Home', icon: ICON.home }],
  },
  {
    id: 'inventory',
    label: 'Inventory',
    collapsible: true,
    items: [
      { to: '/agents', label: 'Agents', icon: ICON.agents },
      { to: '/tools-mcp', label: 'MCPs', icon: ICON.tools },
      { to: '/govern/models', label: 'LLM Gateway', icon: ICON.models, comingSoon: true },
      { to: '/governance-graph', label: 'Governance Graph', icon: ICON.graph },
    ],
  },
  {
    id: 'security-policy',
    label: 'Security & Policy',
    collapsible: true,
    items: [
      { to: '/secure/guardrails', label: 'Guardrails & Policy', icon: ICON.shield },
      { to: '/govern/audit', label: 'Audit & Incidents', icon: ICON.audit, comingSoon: true },
    ],
  },
  {
    id: 'operations',
    label: 'Operations',
    collapsible: true,
    items: [
      { to: '/observability', label: 'Observability', icon: ICON.eye },
      { to: '/govern/finops', label: 'Cost & FinOps', icon: ICON.cost, comingSoon: true },
    ],
  },
  // Standalone Marketplace (Epic 15 — collapsed from the old 3-item group to one
  // tabbed destination at /marketplace).
  {
    id: 'marketplace',
    label: '',
    collapsible: false,
    items: [{ to: '/marketplace', label: 'Marketplace', icon: ICON.marketplace }],
  },
  // Standalone Admin console (Epic 15). adminOnly → visibleNavItems() drops it for
  // non-admins; the /admin page self-guards and the backend is the real gate.
  {
    id: 'admin',
    label: '',
    collapsible: false,
    items: [{ to: '/admin', label: 'Admin', icon: ICON.admin, adminOnly: true }],
  },
  {
    id: 'resources',
    label: 'Resources',
    collapsible: true,
    items: [
      { to: '/docs', label: 'Documentation', icon: ICON.docs },
    ],
  },
];

/** Filter a section's items by the viewer's admin status (drops adminOnly items for non-admins). */
export function visibleNavItems(items: NavItem[], isAdmin: boolean): NavItem[] {
  return items.filter((i) => !i.adminOnly || isAdmin);
}

/** Flat, deduped list of every leaf route in the governance menu (incl. children). */
export function allRoutes(): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const sec of GOVERNANCE_NAV) {
    for (const item of sec.items) {
      if (!seen.has(item.to)) { seen.add(item.to); out.push(item.to); }
      for (const child of item.children ?? []) {
        if (!seen.has(child.to)) { seen.add(child.to); out.push(child.to); }
      }
    }
  }
  return out;
}
