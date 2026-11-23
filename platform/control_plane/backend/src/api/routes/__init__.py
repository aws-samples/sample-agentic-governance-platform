"""The routers ``main.py`` mounts."""

from api.routes.agents import router as agents_router
from api.routes.builds import router as builds_router
from api.routes.connections import router as connections_router
from api.routes.containment import router as containment_router
from api.routes.github_link import router as github_link_router
from api.routes.github_templates import infra_templates_router
from api.routes.github_templates import router as github_templates_router
from api.routes.governance_graph import router as governance_graph_router
from api.routes.grants import entra_router
from api.routes.grants import router as grants_router
from api.routes.guardrails import router as guardrails_router
from api.routes.health import router as health_router
from api.routes.marketplace import router as marketplace_router
from api.routes.mcp_cedar import router as mcp_cedar_router
from api.routes.mcp_server_grants import agent_mcp_router
from api.routes.mcp_server_grants import router as mcp_grants_router
from api.routes.mcp_servers import router as mcp_servers_router
from api.routes.observability import router as observability_router
from api.routes.project_repos import router as project_repos_router
from api.routes.projects import router as projects_router
from api.routes.repositories import repositories_router
from api.routes.tenants import router as tenants_router
from api.routes.users import router as users_router
from api.routes.users_admin import router as users_admin_router

__all__ = [
    "agent_mcp_router",
    "agents_router",
    "builds_router",
    "connections_router",
    "containment_router",
    "entra_router",
    "github_link_router",
    "github_templates_router",
    "governance_graph_router",
    "grants_router",
    "guardrails_router",
    "health_router",
    "infra_templates_router",
    "marketplace_router",
    "mcp_cedar_router",
    "mcp_grants_router",
    "mcp_servers_router",
    "observability_router",
    "project_repos_router",
    "projects_router",
    "repositories_router",
    "tenants_router",
    "users_admin_router",
    "users_router",
]
