# Databricks account onboarding — operator setup guide

> **Audience:** platform admins connecting a **Databricks workspace** to AGP so its Apps (and
> serving endpoints) can be discovered, registered, and governed under Entra ID identity.
> **Feature:** Epic 29 — Databricks as a second governed runtime platform.

## What "connecting a tenant" actually wires up

AGP governs a Databricks workspace through a **tenant** record (platform = `databricks`). To
create one you paste a handful of values into **Admin → Tenants → New tenant → Databricks**.

**AGP governs Databricks by federating the real caller's identity — so an account-admin
credential is required, not optional.** Federation is what makes the agent act *as the user who
invoked it*: their own Unity Catalog permissions, their own name in `system.access.audit`. Setting
it up needs one account-level act — amending the account **federation policy**'s `audiences[]` so
an Entra user's token can be exchanged at the workspace. That is a **one-time trust
configuration**, not a credential on the request path: AGP writes the audience and steps out, and
every per-call trust decision after that rides the user's own exchanged token
(`platform/control_plane/backend/src/services/databricks_identity_service.py:9-27` — federation is
the only binding AGP offers; there is no fallback).

Two credentials sit behind the tenant-form values:

| Credential | Who it is | What it does | Required? |
|---|---|---|---|
| **Workspace service principal** (client id + secret) | AGP's own machine hand on the workspace | discovery (`apps list`), provisioning, and **asserting app ACLs** (the per-user `CAN_USE` mirror) | **yes** |
| **Account-admin credential** (client id + secret) | a service principal holding the account's **`account_admin`** role | amending the **account federation policy** audiences that let a real Entra user's token be exchanged at the workspace | **yes — invoke is refused without it** |

Both come out of this guide: the helper script mints one account-level service principal, grants
it the `account_admin` role, and assigns it to the workspace, so its client id + secret fill both
fields. AGP holds only the `audiences[]` half of the federation policy and never creates one
(`databricks_identity_service.py:43-66` — account-level calls prefer the account-admin credential,
falling back to the workspace SP only when no account-admin credential was supplied;
`databricks_workspace_service.py:1-36` defines the REST client contract that never fetches
credentials, accepting them as parameters only).

### A note on what AGP does and does not control

Databricks has no inbound-auth setting to point at Entra — access to an App is governed by the
platform's **`CAN_USE`** ACL. AGP therefore **mirrors** each Entra grant to a per-user `CAN_USE`
entry (the Entra app-role assignment stays the source of truth), asserts the app's ACL at
provision time, and shows **drift** when someone edits the ACL around the platform
(`databricks_workspace_service.py:86-100` — the ACL vocabulary is closed and ranked, because a
write shape keeps one level per principal and the strongest must win). This is why the workspace
SP needs enough permission to *manage* app ACLs, not just read them — see `--scope` below.

## Prerequisites

- The [`databricks` CLI](https://docs.databricks.com/dev-tools/cli) installed
  (`brew install databricks/tap/databricks`).
- An **account-admin** login for the CLI:
  ```
  databricks auth login --host https://accounts.cloud.databricks.com --account-id <ACCOUNT_ID>
  ```
  (Your `account_id` is in the Databricks **account console** → dropdown by your username. It is
  never visible inside a workspace.)
- The **workspace id** you want AGP to govern (Account console → Workspaces).
- The workspace on **Premium or above** (required for SSO/AIM identity federation, which the
  governed invoke path relies on). New trial accounts are Premium by default — verify in the
  account console.
- Entra SSO + automatic identity management (AIM) configured on the same tenant, so Entra users
  resolve to Databricks users — this is what supplies the `user_sync` capability the probe checks
  for (`services/tenant_service.py:36-50` — capability probing runs at tenant create/update time;
  any probe failure closes all flags to False, yielding `invoke_unavailable`).

## Path A — the helper script (recommended)

`platform/control_plane/infrastructure/scripts/databricks-onboard.sh` creates the service
principal, mints its OAuth secret, grants it the account's `account_admin` role, assigns it to the
workspace, and prints the exact tenant-form values — enough for the account-admin half of a
**federation** tenant; the other half is user sync, which needs Entra SSO/AIM to be in place
already (see **Prerequisites** above — the script cannot create it). It is idempotent on the SP
(reuses one with the same name) and always mints a fresh secret (a Databricks OAuth secret is
shown once and cannot be re-read).

```
cd platform/control_plane/infrastructure/scripts
./databricks-onboard.sh \
    --account-profile <your-account-admin-CLI-profile> \
    --workspace-id     <numeric-workspace-id> \
    [--account-id      <account-id>]        # auto-detected from the profile if omitted
    [--name            agp-control-plane]   # the SP display name
    [--scope           admin]               # admin (default) | discover
```

**`--scope`:**

| | `admin` (default) | `discover` |
|---|---|---|
| Workspace role granted to the SP | **workspace admin** | **user** |
| Discovery (`apps list`) sees | every app + serving endpoint | only what the SP is explicitly granted |
| Asserting app ACLs (the `CAN_USE` mirror) | works out of the box | you must grant the SP **`CAN_MANAGE`** on each governed app first |
| Use when | you want the straightforward control-plane posture | you want the SP's *workspace* rights narrowed and will grant per-app access manually |

`discover` narrows only what the SP is granted **inside the workspace** — the same SP still holds
the account's `account_admin` role, which federation requires. It is least privilege on the data
plane, not on the account.

On success it prints:

```
Done — Databricks is instrumented for AGP.

Paste these into AGP → Admin → Tenants → New tenant → platform = Databricks:
  Workspace URL        https://dbc-XXXXXXXX-XXXX.cloud.databricks.com
  Workspace ID         <numeric>
  Cloud / Region       AWS / <region>
  Account ID           <account-id>
  SP client id         <uuid>
  SP client secret     <secret — shown once>

  Account-admin client id      <same uuid>
  Account-admin client secret  <same secret>
```

Copy the secret before closing the terminal. Paste all of it, including the account-admin pair —
that pair is what makes the tenant **federation**-capable *once Entra SSO/AIM is configured*
(AGP needs both `account_admin` and `user_sync`); without it the tenant connects but refuses
invoke.

## Path B — manual setup

If you cannot run the script (no CLI, restricted host), do the same steps by hand.

1. **Create an account service principal.**
   Account console → **User management → Service principals → Add service principal** →
   name it (e.g. `agp-control-plane`). Note its **Application ID** (a UUID) — that is the
   *client id*.
2. **Mint an OAuth secret.**
   On that SP → **Secrets → Generate secret**. Copy the **secret value** — it is shown once.
   (API equivalent: `POST /api/2.0/accounts/servicePrincipals/{sp_id}/credentials/secrets`.)
3. **Grant the SP the account's `account_admin` role** — this is what makes the tenant
   federation-capable. Account console → **User management → Service principals → <the SP> →
   Roles** → enable **Account admin**.
   (API equivalent: SCIM `PATCH /api/2.0/accounts/{account_id}/scim/v2/ServicePrincipals/{sp_id}`
   with `Operations: [{"op": "add", "path": "roles", "value": [{"value": "account_admin"}]}]`.)
4. **Assign the SP to the workspace.**
   Account console → **Workspaces → <your workspace> → Permissions → Add service principal** →
   grant **Admin** (matches `--scope admin`) or **User** (matches `--scope discover`).
5. **(discover scope only) Grant the SP `CAN_MANAGE` on each governed app** and `CAN_VIEW` on
   serving endpoints you want discovered — otherwise discovery sees nothing and ACL assertion
   is refused.

Then gather the tenant-form values:

| Field | Where to find it |
|---|---|
| Workspace URL | `https://<deployment-name>.cloud.databricks.com` (Account console → Workspaces) |
| Workspace ID | the numeric id (Account console → Workspaces) |
| Cloud / Region | AWS / the workspace's region |
| Account ID | Account console → dropdown by your username |
| SP client id | the SP's **Application ID** (step 1) |
| SP client secret | the secret from step 2 |
| Account-admin client id / secret | the same pair, once the SP holds `account_admin` (step 3) |

## Verify the credential (optional, recommended)

Confirm the SP can authenticate and discover before creating the tenant — this is exactly the
call AGP makes:

```bash
WS=https://<deployment-name>.cloud.databricks.com
TOKEN=$(curl -s -u "<client_id>:<client_secret>" \
    -X POST "$WS/oidc/v1/token" \
    -d grant_type=client_credentials -d scope=all-apis \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -s -H "Authorization: Bearer $TOKEN" "$WS/api/2.0/apps" \
    | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("apps",[])), "apps visible")'
```

A token and a non-empty app count (once you have at least one App) means the tenant will connect.

## After the tenant connects

- Watch the tenant's **capability badges**: `can_discover`, `user_sync`, `account_admin`, and a
  **readiness** badge — `Federation` when the account-admin credential + user sync are both
  present, otherwise **`Invoke unavailable — federation required`** naming what is missing
  (`tenant_service.py:125-139` defines the binding-mode vocabulary; the probe computes
  `binding_mode = "federation"` iff `account_admin AND user_sync`, else `"invoke_unavailable"`). A
  tenant in that state is still useful: it discovers, registers, catalogues, and observes agents,
  but **invoke is refused** rather than quietly downgraded to a shared identity
  (`databricks_identity_service.py:12-18` — refusing is the honest answer; provisioning a weaker
  binding would hand an operator a working agent whose audit story is the opposite of the one
  their tenant page promises). AGP never pretends to govern an invoke path it cannot see.
- A dormant service-principal binding exists for **non-human agents** (batch/system-to-system,
  where acting as a service identity is correct by design). It is off by default, gated behind
  `DATABRICKS_ALLOW_SP_SECRET_BINDING`, and never offered as a fallback for a tenant that cannot
  federate (`databricks_identity_service.py:20-27` — the probe cannot produce it and the caller
  never could; with the flag off it is refused as `sp_secret_disabled`).
- Register a discovered App, provision it, and grant a user — the grant writes both the Entra
  assignment **and** the app's `CAN_USE` (`api/routes/agents.py` invokes the provisioning path).
  Revoking kills both.
- The Access tab shows a **drift** panel if the app's ACL diverges from AGP's grants (someone
  edited `CAN_USE` in Databricks directly); **Re-assert** rewrites the platform ACL to match.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tenant shows `Invoke unavailable — federation required` badge | The probe found either no account-admin credential, or no user sync (Entra identities not synced to Databricks via AIM), or both | Supply both: the account-admin credential in the tenant form, and configure Entra SSO + automatic identity management on the Databricks account. Both must be present for `binding_mode = "federation"` (`tenant_service.py:125-139`). |
| `can_discover` capability is False | The workspace SP cannot authenticate, or has no workspace-level assignment, or (with `--scope discover`) lacks per-app `CAN_MANAGE` grants | Re-check the SP's client secret, verify the SP is assigned to the workspace, and for `discover` scope grant the SP `CAN_MANAGE` on each governed app. |
| `account_admin` capability is False | The SP does not hold the account's `account_admin` role, or the account-admin credential fields were left empty | Grant the SP the `account_admin` role in the Databricks account console (User management → Service principals → Roles), then update the tenant with the same client id + secret in the account-admin fields. |
| `user_sync` capability is False | Entra identities are not synced to the Databricks account — Entra SSO or automatic identity management (AIM) is missing or misconfigured | Configure Entra SSO and AIM on the Databricks account. Verify in the Databricks account console that Entra users appear as Databricks users. |
| Invoke returns `502 [...federation_unavailable]` | The agent's tenant cannot federate — either `account_admin` or `user_sync` is missing | See the first row. The full error names which capability is missing (`api/routes/agents.py:816-823`). |
| Invoke returns `502 [...sp_secret_disabled]` | The agent carries the dormant per-agent service-principal binding, which is disabled by default | Either move the tenant to federation (supply both account-admin and user sync), or set `DATABRICKS_ALLOW_SP_SECRET_BINDING=true` on the backend to enable the non-human-agent path (`agents.py:824-829`, `databricks_identity_service.py:20-27`). |
| Invoke returns `502 [...workspace_stage_unresolved]` | The tenant has more than one workspace stage, and AGP cannot tell which one hosts the agent — Databricks Apps hostnames carry no workspace identity | Have provisioning record which workspace stage the agent was bound in. This is a structural limit: a tenant with multiple workspaces cannot be disambiguated from the app URL alone (`agents.py:830-836`). |
| Invoke returns `502 [...federation_exchange_failed]` | The RFC 8693 token exchange at the workspace failed — the federation policy's issuer/subject/audience triple does not match the Entra token, or the user is not synced to Databricks | Verify the federation policy's `audiences[]` includes AGP's Entra audience (`api://agp` by default), and confirm the calling user exists in the Databricks workspace. Check the backend logs for the upstream Databricks error kind (never forwarded to the caller). |
| App invoked successfully yesterday, but today the agent's MCP tools silently vanish — no error, just an answer that ignores the tools | Apps on-behalf-of (OBO) was enabled on the Databricks App after the app was already running — Databricks does not reload the permissions on a live app | Stop the app, then start it again. Databricks Apps load OBO permissions at start time only; a live app will not pick up the change. |
| Secret rotation: updated the tenant's credential, but the old value still works (or the new one doesn't) | Secrets Manager secret names carry a recovery window — a deleted secret is scheduled for deletion (default 7 days) and can be restored if re-created with the same name during that window | If you meant to rotate: delete the old secret in Databricks account console, not Secrets Manager. If the new secret isn't working: verify you updated **both** the workspace SP and the account-admin fields (they are the same credential), then check the backend logs for a Secrets Manager read failure. The tenant service restores a secret scheduled for deletion if the name collides (`tenant_service.py:738-820`). |

---

## Rotating or removing the SP

- **Rotate the secret:** re-run the script (it mints a fresh secret on the existing SP) and
  update **both** credential fields in the tenant — they are the same pair. Old secrets can be
  revoked in the account console.
- **Remove access:** delete the SP in the account console (or remove its workspace assignment).
  AGP-governed apps keep their asserted ACLs until the app is torn down.
