import { type JSX } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext';
import { useAuth } from './auth/useAuth';
import { sectionFromPath, SECTION_THEME } from './components/operations/operationsTheme';
import { UserProvider } from './contexts/UserContext';
import { DemoStoreProvider } from './components/operations/DemoStoreProvider';
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
import ObjectList from './components/operations/ObjectList';
import RepositoryDetail from './components/operations/RepositoryDetail';
import Deployments from './components/operations/Deployments';
import Projects from './components/operations/Projects';
import ProjectDetail from './components/operations/ProjectDetail';
import Settings from './components/operations/Settings';
import Templates from './components/operations/Templates';
import ConnectionCallback from './components/operations/ConnectionCallback';
import GitHubLink from './components/operations/GitHubLink';
import GitHubLinkCallback from './components/operations/GitHubLinkCallback';
import Experiments from './components/operations/Experiments';
import Playground from './components/operations/Playground';
import Studio from './components/operations/Studio';
import AccessKeys from './components/operations/AccessKeys';
import ModelCatalog from './components/operations/ModelCatalog';
import { PAGE_PATHS, REDIRECTS, type PagePath } from './routes';

const PAGES = {
  '/': <Home />,
  '/agents': <AgentsOverview />,
  '/agents/all': <AgentsList />,
  '/agents/new': <AgentRegistrationWizard />,
  '/agents/:id': <AgentDetail />,
  '/tools-mcp': <ToolsAndMcp />,
  '/mcp-servers/new': <McpServerRegistrationWizard />,
  '/mcp-servers/:id': <McpServerDetail />,
  '/prompts': <GovernancePrompts />,
  '/governance-graph': <GovernanceGraph />,
  '/marketplace/agents': <Marketplace />,
  '/marketplace/mcps': <Marketplace />,
  '/admin': <AdminConsole />,
  '/govern/models': <ModelManagement />,
  '/govern/finops': <FinOps />,
  '/govern/audit': <AuditIncidents />,
  '/observability': <Observability />,
  '/secure/guardrails': <Guardrails initialTab="templates" />,
  '/secure/guardrails/create': <Guardrails initialTab="builder" />,
  '/secure/guardrails/observability': <Guardrails initialTab="observability" />,
  '/docs': <Documentation />,
  '/docs/:section': <Documentation />,
  '/ops': <OperationsOverview />,
  '/ops/experiments': <Experiments />,
  '/ops/playground': <Playground />,
  '/ops/studio': <Studio />,
  '/ops/agents': <ObjectList key="agent" kind="agent" />,
  '/ops/agents/:id': <RepositoryDetail />,
  '/ops/mcp-servers': <ObjectList key="mcp_server" kind="mcp_server" />,
  '/ops/mcp-servers/:id': <RepositoryDetail />,
  '/ops/repositories': <Repositories />,
  '/ops/repositories/:id': <RepositoryDetail />,
  '/ops/deployments': <Deployments />,
  '/ops/access': <AccessKeys />,
  '/ops/models': <ModelCatalog />,
  '/ops/projects': <Projects />,
  '/ops/projects/:id': <ProjectDetail />,
  '/ops/templates': <Templates />,
  '/ops/settings': <Settings />,
  '/ops/connections/callback': <ConnectionCallback />,
  '/ops/github-link': <GitHubLink />,
  '/ops/github-link/callback': <GitHubLinkCallback />,
} satisfies Record<PagePath, JSX.Element>;

function AuthGate() {
  const { isAuthenticated, isLoading, arm } = useAuth();
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
    <div className="h-screen flex overflow-hidden" data-auth-arm={arm}>
      <Sidebar />
      <main className="flex-1 overflow-y-auto relative">
        <div className="fixed inset-0 pointer-events-none z-0" style={{
          background: pageBackground,
          animation: 'gradientDrift 20s ease-in-out infinite',
        }} />
        <div className="relative h-full">
          <Routes>
            {PAGE_PATHS.map((path) => (
              <Route key={path} path={path} element={PAGES[path]} />
            ))}
            {Object.entries(REDIRECTS).map(([path, to]) => (
              <Route key={path} path={path} element={<Navigate to={to} replace />} />
            ))}
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
