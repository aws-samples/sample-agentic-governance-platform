# Entra ID setup script

`setup_entra.py` automates steps 2–10 of [`docs/entra-setup.md`](../../docs/entra-setup.md): it creates
the two app registrations with everything they carry, grants the admin consents, assigns you to
`Platform.Admin`, and **prints** the values step 1 of [Bootstrapping](../../README.md#bootstrapping) asks
you to come back with.

It is optional, and it is **print-only** — the script writes nothing to disk. You paste its output into
`secrets.auto.tfvars` and the frontend `.env` yourself. The manual guide stays the authoritative
reference and the place to understand what the script created.

## What it does

1. **Preflight and reads.** Checks the Azure CLI is on `PATH`, takes a Microsoft Graph token from it, then
   reads your own object id, the tenant's `*.onmicrosoft.com` domain, and the ids of the six Microsoft
   Graph application permissions it needs.
2. **The backend app registration.** One create call carrying the Application ID URI, the `Access.Default`
   delegated scope, the three `Platform.*` app roles, the six Graph application permissions, and a client
   secret — then its service principal, with *Assignment required* set.
3. **Admin consent** for those six application permissions.
4. **The SPA app registration.** Single-tenant, with an empty single-page-application platform ready for
   the redirect URI you add after your first deploy, and delegated admin consent for the backend's
   `Access.Default` scope.
5. **Your own role.** Assigns the signed-in account to `Platform.Admin` on the backend app.
6. **Prints the output blocks** (below).

Re-runs reuse what exists — the backend app is found by its Application ID URI, the SPA by display name.
Neither one's roles, scope or permissions are ever edited: the script compares both against what it
expects and prints a **drift report** for you to act on, because rewriting an app's roles or scope
re-mints their ids and silently orphans every assignment made against them. (`--rotate-secret` is the
only write it makes to a registration that already exists, and it only adds a secret.)

## Prerequisites

- **Azure CLI installed, and `az login` completed** as an administrator of the tenant. The CLI is used for
  the token and nothing else; every write goes straight to the Microsoft Graph REST API. Because the token
  is the signed-in user's, a Graph `403` always means your account lacks the directory role.
- **A directory role that can do the writes.** *Application Administrator* together with *Privileged Role
  Administrator*, or *Global Administrator* on its own — the same pair the script's `--help` names. The
  Privileged Role Administrator half is Microsoft's minimum for consenting to Graph application permissions.
- **`requests`** — the script's only third-party dependency. A virtualenv beside it keeps your system
  Python clean:

  ```bash
  cd scripts/entra-setup
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  ```

## Usage

```bash
cd scripts/entra-setup
.venv/bin/python setup_entra.py --dry-run    # always start here
.venv/bin/python setup_entra.py
```

| Flag | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Performs only the reads in step 1 plus existence checks, then prints every mutation it *would* send, with payloads. Zero writes — the safe first run against a configured tenant. |
| `--audience` | `api://agp` | backend Application ID URI |
| `--backend-name` | `AGP - Backend Graph Client` | backend app display name |
| `--spa-name` | `AGP - Frontend` | SPA app display name |
| `--rotate-secret` | off | mint an additional client secret on an existing backend app |

The scope name is fixed at `Access.Default` and has no flag; to expose a different one, set
`VITE_ENTRA_SPA_SCOPE` by hand (guide step 6).

## What it prints

Everything goes to stdout, in this order:

1. A **`secrets.auto.tfvars` block** — `entra_tenant_id`, `entra_audience`, `entra_spa_client_id`,
   `entra_backend_client_id`, `entra_backend_client_secret` — keyed and quoted exactly as that file
   expects, so it pastes in as-is.
2. A **frontend `.env` block** — `VITE_AUTH_PROVIDER`, `VITE_ENTRA_TENANT_ID`,
   `VITE_ENTRA_TENANT_DOMAIN`, `VITE_ENTRA_SPA_CLIENT_ID`, `VITE_ENTRA_SPA_SCOPE`.
3. An **"after you deploy" note** — `VITE_API_URL` and `VITE_ENTRA_SPA_REDIRECT_URI` come from
   `terraform output`, and that redirect URI must **also** be registered on the SPA app in Entra
   (the guide's *After the first apply* section). It is the step that silently breaks sign-in when skipped.
4. **The client secret, once**, flagged as shown-only-now. Entra never returns it again and neither can a
   re-run, which prints a placeholder in its place; `--rotate-secret` appends a new one.
5. **What it did for you** — the account it assigned to `Platform.Admin`.

The printed audience is read back from the app the script created or found, so it reflects what Entra
actually accepted rather than what you passed to `--audience`.

## Test it safely first

Against a tenant you cannot afford to disturb, work up in four steps:

1. **`--dry-run`.** Reads and existence checks only, then a printed plan of every mutation and its
   payload. Zero writes.
2. **A throwaway pair.** A test audience and test display names create a complete, parallel setup that
   touches nothing real:

   ```bash
   .venv/bin/python setup_entra.py --audience api://agp-test \
     --backend-name 'TEST - Backend Graph Client' --spa-name 'TEST - Frontend'
   ```

3. **Inspect the two apps in the portal** against sections 2–4 of the guide: the roles on the backend app,
   the exposed scope, both consents, and your own assignment.
4. **Delete the two TEST app registrations.** Deleting an app cascades — its service principal, the
   consents, and the app-role assignments go with it, so there is nothing left to clean up by hand.

Then, optionally, re-run it with your real names as a **drift audit** of a setup you built by hand: it
reuses both registrations and prints whatever does not match what it expects. It creates no registrations,
but it is not read-only — the consents and your role assignment are re-sent, so a gap in a hand-built
setup gets filled rather than reported. For an audit that writes nothing at all, use `--dry-run`. Left to
its default the frontend lookup also tries the guide's em-dash spelling (`AGP — Frontend`); any other
name you have to pass with `--spa-name`, exactly as the portal shows it, or the run creates a duplicate.

## What it never does

- **Write or edit a file.** The output is yours to paste.
- **Register a redirect URI.** That value does not exist until Terraform has created the CloudFront domain;
  section 6 of the guide closes that loop by hand.
- **Delete anything.** There is no cleanup mode — deleting the TEST apps above is a portal action.
- **Edit an existing registration's roles, scope or permissions.** Drift is reported, never fixed. The one
  write it will make to an existing registration is `--rotate-secret`, and that only adds a secret.
