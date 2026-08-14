# Microsoft Entra ID setup — screen-by-screen, with fill-in templates

AGP delegates identity to your identity provider, and Microsoft Entra ID is the provider supported
today. This is the long form of step 1 of [Bootstrapping](../README.md#bootstrapping); every value
maps to a Terraform variable or `VITE_*` key.

Two registrations and one group, in twelve steps. **Steps 2–9** build the backend registration
(*AGP — Backend Graph Client*) and produce five values; **step 10** is the frontend one
(*AGP — Frontend*) and produces one. Do them in that order — the SPA's delegated permission points at
a scope the backend has to be exposing already. **Step 11**, the group, is independent of both and can
be done whenever, as long as it is before the post-apply seed.

Within the backend steps the order also matters: **step 5 must exist before step 6 will let you
save.** After each step there is a `key = value` block to fill in as you go; step 12 assembles them
into the two config files.

After the twelve steps comes the reference half of this document:
[where every value goes](#where-every-value-goes),
[assigning users to the platform roles](#assign-users-to-platform-roles),
[Assignment required](#assignment-required-recommended),
[what the write permissions do at runtime](#what-the-write-permissions-do-at-runtime),
the [post-apply redirect-URI loop](#after-the-first-apply-close-the-redirect-uri-loop),
[verifying the result](#verify-it-works), and the [troubleshooting table](#troubleshooting).

**Who you need to be.** Any Entra tenant works; a free Microsoft 365 developer tenant is enough.
Prefer a dedicated dev tenant, because you grant tenant-wide admin consent. You need rights to create
app registrations (*Application Developer*), to grant admin consent for Microsoft Graph application
permissions (*Privileged Role Administrator* or *Global Administrator*), and to assign users and
groups to app roles (*Cloud Application Administrator*, or the owner). Steps 9 and 10 need that
consent right: an Application Developer can register the apps but not consent to them.

> ⚠️ **This file is tracked by git** (`docs/**` is not ignored), so anything you type below can be
> committed. Five of the six values are identifiers and not secret. **The client secret in step 4 is
> a real credential** — a bearer token for your directory's whole app-management surface. Its
> template is here for completeness, but the safe home is `secrets.auto.tfvars`, which *is* ignored
> via `.gitignore:6:**/*.tfvars`. If you fill step 4 in here, either keep this copy outside the working
> tree or clear it before committing.

---

## Step 1 — What you are creating

Two app registrations, five values from the first and one from the second, plus two tenant values and
a group. Nothing runs against your tenant unless you choose to run the optional setup script
([`scripts/entra-setup/`](../scripts/entra-setup/README.md)), which automates steps 2–10 and prints their
values; the step 11 group and the redirect URI are yours either way. This walkthrough is the by-hand path.

**a. Backend app registration** — a confidential client, e.g. *AGP — Backend Graph Client*. This is
the API: it is the `aud` of every inbound user token, **and it is where the platform roles live.**

| What to create | Where the value goes | Step |
|---|---|---|
| Application (client) ID | `entra_backend_client_id` | 3 |
| A client **secret** — copy the *Value* at creation, Entra never shows it again | `entra_backend_client_secret` | 4 |
| An **Application ID URI**, e.g. `api://agp` | `entra_audience` | 5 |
| An exposed **delegated scope**, e.g. `Access.Default` | the scope half of `VITE_ENTRA_SPA_SCOPE` | 6 |
| Three **app roles** (member type *Users/Groups*): `Platform.Admin`, `Platform.Operator`, `Platform.Viewer` | consumed from the token's `roles` claim | 7 |
| Six **Microsoft Graph application permissions** + admin consent | nothing to copy — mandatory anyway | 9 |

**b. SPA app registration** — platform type *Single-page application*, e.g. *AGP — Frontend*. This is
the public MSAL client the browser runs.

| What to create | Where the value goes | Step |
|---|---|---|
| Application (client) ID | `entra_spa_client_id` **and** `VITE_ENTRA_SPA_CLIENT_ID` | 10 |
| Delegated permission on the backend app's `Access.Default` scope | requested by MSAL at sign-in | 10 |
| A **redirect URI** | not knowable until AWS hands you a CloudFront domain — [after the first apply](#after-the-first-apply-close-the-redirect-uri-loop) | after the first apply |

**c. Tenant values** — the tenant ID (a GUID) for `entra_tenant_id` / `VITE_ENTRA_TENANT_ID`, both
picked up free in step 3, and the tenant domain for `VITE_ENTRA_TENANT_DOMAIN` (**Identity** →
**Overview** → **Primary domain**).

**d. A security group**, step 11 — its Object ID is an argument to the post-apply seed and is the one
Entra value that never goes into a config file.

### The list you will be working from

Everything above lives under one blade. Type `App registrations` into the **search bar** at the top of
<https://entra.microsoft.com> and pick it from the results (the nav path is **Identity** →
**Applications** → **App registrations**). The **All applications** tab shows four columns by default:

| Display name | Application (client) ID | Created on | Certificates & secrets |
|---|---|---|---|
| `AGP — Backend Graph Client` | | | |
| `AGP — Frontend` | | | |

Fill that in as you go and it doubles as the end-state check: two rows, two distinct GUIDs, and an
entry under *Certificates & secrets* on the **backend** row only. The SPA row staying empty there is
correct, not an oversight — a public client has no secret (step 10, *What the SPA does not get*).

Two things about this list are worth knowing before you need them:

- **Search matches both the display name and the client ID**, which is how you find a half-finished
  earlier attempt. If a name you expect to be free is taken, also check the **Deleted applications**
  tab — a deleted registration holds its identifier URIs for its full 30-day retention (step 5).
- **App registrations and Enterprise applications are different objects.** The registration is the
  definition; the enterprise application is its service principal in your tenant. You *define* app
  roles on the registration (step 7) and *assign* people to them on the enterprise application
  ([Assign users to platform roles](#assign-users-to-platform-roles)). Looking for role assignment on
  the registration blade is a dead end, and the enterprise-app blade for the **SPA** offers only
  *Default Access* — by design, because the SPA has no app roles.

The display names are yours to choose; nothing parses them. Only the GUIDs and the string values in
steps 5–7 are load-bearing. If you rename them, record what you used — later steps tell you to pick
the backend app out of a list by name.

```ini
# not variables — the names you actually used, if they differ from the examples
backend_app_display_name =
spa_app_display_name     =
```

---

## Step 2 — Create the backend registration

1. Go to <https://entra.microsoft.com> and confirm the account picker (top right) shows the tenant you
   want.
2. **App registrations** → **+ New registration**.
3. **Name**: `AGP — Backend Graph Client`.
4. **Supported account types**: pick the **first** radio — *Accounts in this organizational directory
   only (\<Your tenant\> only - Single tenant)*. Not either multitenant option, and not personal
   accounts. See the note below.
5. **Redirect URI**: leave both the dropdown and the box empty — this app never receives a browser
   redirect.
6. Click **Register**.

> **Why single-tenant, and why it is not a preference.** The backend pins the JWT issuer to
> `https://login.microsoftonline.com/<your-tenant-id>/v2.0` and takes signing keys from that tenant's
> JWKS endpoint only, so a token from any other directory is refused with `401 Invalid issuer`. A
> multitenant registration would let other tenants' users obtain tokens the backend then rejects. The
> two options that include personal Microsoft accounts are worse still: Xbox/Skype accounts are not
> directory users, so they cannot hold `Platform.*` app-role assignments, which is the whole
> authorization model.
>
> Entra warns on that panel that switching audiences **after** registering can error. It is quicker to
> delete the registration and start again than to change this later, so get it right on the first pass.

You land on the app's **Overview** blade. Everything in steps 3–9 is reached from that blade's
left-hand menu.

No values from this step.

---

## Step 3 — Application (client) ID → `entra_backend_client_id`

It's already on screen. In the **Essentials** panel at the top of **Overview**, the second field is
**Application (client) ID**, a GUID with a copy-to-clipboard icon beside it.

- Click the copy icon → paste into `entra_backend_client_id` in `secrets.auto.tfvars`.

**While you're here:** the same panel shows **Directory (tenant) ID**. That's the tenant GUID — copy
it now into both `entra_tenant_id` and `VITE_ENTRA_TENANT_ID` and save yourself a trip to the tenant
Overview page.

Do not confuse **Object ID** (also in this panel) with the client ID. Object ID is the directory
record's own identifier and no AGP variable wants it.

```ini
# → secrets.auto.tfvars
entra_backend_client_id =
entra_tenant_id         =

# → frontend/.env          (same GUID as entra_tenant_id)
VITE_ENTRA_TENANT_ID    =
```

---

## Step 4 — Client secret → `entra_backend_client_secret`

This is the one-shot value. Have the file open before you click Add.

1. Left menu of the app → under *Manage* → **Certificates & secrets**.
2. Make sure you're on the **Client secrets** tab (not *Certificates*, not *Federated credentials*).
3. Click **+ New client secret**. A panel opens on the right.
4. **Description**: `AGP backend Graph client`.
5. **Expires**: choose **730 days (24 months)** — the longest preset, and what the Terraform
   variable's description assumes. *Custom* allows longer if your policy permits.
6. Click **Add**.

The secret now appears as a row in the table with four columns: **Description | Expires | Value |
Secret ID**.

- Copy the **Value** column — it has its own copy icon. Paste it into `entra_backend_client_secret`.
- **Ignore Secret ID.** It's the metadata handle, not the credential, and pasting it produces a stack
  that deploys fine and then fails every Graph call.

Once you navigate away or refresh, the Value is permanently masked. There is no recovery — you delete
the secret and create a new one. Also note the expiry date somewhere: when it lapses, every Graph call
fails at once and sign-in keeps working, which is a confusing combination to debug cold.

```ini
# → secrets.auto.tfvars       ⚠️ credential — see the warning at the top of this file
entra_backend_client_secret =

# not a variable — for your own calendar
secret_expires_on           =
```

---

## Step 5 — Application ID URI → `entra_audience`

**Do sub-step 1 first — it is required for AGP to work at all, and it also prevents the first of the
two errors below.**

1. Left menu → **Manifest** → set the token version to `2` → **Save**:
   - *Microsoft Graph App Manifest* — `"requestedAccessTokenVersion": 2`, nested inside the `"api"`
     object
   - *AAD Graph App Manifest* (legacy toggle) — top-level `"accessTokenAcceptedVersion": 2`
2. Left menu → **Expose an API**.
3. At the top, next to **Application ID URI**, click **Add**.
4. An inline box appears prefilled with `api://<client-id-guid>`. **Delete that and type** your chosen
   URI — `api://agp` if it is free in your tenant.
5. Click **Save**.

You should see it displayed at the top of the blade. Copy it into `entra_audience`.

> **Why sub-step 1 is not optional.** A portal-created app defaults this field to `null`, meaning v1 —
> and **the resource app decides an access token's issuer version**. `core/security_entra.py:48` builds
> one expected issuer, `https://login.microsoftonline.com/<tenant-id>/v2.0`, and enforces it with
> `verify_iss: True`. Left at `null`, every token the SPA gets carries
> `iss: https://sts.windows.net/<tenant-id>/` and the backend refuses all of them with
> `401 Invalid issuer` — while sign-in itself looks like it worked.

### If Save fails

**`All newly added URIs must contain a tenant verified domain, tenant ID, or app ID`** — a current
default tenant policy. Sub-step 1 lifts it; if you skipped it, do it now. Otherwise use a compliant
form (`api://<client-id>`, `api://<tenant-id>/agp`, `https://<verified-domain>/agp`) and carry that
longer string everywhere, scope included.

**`Another object with the same value for property identifierUris already exists`** — identifier URIs
are unique tenant-wide. Check in this order:

1. **This app already has it** — *Expose an API* may already show it from a previous attempt. Then
   you are done; go to step 6.
2. **A stray duplicate** — *App registrations* → **All applications**, search `agp`.
3. **A soft-deleted app** — *App registrations* → **Deleted applications**. A deleted registration
   **keeps its identifier URIs for its full 30-day retention**; deleting is not enough, it must be
   **permanently** deleted to release the string.
4. **Find the holder** — Graph Explorer:
   `GET https://graph.microsoft.com/v1.0/applications?$select=displayName,identifierUris&$top=999`

**Using your own unique string is fully supported** — it is arbitrary; only tenant-uniqueness and
byte-identical repetition matter. But it becomes **mandatory** in both config files, because the repo
defaults to `api://agp` independently in each: `entra_audience` at `variables.tf:215` and
`modules/ecs/variables.tf:282`, and `msalConfig.ts:22` falling back to `'api://agp/Access.Default'`
when `VITE_ENTRA_SPA_SCOPE` is empty. Omit either and the two halves disagree silently.

Leave `AGENT_APP_AUDIENCE_PREFIX` (`api://agp-agent-`) and `MCP_APP_AUDIENCE_PREFIX`
(`api://agp-mcp-`) at `core/config.py:180-181` **unchanged** — different namespaces, for the per-agent
and per-MCP registrations the platform mints, and collision-safe via the id they append.

Then paste this same string as the first half of `VITE_ENTRA_SPA_SCOPE` in `frontend/.env`. It has to
be byte-identical in the Entra field, `entra_audience`, and that `VITE_` key — mismatches show up as a
`401 Invalid audience` on every call, with nothing indicating which of the three is wrong.

```ini
# → secrets.auto.tfvars
entra_audience =
```

---

## Step 6 — Exposed delegated scope → the scope half of `VITE_ENTRA_SPA_SCOPE`

Same blade, directly below what you just saved.

1. Under **Scopes defined by this API**, click **+ Add a scope**.
2. **Scope name**: `Access.Default` (this is the value the step refers to — the scope *name*).
3. **Who can consent?**: select **Admins and users**.
4. **Admin consent display name**: `Access AGP`.
5. **Admin consent description**: `Allows the signed-in user to call the AGP API.`
6. Leave the two *User consent* fields empty — optional.
7. **State**: **Enabled**.
8. Click **Add scope**.

The scope now lists as `api://agp/Access.Default` in the table. **That full string — not just the
name — is what goes into `VITE_ENTRA_SPA_SCOPE`.** The variable carries URI and name together; nothing
carries the name alone, which is why renaming the scope forces you to set this key explicitly rather
than relying on the code's fallback.

If sub-step 1 instead prompts you to set an Application ID URI, you skipped step 5.

```ini
# → frontend/.env             the FULL string: <step 5 value>/<scope name>
VITE_ENTRA_SPA_SCOPE =

# not a variable — recorded only if you renamed it from Access.Default
scope_name           =
```

---

## Step 7 — Three app roles → consumed from the token's `roles` claim

No config file for these; the backend reads them from the token. Create them **here, on this backend
registration** — this is the mistake that costs people the most time, because putting them on the SPA
app is a silent no-op with no error anywhere.

1. Left menu → **App roles**.
2. Click **+ Create app role**. A panel opens.
3. Fill in:
   - **Display name**: `Platform Admin`
   - **Allowed member types**: **Users/Groups** (not *Applications*, not *Both*)
   - **Value**: `Platform.Admin`
   - **Description**: `Full platform administration.`
   - Leave **Do you want to enable this app role?** checked.
4. Click **Apply**.
5. Repeat twice more:

| Display name | Value | Description |
|---|---|---|
| Platform Operator | `Platform.Operator` | Create, edit, and delete agents; issue grants; author policies. |
| Platform Viewer | `Platform.Viewer` | Read-only access. |

The **Value** strings must match exactly — no spaces, correct dots, correct capitalisation. They're
hardcoded backend defaults in `core/config.py`, not Terraform variables, so a typo here means editing
backend code rather than a config file. The Display name and Description are yours; only Value is
load-bearing.

Creating the roles does **not** assign anyone to them. That's a separate action, on **Enterprise
applications** → this app → **Users and groups**
([Assign users to platform roles](#assign-users-to-platform-roles)). If that role picker offers only
*Default Access*, you are either on the SPA app or these three roles did not save — see step 8.

```ini
# no variables — confirm each one exists with member type Users/Groups (yes / no)
role_platform_admin_created    =
role_platform_operator_created =
role_platform_viewer_created   =
```

---

## Step 8 — Verify the backend's five in one view

Left menu → **Manifest**. The JSON shows every value you just set, so you can eyeball them together:

- `"appId"` → step 3
- `"identifierUris"` → step 5, holding your chosen URI
- `"requestedAccessTokenVersion": 2` → step 5 sub-step 1 (inside `"api"`). **If this reads `null`,
  every API call will 401 on issuer** — fix it before going further.
- `"oauth2PermissionScopes"` → step 6, with `"value": "Access.Default"`
- `"appRoles"` → step 7, three entries, each `"allowedMemberTypes": ["User"]`

The secret is deliberately absent — secrets never appear in the manifest.

`"allowedMemberTypes": ["User"]` is what *Users/Groups* serialises to; if you see `["Application"]` you
picked the wrong radio and that role will never land in a user's token. It also will not appear in the
**Users and groups** role picker on the enterprise app, which is the symptom you notice first.

```ini
# checked in the manifest (yes / no)
manifest_appid_matches            =
manifest_identifieruris_ok        =
manifest_token_version_is_2       =
manifest_scope_ok                 =
manifest_approles_user_type_ok    =
```

---

Only one of these five cannot be recovered later: the secret **Value** from step 4, gone on refresh.
The other four are re-readable from the portal any time. So if you get interrupted, the secret is the
only thing worth restarting for.

---

## Step 9 — Six Graph application permissions — grant, consent, verify

Still on the **backend** registration. This step produces no value to copy, and without it the platform
starts fine and then fails the first time anyone registers an agent — the Graph call 403s and the error
surfaces in the UI as a generic failure.
The table below is the reasoning; this blade is where you confirm the result. (What the write
permissions provision — and delete — at runtime is
[covered below](#what-the-write-permissions-do-at-runtime).)

Left menu → **API permissions**. You are reading a table with these columns: *API / Permissions name*,
**Type**, *Description*, *Admin consent required*, **Status**. All six below must be present, and for
each one **Type** must read `Application` and **Status** must read `Granted for <your tenant>`.

| Permission | Why the platform needs it |
| --- | --- |
| `Application.ReadWrite.All` | Creates the per-agent and per-MCP app registrations + service principals, mints their secrets, flips `appRoleAssignmentRequired`, deletes them on cascade |
| `AppRoleAssignment.ReadWrite.All` | Every user→agent and agent→MCP grant, plus the platform-role assignments |
| `DelegatedPermissionGrant.ReadWrite.All` | The `oauth2PermissionGrants` behind on-behalf-of flows |
| `User.Read.All` | Principal search, the admin panel, transitive group membership |
| `Group.Read.All` | Group principals in search, grants, and the governance graph |
| `Directory.Read.All` | Governance-graph aggregation |

`User.ReadWrite.All` is deliberately **not** in that list — the platform never creates or modifies
directory users. Do not add it. The `User.Read` **Delegated** permission that Entra adds to every new
registration is harmless; leave it alone rather than tidying it away.

> Two traps, both of which leave a green-looking screen:
>
> 1. **Type says `Delegated` instead of `Application`.** The *Request API permissions* blade opens on
>    **Delegated permissions** by default, so a permission picked without first clicking **Application
>    permissions** has the same name and the wrong semantics. The backend calls Graph as itself, with no
>    signed-in user, so a Delegated grant is never used.
> 2. **A later addition is unconsented.** **Grant admin consent for \<tenant\>** only covers the
>    permissions listed at the moment you click it. Add a seventh permission afterwards and the first six
>    still read *Granted* while the new one sits at *Not granted* — click the button again after **any**
>    change.

The authoritative cross-check for what is actually consented (rather than merely requested) lives
elsewhere in the portal: **Entra admin center** → **Enterprise applications** → *AGP — Backend Graph
Client* → **Permissions** → the **Admin consent** tab. The API permissions blade shows your request; this
shows the tenant's answer.

Fastest single answer, if you would rather not read a table: left menu → **Manifest** → find
`"requiredResourceAccess"`. You want one entry whose `"resourceAppId"` is
`00000003-0000-0000-c000-000000000000` (that GUID *is* Microsoft Graph, in every tenant), and whose
`"resourceAccess"` array holds **six** objects with `"type": "Role"`. In the manifest, `Role` means
application permission and `Scope` means delegated — so six `Role` entries is the whole check. Consent
status is not in the manifest; that still needs one of the two views above.

```ini
# confirmed in the portal (yes / no)
graph_six_permissions_present  =
graph_all_type_application     =
graph_all_status_granted       =
```

---

## Step 10 — SPA registration → `entra_spa_client_id`

A second registration, and the last portal object in the app-registrations blade. It yields **one**
value plus two actions that produce nothing to copy and are still mandatory.

**Prerequisite: step 6 must be done.** The **My APIs** tab in sub-step 9 lists only apps that expose a
scope. Skip step 6 and the backend app simply is not in that list, with nothing on screen explaining
why.

### Create it

1. Confirm the account picker (top right) still shows the tenant you used for the backend app.
2. **App registrations** → **+ New registration**.
3. **Name**: `AGP — Frontend`.
4. **Supported account types**: the **first** radio — single tenant. Same reason as the backend: the
   issuer is pinned to this one directory.
5. **Redirect URI**: set the **Select a platform** dropdown to **Single-page application (SPA)**, then
   type `http://localhost:5173/auth/callback` in the box beside it.
6. Click **Register**. The **Essentials** panel on **Overview** now shows **Application (client) ID** —
   that is the value.

> **SPA, not Web.** *Web* creates a confidential client that expects a client secret; MSAL v5 uses the
> authorization-code flow with PKCE and has no secret to send, so Entra rejects the exchange. Correcting
> it later means deleting the URI and re-adding it under the right platform — the dropdown is not
> editable in place.
>
> `http://localhost:5173/auth/callback` is the `msalConfig.ts:21` fallback, so `npm run dev` works
> without further configuration. The production redirect URI is a CloudFront domain that does not exist
> until Terraform has run;
> [after the first apply](#after-the-first-apply-close-the-redirect-uri-loop) closes that loop. Do not
> guess it.

### Grant it the backend's scope

7. Left menu → **API permissions** → **+ Add a permission**.
8. Three tabs appear: *Microsoft APIs* | *APIs my organization uses* | **My APIs**. Choose **My APIs**.
9. Select **AGP — Backend Graph Client**.
10. Choose **Delegated permissions** — not *Application permissions* — tick **`Access.Default`**, then
    **Add permissions**.
11. Back on the blade, click **Grant admin consent for \<your tenant\>** → **Yes**. The Status column
    turns into a green check.

Without step 11 every user gets a consent prompt on first sign-in, and where user consent is disabled
tenant-wide, sign-in fails outright with `AADSTS65001`.

### If **My APIs** is empty

The tab lists only apps that both expose a delegated scope **and** that you own, so an empty list has two
causes. Check them in this order — the first is far more likely.

**1. Does the backend app actually expose a scope?**

**AGP — Backend Graph Client** → **Expose an API**. Under *Scopes defined by this API* there must be a row
reading `<your Application ID URI>/Access.Default` with **State: Enabled**.

- *Nothing there* → step 6 was never saved. Do it now (**+ Add a scope**, name `Access.Default`, who can
  consent **Admins and users**, State **Enabled**, **Add scope**), then come back to sub-step 8.
- *A row exists but State says Disabled* → a disabled scope does not appear in the picker. Click it and
  enable it.
- *You added it moments ago* → wait a minute and hard-refresh. A newly exposed scope takes a short while
  to surface on another app's permission picker.

**2. Are you an owner of the backend app?**

Backend app → **Owners**. If your account is not listed, add it. Portal registration normally adds the
creator automatically, but when the registration was made under a different account — or the auto-add
simply did not happen — this tab is empty even though the scope is configured correctly.

**The workaround that needs neither.** Use the middle tab, **APIs my organization uses**, instead: it
lists every service principal in the tenant regardless of ownership. Search the app name, its client ID,
or the Application ID URI. The outcome is identical — the same `oauth2PermissionGrants` record either way.

**Settling it in one look:** backend app → **Manifest** → `"oauth2PermissionScopes"`. An empty `[]` is
proof that step 6 never saved, and no amount of retrying sub-step 8 will help until it does.

### What the SPA does *not* get

No client secret, no Graph application permissions, and **no app roles**. All three belong to the backend
registration. A role defined or assigned here never reaches the token — a silent no-op with no error
anywhere, and the costliest mistake in this whole setup. It is also why this app's **Users and groups**
blade offers only *Default Access*: there is nothing else there to offer.

The same GUID goes in **both** files. The frontend builds its MSAL client from it; the backend accepts it
as an alternative `aud` because Entra's v2.0 access tokens sometimes carry `aud=<client-id-GUID>` instead
of the Application ID URI. Fill only one and a fraction of otherwise valid tokens 401 with nothing
distinguishing them from the rest.

```ini
# → secrets.auto.tfvars
entra_spa_client_id      =

# → frontend/.env          (same GUID)
VITE_ENTRA_SPA_CLIENT_ID =

# confirmed in the portal (yes / no)
platform_is_spa_not_web  =
access_default_consented =
```

---

## Step 11 — Tenant membership group → `--group-id`

Not a registration and **not a config-file value** — the one Entra value in this whole setup that never
gets written into a file. It is an argument you type on the seed command after the first apply.

**New group or existing?** Either. Nothing in the platform ever creates a group, and any security group
in the tenant works. A dedicated one is cleaner for a first deployment, because its membership *is* the
tenant's membership and you probably do not want that to be some pre-existing distribution list.

### Create it

1. **entra.microsoft.com** → **Identity** → **Groups** → **All groups** → **+ New group**.
2. **Group type**: **Security**. Not *Microsoft 365* — that also provisions a mailbox and a Teams team you
   have no use for. Either works technically; Security is the honest one for an authorization boundary.
3. **Group name**: e.g. `AGP Default Tenant`. Nothing parses it; only the Object ID is load-bearing.
4. **Group description**: optional. Worth one line — future-you will wonder what the group is wired to.
5. **Microsoft Entra roles can be assigned to the group**: **No**. This is `isAssignableToRole`, and it
   concerns **directory** roles (Global Administrator and friends) — a different axis from this
   platform's app roles and from tenant membership, neither of which touches it. It is also
   **permanent**: the flag cannot be changed after creation, so a stray *Yes* means deleting the group
   and re-running the seed with a new Object ID. It additionally needs P1/P2, requires Privileged Role
   Administrator to create, and turns the group into a protected object whose membership only
   privileged callers may edit. It breaks nothing — the platform never writes to groups, only reads
   them — it is simply unnecessary privileged surface.
6. **Membership type**: **Assigned**. *Dynamic* needs an Entra ID **P1/P2** licence, and Global
   Administrator does not substitute for the licence.
7. **Owners**: optional. **Members**: add yourself, plus anyone who should be a member of the tenant. This
   is editable afterwards from the group's **Members** blade, so an empty group now is not a mistake.
8. **Create**.
9. Reopen the group from **All groups** → **Overview**.
10. Copy **Object ID** — the field with that exact label. Not the group name, not the mail nickname.

> **Your own membership is not what makes the admin view work.** `Platform.Admin` resolves to
> `is_global=True` (`tenant_resolver.py:71`) and sees every tenant's resources regardless of groups.
> Membership decides what **operators and viewers** see: their visible tenants come from intersecting
> their group set with each tenant's `entra_group_ids` (`tenant_resolver.py:75-77`). Nested groups are
> fine — the lookup is `transitiveMemberOf`.

> **Do not add a `groups` optional claim under the backend app's Token configuration.** The resolver
> decides its group source **by key presence**: a `groups` claim that is present is authoritative and
> Graph is *not* consulted, even when the claim is empty (`tenant_resolver.py:_group_ids`, documented at
> the top of the module). Entra also truncates that claim past ~150–200 groups and substitutes an
> overage indicator. With no claim configured, the backend falls back to Graph `transitiveMemberOf` —
> which is why `User.Read.All` is in the six permissions — and that path has neither failure mode.

### Where the Object ID goes

Into the `--group-id` flag on the post-apply seed, whose full command lives at
[`modules/default_tenant/main.tf:16-30`](../platform/control_plane/infrastructure/modules/default_tenant/main.tf) —
copy it from there, since every other argument is a `terraform output` lookup and this is the only value
you substitute by hand:

```bash
cd platform/control_plane/backend
PYTHONPATH=src venv/bin/python scripts/seed_default_tenant.py \
  ... \
  --group-id "<paste-the-object-id>"          # add --dry-run first to see the plan
```

`export DEFAULT_TENANT_GROUP_ID=<guid>` works instead of the flag
(`seed_default_tenant.py:106,795`).

> **Do not put it in `secrets.auto.tfvars` or `frontend/.env`.** `variables.tf:265-268` records that
> `default_tenant_group_id` used to be declared and was removed when the seed stopped being a Terraform
> `local-exec`; no module consumes it now, so it would be an undeclared variable with no effect.
>
> It is required, though — not cosmetic. `tenant_service.py:214` rejects any tenant with an empty group
> list ("A tenant requires at least one Entra group") on **both** the seed path and the API/UI path, and
> `seed_default_tenant.py:795-800` exits `2` without it. Until a tenant exists, `POST /agents` answers
> `400 unknown tenant`, projects cannot be created, and runtime builds are refused with
> `unknown tenant or stage`.

```ini
# not a config file — keep this to hand for the post-apply seed
group_object_id     =
group_members_added =    # yes / no — editable later; only affects operators and viewers
```

---

## Step 12 — Transfer into the two config files

Everything above, collected. Paste your filled values into the real files; these blocks are the shape
each file should end up in. (The full variable-by-variable reference, including the traps on each
side, is [Where every value goes](#where-every-value-goes) below.)

`platform/control_plane/infrastructure/secrets.auto.tfvars`

```hcl
entra_tenant_id             = ""   # step 3
entra_audience              = ""   # step 5
entra_backend_client_id     = ""   # step 3
entra_backend_client_secret = ""   # step 4 — the Value column, never the Secret ID
entra_spa_client_id         = ""   # step 10
langfuse_admin_password     = ""   # not from Entra — nothing validates it; this becomes the real login
langfuse_admin_email        = ""   # not from Entra — same
```

> Fill in the file named `secrets.auto.tfvars`, **not** `secrets.auto.tfvars.example`. The example is
> tracked by git; the real one is ignored. They sit side by side with near-identical contents, and a real
> client secret typed into the wrong one is staged by the next `git add .`.

`platform/control_plane/frontend/.env`

```bash
VITE_ENTRA_TENANT_ID=            # step 3 — same GUID as entra_tenant_id
VITE_ENTRA_SPA_SCOPE=            # step 6 — full string, e.g. api://agp/Access.Default
VITE_ENTRA_SPA_CLIENT_ID=        # step 10 — same GUID as entra_spa_client_id
VITE_ENTRA_TENANT_DOMAIN=        # not from a registration — Identity → Overview → Primary domain
```

> `VITE_ENTRA_TENANT_DOMAIN` is the one value nothing checks for you. `deploy-full.sh:148` refuses to
> deploy while any of the four required `VITE_` variables is unusable, but `frontend_var_unusable`
> (`deploy-full.sh:86`) only rejects empty strings, `<…>` stubs, and all-zero GUIDs. A leftover
> `contoso.onmicrosoft.com` looks like a real domain to that test and sails through the preflight.

- [ ] Confirm `git check-ignore -v secrets.auto.tfvars` prints `.gitignore:6:**/*.tfvars`, run from
      the `infrastructure` directory — from anywhere else it matches the bare filename and proves
      nothing.

Three actions on the **backend** registration produce no value to record, but the platform does not work
without them:

- [ ] **Six Graph application permissions + admin consent** —
      [step 9](#step-9--six-graph-application-permissions--grant-consent-verify). Unconsented permissions
      do nothing, and the failure surfaces only when you first register an agent.
- [ ] **Assignment required = Yes** on the enterprise app —
      [Assignment required](#assignment-required-recommended) (recommended). Note which way you set
      it, because it decides which of two failures an unassigned user hits: **Yes** → sign-in is refused
      with the named `AADSTS50105`; **No** → sign-in succeeds and a quiet `403` appears several screens
      later, which reads like a platform bug.

```ini
# how you set it (Yes / No)
assignment_required =
```

- [ ] **At least one user assigned to `Platform.Admin`** —
      [Assign users to platform roles](#assign-users-to-platform-roles), on the **backend** app, not
      the SPA.

---

## Where every value goes

Your Entra values live in two gitignored files — `secrets.auto.tfvars` and the frontend `.env`. A third,
`terraform.tfvars`, carries no Entra values but the deploy expects it, so copy all three from the tracked
examples:

```bash
cd platform/control_plane/infrastructure && cp terraform.tfvars.example terraform.tfvars
cp secrets.auto.tfvars.example secrets.auto.tfvars && cd ../frontend && cp .env.example .env
cd ../infrastructure   # go back — the check-ignore in step 12 must run where the file actually is
```

`terraform.tfvars` ships with defaults that work as-is for a single-account dev deploy; step 2 of
[Bootstrapping](../README.md#bootstrapping) covers the few keys most people touch. Terraform **auto-loads
any `*.auto.tfvars`**, so there is no `-var-file` flag to remember.

> **Replace the two Langfuse placeholders in `secrets.auto.tfvars`.** The example ships
> `langfuse_admin_password = "REPLACE_WITH_A_STRONG_PASSWORD"` and `langfuse_admin_email = "admin@example.com"`,
> and nothing validates them — both default to `""`, so `terraform apply` succeeds and seeds Langfuse with the
> literal placeholder as its admin password. The password needs letters, numbers, and one special character.

### Backend — `platform/control_plane/infrastructure/secrets.auto.tfvars`

Every variable below is declared in `variables.tf` and reaches the backend container as an environment
variable through the ECS task definition.

| Terraform variable | Value from | Default | Backend env var |
|---|---|---|---|
| `entra_tenant_id` | Tenant ID (step 3) | `""` | `ENTRA_TENANT_ID` |
| `entra_audience` | Backend Application ID URI (step 5) | `"api://agp"` | `ENTRA_AUDIENCE` |
| `entra_backend_client_id` | Backend Application (client) ID (step 3) | `""` | `ENTRA_BACKEND_CLIENT_ID` |
| `entra_backend_client_secret` | Backend client secret **value** (step 4) | **none — required** | `ENTRA_BACKEND_CLIENT_SECRET`, via AWS Secrets Manager |
| `entra_spa_client_id` | SPA Application (client) ID (step 10) | `""` | `ENTRA_SPA_CLIENT_ID` |
| `auth_provider` | optional | `"entra"` | `AUTH_PROVIDER` |

`auth_provider` accepts only `entra`; anything else fails the plan, and it is also the default. The empty-string
defaults mean **Terraform will not stop you** from applying with the Entra values missing: the stack comes up
healthy, and every authenticated API request then fails with a bare `500 Internal server error` whose real
reason (`ENTRA_TENANT_ID is not configured`) goes to CloudWatch, not to the client.

### Frontend — `platform/control_plane/frontend/.env`

The SPA declares exactly seven variables in `platform/control_plane/frontend/src/vite-env.d.ts`, and reads six of them:

| `VITE_*` key | Value from | Read by |
|---|---|---|
| `VITE_API_URL` | `terraform output -raw api_endpoint` — **post-apply** | the API client |
| `VITE_AUTH_PROVIDER` | `entra` | **nothing in the SPA** — the fail-fast guard no longer consults it (below). Still written by `deploy-full.sh`, and still expected to match the backend's `AUTH_PROVIDER` |
| `VITE_ENTRA_TENANT_ID` | Tenant ID (step 3) — same value as `entra_tenant_id` | `msalConfig.ts` → MSAL authority |
| `VITE_ENTRA_TENANT_DOMAIN` | Tenant domain (step 12) | display only in the UI, and no Terraform twin — **but `deploy-full.sh` refuses to deploy without it** |
| `VITE_ENTRA_SPA_CLIENT_ID` | SPA client ID (step 10) — same value as `entra_spa_client_id` | `msalConfig.ts` → MSAL `clientId` |
| `VITE_ENTRA_SPA_REDIRECT_URI` | `<frontend_url>/auth/callback` — **post-apply**, [below](#after-the-first-apply-close-the-redirect-uri-loop) | `msalConfig.ts` |
| `VITE_ENTRA_SPA_SCOPE` | `<application-id-uri>/<scope-name>`, e.g. `api://agp/Access.Default` (steps 5–6) | `msalConfig.ts` → `apiScopes` |

Fill in the five you already know; the other two come from the first deploy. Four of the five are a hard deploy gate:
`deploy-full.sh` resolves `VITE_ENTRA_TENANT_ID`, `VITE_ENTRA_TENANT_DOMAIN`, `VITE_ENTRA_SPA_CLIENT_ID`, and
`VITE_ENTRA_SPA_SCOPE` in preflight, and exits with *"Refusing to deploy"* if any is empty, still angle-bracketed, or
an all-zero GUID. `VITE_ENTRA_SPA_REDIRECT_URI` is exempt — it does not exist until the first apply.

Two of them are a hard **build** gate as well. `auth/authConfigGuard.ts` throws at module load when
`VITE_ENTRA_TENANT_ID` or `VITE_ENTRA_SPA_CLIENT_ID` is missing, angle-bracketed, or an all-zero GUID — the same
predicate the preflight uses, deliberately, so the two gates cannot disagree about what "configured" means. The guard
is **unconditional**: `VITE_AUTH_PROVIDER` used to gate it, which made it inert on every build path that does not
write that key (`deploy-frontend.sh`, a bare `npm run build`, CI) and shipped an empty client ID against the `/common`
multi-tenant authority instead — a sign-in that completes against the wrong directory and then fails every API call
on issuer. A configured tree is unaffected: Vite loads `.env` as the base file in both dev and production modes.

> **`.env` is where you edit; `.env.production` is what the build reads.** `deploy-full.sh` reads `.env` and
> regenerates `.env.production` from it, and Vite ranks `.env.production` above `.env` for a production build.
> So editing `.env` and then running the frontend-only `deploy-frontend.sh` — which generates nothing — ships
> the previous values and reports success. See step 5 of [Bootstrapping](../README.md#bootstrapping) for the
> two supported ways out; both scripts live in `platform/control_plane/infrastructure/scripts/`.

### Values with no variable

Four things you configure in Entra have no Terraform variable or `VITE_*` key, and are fixed by convention. The three
app-role values are backend defaults in `core/config.py`, so renaming them in Entra means changing backend
configuration. The scope name `Access.Default` is not: it only ever appears as the second half of
`VITE_ENTRA_SPA_SCOPE`, so renaming it means setting that key (step 6), nothing backend-side. Which app registration
holds the `Platform.*` assignments defaults to the backend app, overridable only through the
`ENTRA_PLATFORM_APP_CLIENT_ID` backend setting, which Terraform does not wire. And the **group object ID** for the
default tenant seed (step 11) is passed as `--group-id` to `platform/control_plane/backend/scripts/seed_default_tenant.py`
(step 6 of Bootstrapping; demo only).

## Assign users to platform roles

At least one user must hold a platform role, or nobody can do anything after signing in. **Enterprise
applications** → **your backend app** → **Users and groups** → **Add user/group** → pick the principal, then pick
`Platform.Admin`, `Platform.Operator`, or `Platform.Viewer`. Groups work too and are the better choice beyond a
demo, but group assignment requires an Entra ID P1 or P2 licence.

> **Again: the backend app, not the SPA app.** An assignment made on the SPA app's service principal is a
> silent no-op — see [step 7](#step-7--three-app-roles--consumed-from-the-tokens-roles-claim).

### What each role can do

The backend maps the token's `roles` claim onto an ordered enum — `VIEWER (0) < OPERATOR (1) < ADMIN (2)` —
and each route declares a minimum:

| Role | Grants |
|---|---|
| `Platform.Viewer` | Read-only. Every `GET` across the registry, grants, graph, marketplace, and observability. |
| `Platform.Operator` | Viewer, plus mutations: create, edit, and delete agents and MCP servers, drive lifecycle transitions, issue and revoke grants, author policies. |
| `Platform.Admin` | Operator, plus lifecycle and administration: approvals, **marketplace** publish/unpublish, tenant configuration, and the platform-users panel — the **Users** tab of the Admin console (`/admin` in the UI). |

Two things the table cannot show. There are two "publish" verbs: an agent's cross-tenant publish flag
(`PUT /agents/{id}/publish`) is OPERATOR, a *marketplace* listing is ADMIN. And a platform role is a floor, not
the whole gate: project-scoped routes add an independent per-project check, a caller who cannot see a resource's
tenant gets 404 rather than 403, and the highest role wins when a caller holds several.

### What an unassigned user sees

Two different failures, depending on [Assignment required](#assignment-required-recommended). With **Assignment
required = Yes**, Entra refuses at sign-in with `AADSTS50105` — *"The signed in user is not assigned to a role for
the application"* — and issues no token. With **Assignment required = No**, sign-in succeeds, the token carries no
`roles` claim, the platform treats the caller as `VIEWER`, and every mutating call returns
`403 Requires operator role or higher`. Neither is a bug.

### Managing roles from the UI afterwards

Once one admin exists, the rest is self-service: an admin adds, re-roles, and removes platform users from the
**Users** tab of the Admin console — navigate to `/admin` and pick *Users* (there is no `/admin/users` page; that
is the API route behind it, `/api/v1/admin/users`). It writes the same `appRoleAssignedTo` entries on the same
backend service principal, and leaves non-`Platform.*` assignments alone.

*Derived from the backend's `core/rbac.py` and `api/routes/{agents, marketplace, users_admin}.py`.*

## Assignment required (recommended)

**Enterprise applications** → your backend app → **Properties** → set **Assignment required?** to **Yes**.
Entra then refuses tokens for users with no role assignment, so sign-in fails with a named error
(`AADSTS50105`) instead of issuing a valid token that gets a quiet 403 later. The backend sets the same flag
on every *agent* service principal it creates.

> **Do not generalize that to MCP servers.** The MCP path sets the flag to **`false`** deliberately, because Entra
> enforces assignment-required against the *user* in the agent→MCP OBO flow and this design never assigns users to
> MCP servers. Flipping an MCP service principal to `true` blocks the delegated user with `AADSTS50105`, which
> reads exactly like a missing role assignment. The agent→MCP admission gate is instead the per-agent
> `oauth2PermissionGrant`; revoke it and the OBO fails with `AADSTS65001`.

## What the write permissions do at runtime

The write permissions are load-bearing because registering an agent auto-provisions its identity and each grant
mints a fresh secret on the agent's own app registration — and only for governed agents, of which two platforms
qualify: AgentCore-governed (`agent_arn` **and** `auth_type=entra` **and** `platform=aws_bedrock`) or
Databricks-governed (`runtime_handle` **and** `auth_type=entra` **and** `platform=databricks`); anything else — a
metadata-only record with no runtime — provisions nothing and stays at `identity_status="none"`. That is the gate,
not a failure.

Deleting an agent or an MCP server **does** delete its Entra objects. `DELETE /agents/{id}` first tears
down what a *registered* agent owns — its OBO credential provider in the Token Vault, then its Entra app, whose
deletion cascades the service principal and every consent and app-role assignment on it, then its Langfuse project
and secret. `DELETE /mcp-servers/{id}` deletes the MCP's Entra app and SP, then its own Cedar policy engine; the
gateway is never deleted, because the platform did not create it. Both cascades are **best-effort and
per-resource**: a failed leg is logged under `[teardown]` and never blocks the record delete, so it has to be
reclaimed by hand once the record — the only pointer to those resources — is gone. The one exception is an agent a
**repository** owns: all three legs report `skipped`, because that teardown belongs to the repo cascade,
`DELETE /projects/{id}/repos/{repo_id}`.

*Derived from `platform/control_plane/infrastructure/variables.tf` and the backend's `services/{graph_service,
agent_identity_service, mcp_identity_service}.py`.*

## After the first apply: close the redirect-URI loop

**Read this section before you deploy, and do it after.** It is the step that silently breaks sign-in: the SPA's
redirect URI is a CloudFront domain that does not exist until Terraform has created it. Deploy, then read the two
values back from `platform/control_plane/infrastructure`: `terraform output -raw frontend_url` (e.g.
`https://d111111abcdef8.cloudfront.net`) and `terraform output -raw api_endpoint` (e.g.
`https://abc123.execute-api.us-east-1.amazonaws.com/dev`). Then update **both** sides — one is Entra
configuration, the other a build-time constant baked into the JavaScript bundle:

1. **In the SPA app registration** → **Authentication** → the *Single-page application* platform → add
   `<frontend_url>/auth/callback` as a redirect URI.
2. **In `platform/control_plane/frontend/.env`** → set `VITE_ENTRA_SPA_REDIRECT_URI` to that exact same
   string, and `VITE_API_URL` to the `api_endpoint` value.
3. **Rebuild and redeploy the frontend.** `deploy-full.sh` re-reads `.env`, regenerates `.env.production`,
   and redeploys; `deploy-frontend.sh` requires you to copy both values into `.env.production` yourself.

The two strings must match byte for byte, trailing slash included. MSAL compares the redirect URI it sends
against the registered list exactly; a mismatch fails with `AADSTS50011`.

> **This repeats every time the CloudFront domain changes** — after a `terraform destroy` and re-apply, or when
> moving to a custom domain. Nothing detects the drift: sign-in stops working, and the error is in the Entra
> redirect, not in any log the platform writes.

Where a custom domain is configured (`domain_name` + `hosted_zone_id`), register that hostname's callback instead
— and register both if you intend to reach the app either way.

## Verify it works

### Sign in

Open the `frontend_url`. You should be redirected to Microsoft, sign in, and land back on the platform with
your role reflected in the UI. A blank page with a console error means the SPA is missing
`VITE_ENTRA_TENANT_ID` or `VITE_ENTRA_SPA_CLIENT_ID`.

### Confirm your identity and role

The authoritative check is the API's own view of the caller. The signed-in app mirrors its access token into
`localStorage` under the key `auth_token`, so copy it from the browser devtools console with
`localStorage.getItem('auth_token')`, then call `/users/me` with it:

```bash
cd platform/control_plane/infrastructure
API=$(terraform output -raw api_endpoint)
TOKEN='<paste the token>'
curl -s -H "Authorization: Bearer $TOKEN" "$API/api/v1/users/me" | jq
```

`api_endpoint` already includes the API Gateway stage, so `$API/api/v1/...` is the full path. A correct tenant
returns `200` and a body with your `email`, `oid`, `name`, `tenants`, `can_deploy`, and the `role` / `role_level`
your assignment gives you — e.g. `"role": "admin", "role_level": 2`. If `role` says `viewer` when you assigned
yourself `Platform.Admin`, go back to [Assign users to platform roles](#assign-users-to-platform-roles): the
assignment did not land in the token, so check it is on the **backend** app.

### Confirm an unassigned user is refused

Sign in as a user you have **not** assigned to any platform role, in a private window. With **Assignment required
= Yes** ([Assignment required](#assignment-required-recommended)), Entra blocks sign-in with `AADSTS50105`. With
**Assignment required = No**, sign-in succeeds, `/users/me` returns `role: "viewer"`, and any mutation returns 403.
Either outcome proves authorization is live.

### Confirm the API rejects anonymous callers

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/v1/agents"     # 401
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer not-a-token" "$API/api/v1/agents"     # 401
```

Both must be `401`. A `200` means the backend is running with the local dev-auth bypass (`USE_DEV_AUTH` or
`DEBUG`), which skips token validation entirely and must never be set in a deployed environment. A `500` means
the Entra tfvars were never filled in ([the backend table above](#backend--platformcontrol_planeinfrastructuresecretsautotfvars))
— the validator raises before it ever parses the token; fix `secrets.auto.tfvars`, re-apply, and re-run both
commands.

> **Six endpoints are deliberately public** and answer without a bearer token. They are a decision, not a
> misconfiguration, and finding them open is not a finding: `/` (service banner — name, version, liveness),
> `/ping` (liveness probe), `/health` (the load balancer's health check), `/docs` (Swagger UI), `/redoc` (ReDoc
> UI), and `/openapi.json` (the schema behind both, which exposes no secrets and no data). **Everything else
> requires a valid bearer token.** Only `/`, `/ping` and `/health` also answer under the API Gateway stage
> prefix, so `$API/docs` returns 404 — expected, not a broken deploy. And because the ALB that health-checks the
> bare `/health` is internal, the only internet-reachable health URL is `$API/health`, not the bare `/health`.

*Derived from `platform/control_plane/backend/src/main.py` and `infrastructure/modules/ecs/main.tf`.*

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every API call returns `401 Invalid audience (accepted [...])` | The Application ID URI, `entra_audience`, and the URI half of `VITE_ENTRA_SPA_SCOPE` are not identical | Make all three match (step 5). The error message lists what the backend accepted. |
| `401 Invalid issuer (expected https://login.microsoftonline.com/<guid>/v2.0)` | `entra_tenant_id` points at a different tenant than the one that issued the token — or the backend app's token version was left at v1 (step 5 sub-step 1) | Fix `entra_tenant_id` and re-apply, or set `requestedAccessTokenVersion: 2` |
| `500 Internal server error` on every authenticated call, but `/health` is fine | The Entra tfvars were never filled in — they default to `""` and Terraform applies anyway. The real message, `ENTRA_TENANT_ID is not configured`, is in CloudWatch, not in the response | Fill `secrets.auto.tfvars` (step 12), re-apply |
| `401 Invalid token: signing key lookup failed` | The tenant's JWKS endpoint is unreachable, or the token is not an Entra RS256 JWT | Check egress from the ECS task; confirm the token came from MSAL |
| Signed in fine, but everything that saves returns `403 Requires operator role or higher` | The user holds no recognised `Platform.*` role, so they were defaulted to `VIEWER` | Assign a role **on the backend app** ([Assign users](#assign-users-to-platform-roles)) |
| `AADSTS50105` at sign-in | Assignment required is on and the user has no role assignment | Assign a role ([Assign users](#assign-users-to-platform-roles)) |
| `AADSTS50011` — redirect URI mismatch | The registered redirect URI and `VITE_ENTRA_SPA_REDIRECT_URI` differ | Make them byte-identical, then rebuild the frontend ([after the first apply](#after-the-first-apply-close-the-redirect-uri-loop)) |
| Sign-in worked yesterday, fails today after a re-apply | The CloudFront domain changed | Redo [the redirect-URI loop](#after-the-first-apply-close-the-redirect-uri-loop) |
| Registering an agent fails with a Graph error | Missing Graph application permission, admin consent was never granted, or the backend client secret is wrong — sign-in still works, because it is not used for sign-in | Re-check all six permissions **and** the consent (step 9); if both are fine, re-check the secret (step 4) |
| Grants fail but agent registration works | `AppRoleAssignment.ReadWrite.All` missing or unconsented | Step 9 |
| Agent invocation fails after a successful grant | `DelegatedPermissionGrant.ReadWrite.All` missing — the OBO consent was never written | Step 9 |
| The agent answers, but **its MCP tools have silently vanished** — no error, no failed call, just an answer that ignores the tools it used to have | The agent→MCP consent (`oauth2PermissionGrant`) was revoked, or the credential provider is gone — the per-agent secret store AgentCore Identity manages in its Token Vault, platform-managed and never created or rotated by hand — so the token exchange fails inside the runtime. The runtime **degrade-drops** the unreachable MCP and runs with whatever wired successfully — fail-closed by design, and invisible to the caller | Re-issue the agent→MCP grant, which rewrites the consent and re-creates the credential provider. The real cause is only in the **runtime's** logs — look for `MCP … unavailable this invoke — dropping (degrade)`; nothing surfaces in the API response |
| Roles you assigned never appear in the token | They were defined or assigned on the SPA app registration | Move them to the backend app (step 7; [Assign users](#assign-users-to-platform-roles)) |
| A once-working deployment starts failing all Graph calls at the same time | The backend client secret expired | Generate a new one, update `entra_backend_client_secret`, re-apply (step 4) |

**Rotating the client secret.** Generate a new secret in the Entra portal, replace the value in
`secrets.auto.tfvars`, and re-apply. Terraform updates the Secrets Manager secret, and ECS picks up the new
value on the next task replacement. Delete the old secret in Entra only once the new tasks are healthy.

## Related

- [Bootstrapping](../README.md#bootstrapping) — the AWS deploy these values feed
- [`scripts/entra-setup/`](../scripts/entra-setup/README.md) — the optional script that automates steps 2–10
