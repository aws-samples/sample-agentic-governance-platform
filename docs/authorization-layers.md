# Authorization layers

A valid token proves who is calling. It decides nothing else. Three gates then decide what that caller may do.
They always run in the same order, and each refuses in a different shape — which is why two of the three are
routinely mistaken for defects.

| # | Gate | The question it answers | Refusal |
|---|------|-------------------------|---------|
| 1 | Platform role | May this kind of user call this endpoint at all? | `403 Requires <role> role or higher` |
| 2 | Tenant visibility | Does this resource exist *for this caller*? | `404`, in the same words a missing resource gets |
| 3 | Project role | Does this caller hold enough authority over *this* project? | `403 insufficient project role`, or `503` if the grant read fails |

**Read the refusal before anything else.** A 403 naming a platform role is gate 1, and almost always a
role-assignment problem. A 404 on a resource you know exists is gate 2. A bare `403 insufficient project role`
is gate 3, and the grant it wants may be held by a group. A `503 could not verify project ownership` is also
gate 3: a project list whose grant read failed.

The order is fixed. Gate 2 runs before gate 3, so a foreign tenant's resource has already answered 404 before
any project logic runs: **a project-role refusal can only ever concern a project the caller's own tenant
already exposes.** Reversed, every such refusal would confirm another tenant's project exists.

## Gate 1 — the platform role

Three platform roles, in a ladder: **Viewer**, then **Operator**, then **Admin**. Each holds everything below
it. Viewer reads. Operator authors and publishes. Admin destroys, and administers tenants. Every gated
endpoint names a minimum, and the gate compares along the ladder.

Assignments live in the identity provider, not in the platform. Microsoft Entra ID is the provider supported
today, and the gate is not specific to it. The platform reads the role the token carries and matches it
against the three names it expects. The provider stays the system of record: the Admin console's Users tab
writes assignments back to it rather than storing them here. See [Microsoft Entra ID setup](entra-setup.md).

**An unrecognised role means Viewer, not rejection.** A token carrying no role the platform recognises — or no
role at all — resolves to Viewer, never to a refusal. That is least privilege by design: an unmapped role must
never be read as an unmapped privilege. It is also the platform's most confusing failure mode, because a
configuration error wears the costume of a bug. Roles assigned on the wrong application registration never
reach the token, so the user signs in, is silently treated as a Viewer, and every mutation comes back 403.
Nothing logs a warning, because from the gate's point of view nothing went wrong. Confirm the assignment
before debugging a 403 storm; [Microsoft Entra ID setup](entra-setup.md) says what an unassigned user sees.

## Gate 2 — tenant visibility

Gate 1 asked about the caller's kind. Gate 2 asks about the caller's scope, resolved independently of gate 1.
It answers one question about one resource: is this visible to this caller?

- A platform Admin is **global**. Gate 2 never refuses an admin anything.
- A resource marked **shared** is visible to every caller.
- Otherwise the tenant must be one the caller belongs to, so a caller in no tenant sees shared only.
- An **untagged** resource, one that names no tenant, is visible to global callers only. A record that
  predates tenant tagging does not quietly become public; it becomes admin-only.

Membership comes from the groups the token carries, matched against the groups each tenant names — see
[the data model](data-model.md) for where a tenant records them. Precedence turns on whether a groups claim is
*present*, not on whether it is populated. **A present claim is the answer even when empty** — it reads as
"belongs to no group", not "we do not know yet", and no directory lookup follows. Only a token with no groups
claim at all triggers a directory lookup, and only if it identifies the caller; that lookup degrades to no
groups when the directory cannot be reached. An empty membership is normal, never an error.

**Reads filter, writes verify.** The same question is asked three ways, and the difference surprises people.

| Shape | Answer |
|-------|--------|
| A list | Filtered. No error, and no sign that other tenants' rows exist. |
| A detail read, or a write | **404**, in the same words a genuinely missing id gets. |
| Creating *into* a tenant | **403 tenant not permitted**, or **400 unknown tenant** if no such tenant exists. |

Another tenant's project answers `404 Project not found` — the same words as a project that never existed.
This is deliberate. **A 403 there would be an existence oracle:** answering "forbidden" confirms the resource
exists, so anyone could enumerate another tenant's projects and agents by watching for the 403s among the
404s. Creating *into* a foreign tenant is the one 403: nothing exists there yet to hide.

> **If a record has gone missing, check gate 2 before you check the store.** A 404 from a detail route means
> "not visible to you", which covers "another tenant owns it" and "nobody tagged it". Neither is
> distinguishable from deletion by reading the response.

## Gate 3 — the project role

Gate 3 is the least visible of the three, and the one most often reported as a bug: an Operator who passed
gate 1 and gate 2 can still be refused a project mutation, because platform role and project authority are
different questions. A **grant** is one edge — one principal, one project, one level. The principal is a user
or a group, and a group grant reaches everyone in it. Project routes check it, and so do agent routes — but
only for an agent that has a project. An agent registered directly has none, so it stays tenant-gated, and
nothing falls back to gating it against some default project.

| Project role | Grants |
|--------------|--------|
| `viewer` | read the project and its repositories |
| `maintainer` | add a repository, retry, open, approve and merge a pull request |
| `owner` | grant and revoke roles, promote to production, roll back, destroy |

Global admins pass. Everything else is fail-closed: an unknown project, a missing project and a project with
no matching grant all refuse. Lists filter rather than refuse, and a list whose grant read fails answers
`503`: reading an unreadable store as "nothing is governed" is maximally permissive. The match set is the
caller's groups **plus their own id**, so a grant made to a person and one made to a group they belong to are
found by one lookup. The effective level is the **highest** of every matching grant, so a `viewer` grant held
directly and an `owner` grant held through a group make an owner. A weaker grant never caps a stronger one.

Whoever creates a project holds the first `owner` grant on it. A project with no grants at all is not yet
governed, so any tenant-visible caller acts as a `maintainer` there; otherwise projects that predate project
roles would be stranded. **That fallback never reaches `owner`.** Promotion, rollback and destruction all need
a real grant even on an ungoverned project, and promotion and rollback both skip the fallback entirely:
neither shipping to production nor undoing it may run through a compatibility shim. Builds started by CI cross
a different trust boundary, and their token names a repository rather than a person, so no owner check is
possible on it — that path refuses production outright with `403 prod deploys must be initiated from AGP`,
which makes the owner-only promotion real, not advisory.

## How the gates compose

All three gates run on a governed request: promote this repository's production candidate, refused three ways.

| Caller | Refused by | Response |
|--------|-----------|----------|
| Signed in, no platform role assigned, so a Viewer | Gate 1 | `403 Requires operator role or higher` |
| An Operator, in a different tenant than the project | Gate 2 | `404 Project not found`, identical to a deleted project |
| An Operator in the owning tenant, holding `maintainer` | Gate 3 | `403 insufficient project role` |

Only the third caller learns anything: that the project exists, and that they lack authority over it. Both
facts their own tenant already showed them. The second learns nothing at all, which is the point.

## The bypass that defeats all three

A local-development bypass sits ahead of every gate above. Either of two flags turns it on, `USE_DEV_AUTH` or
`DEBUG`, both off by default, and it is checked **before the token is validated** — so while it is on, no
token is required at all. It maps request headers to a platform role, and **with no headers at all the caller
is an Admin.** That one default defeats all three at once: the caller carries no groups, so gate 2 resolves no
membership — but an Admin is global, a global caller sees every tenant, and gate 3 passes on every project. An
anonymous request becomes a global administrator.

Neither flag appears anywhere in the infrastructure, so neither reaches a deployed task, and neither should
ever be set in one. An anonymous request that answers **200** instead of **401** means the bypass is live;
[Microsoft Entra ID setup](entra-setup.md) carries that check.

## Known limitations

> **A failed grant read is cached like any other answer.** A transient store error reads as "no grants", and
> that answer is cached for up to a minute, so a genuine owner can be locked out of their own project for that
> long after the store has recovered. Fail-closed, but a real availability property of gate 3.

> **A revoked grant can still be honoured for up to one cache lifetime.** Invalidation is process-local, so
> the task that served the revoke clears its own cached answer and no other. Accepted, not a defect.

> **Gate 3 has no deny form.** A grant can only grant, and the effective level is the highest of all matching
> grants, so removing authority means revoking every grant that confers it — including group-derived ones,
> where the grant is only as narrow as the group.

> **The address the interface shows need not be a mailbox.** One order now picks a caller's address
> everywhere — `preferred_username`, then `email`, then `upn` — and the write side always used that order, so
> no attribution was ever recorded under a different one and no `created_by` row changes meaning. What moved
> is the read side: `GET /users/me` used to disagree, and now agrees. For a caller whose token also carries a
> differing `email` claim the interface therefore shows the `preferred_username` value, which Entra does not
> guarantee to be a deliverable mailbox. It feeds the display alone and is never persisted.

## Related

- [Token propagation](token-propagation.md) — the journey a token makes before any of these gates run.
- [Microsoft Entra ID setup](entra-setup.md) — creating and assigning the three roles, gate 1's whole input.
- [Cedar tool policies](cedar-tool-policies.md) — the per-tool gate an agent meets beyond these three.
- [Data model](data-model.md) — where tenants, their groups and project grants are recorded.
