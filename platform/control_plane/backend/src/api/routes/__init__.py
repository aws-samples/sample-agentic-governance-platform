"""
API routes
"""

from api.routes.health import router as health_router
from api.routes.users import router as users_router
from api.routes.guardrails import router as guardrails_router
from api.routes.agents import router as agents_router
from api.routes.mcp_servers import router as mcp_servers_router
from api.routes.grants import router as grants_router
from api.routes.grants import entra_router
from api.routes.mcp_server_grants import router as mcp_grants_router
from api.routes.mcp_server_grants import agent_mcp_router
from api.routes.mcp_cedar import router as mcp_cedar_router
from api.routes.marketplace import router as marketplace_router
from api.routes.governance_graph import router as governance_graph_router
from api.routes.users_admin import router as users_admin_router
from api.routes.connections import router as connections_router
from api.routes.projects import router as projects_router
from api.routes.projects import repositories_router
from api.routes.tenants import router as tenants_router
from api.routes.github_templates import router as github_templates_router
from api.routes.builds import router as builds_router
from api.routes.github_link import router as github_link_router
from api.routes.observability import router as observability_router

__all__ = [
    "health_router",
    "users_router",
    "guardrails_router",
    "agents_router",
    "mcp_servers_router",
    "grants_router",
    "entra_router",
    "mcp_grants_router",
    "agent_mcp_router",
    "mcp_cedar_router",
    "marketplace_router",
    "governance_graph_router",
    "users_admin_router",
    "connections_router",
    "projects_router",
    "repositories_router",
    "tenants_router",
    "github_templates_router",
    "builds_router",
    "github_link_router",
    "observability_router",
]
