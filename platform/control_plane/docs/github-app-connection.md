# GitHub App connections — operator setup guide

> **Audience:** platform admins configuring a Git org connection that authenticates as an
> **org-installed GitHub App** instead of a personal access token (PAT).
> **Feature:** E20/T9 — GitHub App auth for org Connections.
> **Companion reading:** the connections data model and its secret seam are defined in
> `backend/src/models/connection.py` and `backend/src/services/connection_service.py`.

## Why a GitHub App over a PAT

A connection can authenticate in two ways (`auth_type`):

| | `pat` | `github_app` |
|---|---|---|
| Identity owner | a **person** (their account) | the **org** (survives staff churn) |
| Permissions | everything the person can do | **scoped** to what the App was granted |
| Credential lifetime | long-lived token AGP stores | AGP stores only the App **private key**; every operation mints a **fresh, short-lived (~1h)** installation token |
| Blast radius if leaked | large, long-lived | small, expires quickly |

AGP mints a new installation access token **per operation** (verify, repo create, set CI
variables) from the stored private key — nothing long-lived is ever handed to the GitHub
API on your behalf. The token is a plain `Authorization: Bearer {token}`, so every
downstream consumer works identically to the PAT path.

## What AGP stores (the secret seam)

- **In DynamoDB (non-secret, returned by the API):** `auth_type=github_app`, `app_id`,
  `installation_id`, plus the usual connection metadata (`org`, `status`, `account_login`,
  `secret_arn`, …).
- **In AWS Secrets Manager only:** the App **private key** (PEM), as
  `{"private_key": "<PEM>"}`. The private key is **never** returned by any API response,
  never logged, and never placed in an error message. The minted installation token is
  transient — it is never stored.

## Two ways to set up the App

The **What AGP stores** seam above applies to both paths — only *how the App gets created and
its credentials reach AGP* differs.

| | **Path A — via the platform (App Manifest)** | **Path B — manual / Enterprise** |
|---|---|---|
| Recommended for | github.com, the common case | GitHub Enterprise, or when the browser redirect handshake isn't viable |
| Who creates the App | the platform builds a **manifest**; GitHub creates the App on one approval | you create the App by hand in GitHub settings |
| Key material | AGP captures **App ID + private key + webhook secret automatically** — you never copy or paste key material | you generate a `.pem` and paste **App ID + Installation ID + private key** |
| Steps | approve → install → auto-finalize | Steps 1–5 below |

Path A is described next; Path B is the numbered runbook after it.

## Path A (recommended) — via the platform (App Manifest)

From the Operations Admin UI, **Org Connections** tab:

1. Click **Add connection**, choose **App via Manifest**, and enter the target **org** login.
2. Click **Create GitHub App**. The platform builds a GitHub App *manifest* and sends your
   browser to GitHub. The manifest's `redirect_url` is the SPA origin — e.g.
   `http://localhost:5173/ops/connections/callback` for local dev, the CloudFront URL in cloud.
3. On GitHub, **approve** the App (one click). GitHub creates the App and redirects your browser
   back to the `redirect_url` with a one-time code. The platform exchanges that code (the
   manifest-conversion API) and **automatically captures the App ID, private key, and webhook
   secret** — you never see or copy key material. The connection is now in a brief
   **pending install** state.
4. **Install the App** on the org (the platform links you to the install page). Choose **All
   repositories** or a selected set — the App can only act on repos it is installed on.
5. **Finalize.** The platform auto-resolves the `installation_id` and verifies; the connection
   flips to **Connected**. If auto-resolve can't determine the installation, paste the
   **Installation ID** as a fallback (see Step 3 of Path B for where GitHub shows it), then verify.

No fields are copied by hand on the happy path — the only manual entry is the org login (and the
Installation ID fallback).

> **One manual permission step (GitHub limitation).** The manifest-created App is granted
> **Administration**, **Contents**, **Workflows**, **Actions** (write) and **Metadata** (read),
> but **not Variables** — GitHub's manifest endpoint rejects a manifest that requests the
> `variables` permission (`"Default permission records resource is not included in the list"`).
> Connection onboarding and template rollout (incl. the scaffold's `.github/workflows/build.yml`,
> which needs Workflows) work without it, but **before creating projects** (which seed build-only
> CI *variables*) add **Variables → Read & write** to the App by hand: App settings →
> **Permissions & events → Repository permissions → Variables**, then accept the update on the
> org's installation. Path B (manual) grants Variables from the start (Step 2 below).

## Path B (manual / Enterprise)

Create the App by hand and paste the three credentials. Use this on GitHub Enterprise or when the
Path A browser redirect isn't viable.

### Step 1 — Create the GitHub App on the org

1. Go to your org: **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. **Name** it (e.g. `agp-<org>`; GitHub caps App names at 34 characters) and set any
   **Homepage URL** (required by the form; not used by AGP).
3. **Webhook:** uncheck **Active** — AGP does not consume App webhooks.

### Step 2 — Set the App's repository permissions

Under **Permissions → Repository permissions**, grant exactly what the platform needs:

| Permission | Access | Why |
|---|---|---|
| **Administration** | Read & write | create the repo in the org |
| **Contents** | Read & write | push the initial commit (scaffold files) |
| **Workflows** | Read & write | push the scaffold's `.github/workflows/build.yml` (workflow files are gated separately from Contents) |
| **Variables** | Read & write | set the build-only GitHub Actions repo variables |
| **Actions** | Read & write | (paired with Variables for CI setup) |
| **Metadata** | Read-only | mandatory baseline; org/repo visibility checks |

Leave organization and account permissions at **No access**. Save changes.

### Step 3 — Install the App on the org

1. In the App's page, open **Install App** and install it on the target **organization**.
2. Choose **All repositories** (or a selected set — the App can only act on repos it is
   installed on).
3. After installing, the browser URL is
   `https://github.com/organizations/<org>/settings/installations/<INSTALLATION_ID>` —
   note the trailing number, that is your **Installation ID**.

### Step 4 — Capture the three credentials

From the App's **General** settings page:

1. **App ID** — shown near the top (a numeric id).
2. **Installation ID** — from the install URL in Step 3.
3. **Private key** — under **Private keys**, click **Generate a private key**. A `.pem`
   file downloads. This is the secret AGP stores; keep it safe and paste its **full
   contents** (including the `-----BEGIN…-----`/`-----END…-----` lines) when creating the
   connection.

### Step 5 — Create the AGP connection

Call the admin API (admin-gated — `POST /api/v1/admin/connections`) with `auth_type` set to
`github_app` and the three captured values:

```json
{
  "provider": "github",
  "org": "<org-login>",
  "auth_type": "github_app",
  "app_id": "123456",
  "installation_id": "78910",
  "private_key": "<PEM private key contents — the .pem you downloaded from the GitHub App>"
}
```

- `base_url` is optional — set it (e.g. `https://ghe.example.com/api/v3`) for GitHub
  Enterprise; omit for github.com.
- On success AGP **mints an installation token, verifies the org is visible to the App**,
  stores the private key in Secrets Manager, and persists the connection with
  `status=connected`. On failure **nothing is stored** and the response carries a safe
  reason (e.g. the App can't see the org, or GitHub declined the token request).

#### Rotating the private key

Generate a new key in the App, then replace it on the connection with
`PUT /api/v1/admin/connections/{id}/key` (admin-gated). AGP verifies the new key (mints an
installation token + runs the org-visibility probe) **before** overwriting the stored
secret; the old key stays in place if verification fails.

```json
{
  "private_key": "<PEM private key contents — the .pem you downloaded from the GitHub App>"
}
```

## Field reference (`ConnectionCreate`)

| `auth_type` | Required fields | Stored secret |
|---|---|---|
| `pat` (default) | `token` | `{"token": <PAT>}` |
| `github_app` | `app_id`, `installation_id`, `private_key` | `{"private_key": <PEM>}` |

Common to both: `provider` (`github`/`gitlab`), `org`, optional `base_url`. Every credential
field is **write-only** — none is ever returned on the `Connection` read model.

## Post-deploy: re-roll out the base template after a scaffold change

When a platform release changes the **base template scaffold** on disk
(`agent-templates/strands-agentcore/`) — say a new `.github/workflows/build.yml` — the change
does **not** reach agent repos automatically. Two things stay stale until acted on:

1. **The org's `strands-agentcore` repo** — a copy of the scaffold pushed at the last rollout.
   New agent repos are **not** generated from it: AGP pushes its own on-disk scaffold bytes
   through the provider seam on every materialize — `create_repo` then `commit_files`
   (`services/project_service.py:1-2`, `:18-21`) — so a new repo always gets the *deployed*
   scaffold, whatever the org copy holds. GitHub's `is_template` flag was abandoned precisely
   because it does not port across providers (`services/template_registry.py:1-12`). What the
   org copy is for is the readable, reviewable reference of what the platform pushes, and it
   is what `reconcile` compares against — so leaving it stale makes the comparison lie.
2. **Existing agent repos** — already-materialized repos carry their own copy of the old
   `build.yml`. They are **intentionally not force-migrated** — smallest blast radius; each
   picks up the new scaffold on its **next recreate** (delete + re-add). This is a
   deliberate design decision, not a gap.

### Start with reconcile, then roll out

`reconcile` is the operator's entry point to this workflow: it compares AGP's template registry
against the org's actual repositories and returns one row per name, each carrying a state and
exactly one sensible action (`api/routes/connections.py:567`,
`services/template_rollout_service.py:10-15`):

| State | Meaning | Your one action |
|---|---|---|
| `registered_present` | in sync | nothing, or re-push the seed |
| `registered_missing` | the repo behind the record is gone | re-create from seed, or deregister |
| `unregistered_present` | a repo in the org that AGP does not know about | **adopt** it |
| `seed_absent` | a seed with nothing in the org | create |

- **API (admin-gated):** `GET /api/v1/admin/connections/{id}/rollout/reconcile`
- **Adopt** an `unregistered_present` repo as-is, without pushing over it:
  `POST /api/v1/admin/connections/{id}/templates/adopt`. Rollout deliberately refuses that
  state — adopting is the explicit verb for it.

Then re-roll out the base template:

- **UI:** Operations Admin → **Org Connections** → the connection's **Roll out templates**
  action. In the modal, tick `strands-agentcore` and enable its **overwrite** toggle (the
  amber confirmation), then submit.
- **API (admin-gated):** `POST /api/v1/admin/connections/{id}/rollout`

  ```json
  { "template_names": ["strands-agentcore"], "overwrite": true, "overwrite_infra": false }
  ```

  Returns a per-item result; the `strands-agentcore` item reports `"overwritten"`.

> **`overwrite` no longer destroys anything, and it is one of two consents.**
>
> The destructive path is gone. A re-push used to `delete_repo` and then recreate from a zip;
> it is now `create_repo` (idempotent) followed by `commit_files` of the seed bytes — **one
> commit on top, history preserved, idempotent by content**
> (`services/template_rollout_service.py:44-50`; `delete_repo`/`create_repo_from_zip` are
> called by nothing here any more, `:245`). The result string `"overwritten"` kept its name but
> now means "re-pushed", and every reported action is derived from the *observed* repo state,
> not from the registry (`:207-217`).
>
> `overwrite` and `overwrite_infra` are **separate consents**, both defaulting to `false`
> (`api/routes/connections.py:527-535`). `overwrite` re-pushes the templates you selected and
> does **not** authorize pushing AGP's Terraform; the forced `agp-runtime-infra` repo has its
> own consent. Sending only `overwrite: true` therefore leaves the infra repo untouched.

Do this **per connected org**.

> **`migrate_to_e25b.py` does NOT cover this.** That migration is infra-only — it calls
> `rollout(template_names=[], overwrite=True, overwrite_infra=True)`
> (`scripts/migrate_to_e25b.py:23`, `:54-57`), and with an empty `template_names` it touches only
> the forced `agp-runtime-infra` repo, never the base template. `overwrite_infra=True` is the flag
> doing the forcing there: without it, an existing infra repo comes back `"skipped"`. Re-rolling
> out `strands-agentcore` is a **separate, explicit** step.
