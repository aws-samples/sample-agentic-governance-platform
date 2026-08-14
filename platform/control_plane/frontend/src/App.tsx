import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { sectionFromPath, SECTION_THEME } from './components/operations/operationsTheme';
import { UserProvider } from './contexts/UserContext';
import { DemoStoreProvider } from './components/operations/demoStore';
import SignIn from './components/SignIn';
import Sidebar from './components/Sidebar';
import Home from './components/Home';
import Documentation from './components/Documentation';
import Observability from './components/Observability';
import Guardrails from './components/Guardrails';
import ModelManagement from './components/govern/ModelRegistry';
import FinOps from './components/govern/FinOps';
import AuditIncidents from './components/govern/AuditIncidents';
import AgentsOverview from './components/governance/AgentsOverview';
import AgentsList from './components/governance/AgentsList';
import AgentRegistrationWizard from './components/governance/AgentRegistrationWizard';
import AgentDetail from './components/governance/AgentDetail';
import ToolsAndMcp from './components/governance/ToolsAndMcp';
import McpServerDetail from './components/governance/McpServerDetail';
import McpServerRegistrationWizard from './components/governance/McpServerRegistrationWizard';
import GovernancePrompts from './components/governance/Prompts';
import GovernanceGraph from './components/governance/GovernanceGraph';
import Marketplace from './components/governance/marketplace/Marketplace';
import AdminConsole from './components/governance/admin/AdminConsole';
import OperationsOverview from './components/operations/OperationsOverview';
import Repositories from './components/operations/Repositories';
// Explicit `.tsx` for the same case-insensitive-filesystem reason documented below: the
// neighbouring pure module is `repositoryDetailTabs.ts`, and an extensionless import can
// resolve to a sibling that differs only in casing.
import RepositoryDetail from './components/operations/RepositoryDetail.tsx';
import Deployments from './components/operations/Deployments';
import Projects from './components/operations/Projects';
import ProjectDetail from './components/operations/ProjectDetail';
// Explicit `.tsx` for the same case-insensitive-filesystem reason documented below:
// `Settings` differs from a would-be `settings.ts` only in casing, and the neighbouring
// pure module is `settingsSections.ts`.
import Settings from './components/operations/Settings.tsx';
// Explicit `.tsx` for the same case-insensitive-filesystem reason documented below: the
// neighbouring pure module is `templatesView.ts`, and `Templates` differs from a would-be
// `templates.ts` only in casing.
import Templates from './components/operations/Templates.tsx';
import ConnectionCallback from './components/operations/ConnectionCallback';
// Explicit `.tsx` extensions, deliberately: extensionless `./operations/GitHubLink`
// resolves to `GitHubLink.ts` FIRST, which on a case-insensitive filesystem (macOS,
// Windows) is the pure-logic module `githubLink.ts` — so the import silently lands on the
// wrong file and fails with "has no default export". The two names differ only in casing,
// which is a constraint of the pinned filenames; the extension is what disambiguates them.
import GitHubLink from './components/operations/GitHubLink.tsx';
import GitHubLinkCallback from './components/operations/GitHubLinkCallback.tsx';
import Experiments from './components/operations/Experiments';
import Playground from './components/operations/Playground';
import Studio from './components/operations/Studio';
import AccessKeys from './components/operations/AccessKeys';
import ModelCatalog from './components/operations/ModelCatalog';

function AuthGate() {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();
  const pageBackground = SECTION_THEME[sectionFromPath(location.pathname)].pageBackground;

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50">
        <div className="text-slate-400 text-sm">Loading...</div>
      </div>
    );
  }

  if (!isAuthenticated) return <SignIn />;

  return (
    <UserProvider>
    <DemoStoreProvider>
    <div className="h-screen flex overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto relative">
        {/* Ombre gradient — per-section (governance blue/purple, operations emerald/teal) */}
        <div className="fixed inset-0 pointer-events-none z-0" style={{
          background: pageBackground,
          animation: 'gradientDrift 20s ease-in-out infinite',
        }} />
        <div className="relative h-full">
          <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/agents" element={<AgentsOverview />} />
        <Route path="/agents/all" element={<AgentsList />} />
        <Route path="/agents/new" element={<AgentRegistrationWizard />} />
        <Route path="/agents/:id" element={<AgentDetail />} />
        <Route path="/tools-mcp" element={<ToolsAndMcp />} />
        <Route path="/mcp-servers/new" element={<McpServerRegistrationWizard />} />
        <Route path="/mcp-servers/:id" element={<McpServerDetail />} />
        <Route path="/prompts" element={<GovernancePrompts />} />
        <Route path="/governance-graph" element={<GovernanceGraph />} />
        {/* Marketplace (Epic 9 → Epic 15 tabbed shell). */}
        <Route path="/marketplace" element={<Navigate to="/marketplace/agents" replace />} />
        <Route path="/marketplace/agents" element={<Marketplace />} />
        <Route path="/marketplace/mcps" element={<Marketplace />} />
        <Route path="/marketplace/admin" element={<Navigate to="/admin" replace />} />
        <Route path="/admin" element={<AdminConsole />} />
        {/* Govern: dedicated pages kept in the governance product */}
        <Route path="/govern/models" element={<ModelManagement />} />
        <Route path="/govern/finops" element={<FinOps />} />
        <Route path="/govern/audit" element={<AuditIncidents />} />
        {/* Legacy redirect */}
        <Route path="/govern/cost-tracking" element={<Navigate to="/govern/finops" replace />} />
        <Route path="/observability" element={<Observability />} />
        <Route path="/secure/guardrails" element={<Guardrails initialTab="templates" />} />
        <Route path="/secure/guardrails/create" element={<Guardrails initialTab="builder" />} />
        <Route path="/secure/guardrails/observability" element={<Guardrails initialTab="observability" />} />
        <Route path="/docs" element={<Documentation />} />
        <Route path="/docs/:section" element={<Documentation />} />
        {/* === Operations platform (Epic 17) — URL-prefixed section === */}
        <Route path="/ops" element={<OperationsOverview />} />
        <Route path="/ops/experiments" element={<Experiments />} />
        <Route path="/ops/playground" element={<Playground />} />
        <Route path="/ops/studio" element={<Studio />} />
        <Route path="/ops/repositories" element={<Repositories />} />
        {/* E28/T11 — the repository detail page. The destination of the shared repo row's
            `onNavigate` (T13 wires both lists to it), and the host of the tab bodies T12
            and T14 add. */}
        <Route path="/ops/repositories/:id" element={<RepositoryDetail />} />
        <Route path="/ops/deployments" element={<Deployments />} />
        <Route path="/ops/access" element={<AccessKeys />} />
        <Route path="/ops/models" element={<ModelCatalog />} />
        <Route path="/ops/projects" element={<Projects />} />
        <Route path="/ops/projects/:id" element={<ProjectDetail />} />
        {/* E28/T7 — Templates + Settings are now top-level nav destinations, and both
            now have their dedicated page. Templates is org-scoped and owns its OpsPage
            frame (E28/T9). Settings (E28/T8) replaces the Operations Admin console: a
            General tab any caller sees and an ADMIN-gated Admin tab, both rendered from
            the `settingsSections.ts` registry. */}
        <Route path="/ops/templates" element={<Templates />} />
        <Route path="/ops/settings" element={<Settings />} />
        {/* /ops/admin was the Ops settings console for E18–E27; keep it as a
            redirect so old links (ConnectionCallback, GitHubLink) and bookmarks
            survive the rename. */}
        <Route path="/ops/admin" element={<Navigate to="/ops/settings" replace />} />
        <Route path="/ops/connections/callback" element={<ConnectionCallback />} />
        {/* Per-user GitHub account link (E27B). The callback path must match the
            backend's LINK_CALLBACK_PATH and the App manifest's callback_urls entry
            byte-for-byte — `buildLinkRedirectUrl` builds the same string everywhere else. */}
        <Route path="/ops/github-link" element={<GitHubLink />} />
        <Route path="/ops/github-link/callback" element={<GitHubLinkCallback />} />
      </Routes>
        </div>
      </main>
    </div>
    </DemoStoreProvider>
    </UserProvider>
  );
}

function App() {
  return (
    <AuthProvider>
      <Router>
        <AuthGate />
      </Router>
    </AuthProvider>
  );
}

export default App;
