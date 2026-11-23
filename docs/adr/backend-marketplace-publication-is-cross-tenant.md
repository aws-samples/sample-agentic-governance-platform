# A published marketplace card is marketplace-wide; publish requests are tenant-scoped

## Status

Accepted 2026-10-24, Epics 49A/50A.

## Context

The marketplace catalogs (`list_agent_products`, `list_mcp_products` in
`platform/control_plane/backend/src/services/marketplace_service.py`) read every registry record
carrying an approved, published `marketplace` block. Every other registry read in the platform is
filtered by the caller's `TenantContext` through `tenant_resolver.visible()`. The subscribe gate
(`_resolve_product_name` through `_product_is_offered`), the grant-time re-gates and the frontend's
tenant badge all depend on the same answer to "who may see a published card".

## Decision

- Publishing is the act of making a product discoverable across tenants: an admin approves a
  publisher's declared datasheet, and from then on the card is visible to every signed-in viewer,
  badged with the owning tenant's name. Neither catalog filters by tenant, and `visible()` is not
  consulted on the subscribe gate either.
- The one tenant-scoped surface is the publish request itself: creating one and reading one's own
  product's record are filtered by `visible(ctx, tenant_id)` without `shared=`, so a foreign
  tenant's product, shared or not, looks absent, and an operator of one tenant cannot file a request
  against another tenant's product.
- Rejected: tenant-scoped catalogs with an explicit cross-tenant flag per card, which would
  duplicate the publish approval with a second approver (the `shared` flag is a different feature,
  read and grant-target rights only).

## Consequences

- A tenant admin's approval is meaningful for every other tenant; scoping the catalog would make
  it a no-op.
- The MCP catalog is marketplace-wide in code as well as in policy: neither its route nor
  `MarketplaceService.list_mcp_products` takes a tenant scope. The two publish-request methods take
  `ctx` as a required keyword, so a call that omits it is refused by pyright and raises at runtime.
- The product invariant "a foreign tenant's marketplace product looks absent" (`CLAUDE.md`) is
  about publish requests, not cards. `platform/control_plane/backend/tests/test_marketplace_service.py`
  pins both halves: foreign published cards appear badged; foreign publish records are `not_found`
  or `None`.
