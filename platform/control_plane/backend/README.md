# Control-plane backend

The FastAPI service behind the Agentic Governance Platform. It is the API the SPA calls and the only
component that talks to Microsoft Graph, the AWS Agent Registry, and DynamoDB — the agent and MCP
registries, access grants, Cedar policy text, the marketplace, the governance graph, and the
observability read paths all live here.

It holds no identity of its own — identity is delegated to your identity provider, and Microsoft
Entra ID is the provider supported today. Every inbound request carries a Microsoft Entra ID access
token that this service validates against Entra's JWKS; every "may this caller reach this target"
decision is an Entra app-role assignment read and written live through Microsoft Graph. See the root
[README](../../../README.md) for what the platform does and
[`docs/project-history.md`](../../../docs/project-history.md) for why it is shaped this way.

---

## Layout

```
backend/
├── src/
│   ├── main.py          # app factory, router registration, startup hooks
│   ├── api/routes/      # 18 modules exporting 21 routers — the HTTP surface
│   ├── services/        # 40 modules — Entra/Graph, registry, marketplace, builds, observability
│   ├── models/          # Pydantic domain models (Agent, McpServer, Tenant, Guardrail, …)
│   └── core/            # config, auth dispatch, RBAC, tenant resolution, secrets
├── tests/               # the pytest suite — mirrors src/, one or more files per unit
├── scripts/             # operational one-shots (seeding, demo gateways) — not imported by src/
├── run_dev.sh           # local dev server
├── Dockerfile           # the deployed artifact (python:3.11-slim, non-root, PYTHONPATH=/app/src)
└── docker-compose.yml   # the same image locally on :8000
```

Every router is registered **twice** in `main.py` — once at `API_PREFIX` (`/api/v1`) and again at
`ROOT_PATH + API_PREFIX` when `ROOT_PATH` is set, because API Gateway serves the app under a stage
prefix (`/dev`). The health router is the single exception: it is registered with no prefix at all and,
when `ROOT_PATH` is set, at `ROOT_PATH` itself. A new router must be added to **both** blocks or it
will 404 in cloud while working locally.

---

## Running it locally

`src/` is not a package. Imports inside the backend are written `from core.config import settings`,
so `src` must be on `PYTHONPATH` — this is the one convention that trips up every newcomer. It is set
for you in `run_dev.sh` and in the `Dockerfile` (`ENV PYTHONPATH=/app/src`).

```bash
cd platform/control_plane/backend
cp .env.example .env          # then fill it in — the server refuses to start without it
./run_dev.sh                  # creates the venv, installs deps, serves on :8000
```

`run_dev.sh` installs `requirements-dev.txt` (which pulls in `requirements.txt`), exports
`PYTHONPATH=src`, and runs uvicorn with hot reload. API docs are at `http://localhost:8000/docs`.

For local API work without an Entra token, set `USE_DEV_AUTH=true` in `.env` — a header-driven
bypass (`x-user-email` / `x-user-role`) that exists only for development. Do **not** set
`DEBUG=true`: it also enables the bypass, but it additionally makes the global exception handler echo
exception text to the caller.

`docker compose up --build` runs the real image instead, from the repo root as build context. Read the
header of [`docker-compose.yml`](docker-compose.yml) first — it explains why `.env` is not optional
there either.

With no `DATABASE_URL` set, SQLAlchemy falls back to a local SQLite file (`control_plane.db`, created
on startup and gitignored). It is a developer convenience; the governance domains it matters for live
in DynamoDB.

---

## Tests

```bash
cd platform/control_plane/backend
python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt
venv/bin/python -m pytest -q
```

Current: **3275 passed, 6 skipped** out of 3281 collected, about 63 seconds. No AWS credentials, no
deployed stack, and no network are required — and that last one is enforced: `tests/conftest.py`
installs an autouse guard that **fails any test which opens a TCP connection to a non-loopback
address**. If a new test starts reaching the internet, that guard is what tells you, not a slow
suite.

`pyproject.toml` sets `pythonpath = ["src"]` for pytest, so a bare `python -m pytest` works from this
directory. `PYTHONPATH=src venv/bin/python -m pytest -q` remains correct and collects identically —
prefixing it is now optional for tests, and still required for anything under `scripts/`:

```bash
PYTHONPATH=src venv/bin/python scripts/seed_agents.py --dry-run
```

---

## Invariants a change must not break

These are load-bearing. Each one was a defect first and a convention second.

**Authorization is per-endpoint, never router-level.** Routes import
`from fastapi import Depends as RBACDepends` and gate with a **trailing parameter**:

```python
from fastapi import Depends as RBACDepends
from core.rbac import Principal, Role, current_principal, require_role

async def delete_agent(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
```

There is no `APIRouter(dependencies=[...])` anywhere in this codebase and none should be introduced:
a router-level dependency silently gates routes a reviewer reads as ungated, and the levels are not
uniform per router. The mapping is `GET` = `VIEWER`, mutations = `OPERATOR`, and lifecycle, user, and
connection administration = `ADMIN`. Roles come from the validated token's `roles` claim, mapped in
[`src/core/rbac.py`](src/core/rbac.py).

**A foreign tenant's resource must look absent, not forbidden.** Reads filter by tenant, writes
verify it, and a detail read of a resource belonging to another tenant returns a **404 that is
byte-identical to the missing-resource 404** — same status, same `detail` string. A 403 there would
confirm the resource exists. Creating *into* a foreign tenant returns **403**, because no resource
exists yet to hide. Every detail, mutation, and lifecycle route funnels through one visibility helper
per domain (for agents, `_load_visible_agent`) precisely so a new endpoint cannot forget.

**An empty table name means in-memory.** `GUARDRAILS_TABLE_NAME`, `MARKETPLACE_TABLE_NAME`,
`CONNECTIONS_TABLE_NAME`, `PROJECTS_TABLE_NAME`, and `TENANTS_TABLE_NAME` all default to `""`, and a
service handed an empty name builds **no boto3 client at all** and keeps its records in a
process-local store that round-trips through the same serialization helpers as the DynamoDB path.
That is what lets the suite exercise these services without credentials. A service that constructs a
boto3 resource in `__init__` cannot be tested and will not be accepted.

**Secrets are never in code, a record, a response, a log, or a doc.** The Entra backend client secret
reaches the running task through the ECS task definition's `secrets` block, resolved from AWS Secrets
Manager at task start, so the application reads it as an ordinary environment variable and never
holds a credential in the repository. Locally it comes from `.env`, which is gitignored. Per-tenant
and per-connection credentials follow the same rule: the platform stores a Secrets Manager *name or
ARN* on a record, never the material.

---

## Configuration

Every setting is a field on `Settings` in `src/core/config.py`, read from the environment (and from
`.env` locally — Pydantic Settings is configured with `env_file = ".env"` and `extra = "ignore"`, so
one `.env` can carry variables belonging to sibling tools without breaking startup).

[`.env.example`](.env.example) is the authoritative local template and lists every variable you must
supply, with the Entra values annotated. It is deliberately short: the rest have working defaults, and
in cloud they are rendered into the ECS task definition by Terraform rather than being written down.

The Entra identifiers themselves — tenant, audience, scope, the three app roles, and the Graph
application permissions the platform needs — are created once in your directory. That process is
documented step by step in [`docs/entra-setup.md`](../../../docs/entra-setup.md), which is
where `ENTRA_AUDIENCE`, `ENTRA_SPA_CLIENT_ID`, and their Terraform counterparts get their values. The
audience must be byte-identical in three places (here, the Terraform tfvars, and the SPA's
`VITE_ENTRA_SPA_SCOPE`); when it is not, every API call returns 401 and nothing in the response says
which side is wrong.

Two conventions worth knowing before you add a setting:

- **The registry name is the operational value; the id is an optional override.** AWS mints registry
  ids, so `AGENT_REGISTRY_NAME` / `MCP_REGISTRY_NAME` are what Terraform passes, and the backend
  resolves a name to an id on first use and memoises it. `AGENT_REGISTRY_ID` / `MCP_REGISTRY_ID`
  stay honoured as explicit overrides for the operational scripts; empty means "resolve by name" and
  is the normal state.
- **No account ids, regions, or ARNs are hardcoded.** Account and region come from ambient
  credentials via STS. A placeholder credential in a checked-in file outranks the real ambient one,
  which is why `.env.example` has no access-key entry.

---

## Contributing

Read [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) for the gates a change must pass and the commit
convention. The frontend that consumes this API is documented in
[`../frontend/README.md`](../frontend/README.md); the Terraform that deploys it is in
[`../infrastructure/`](../infrastructure/).
