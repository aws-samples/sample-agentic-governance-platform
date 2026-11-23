# Frontend absent standing renders nothing

## Status

Accepted 2026-10-23 (Epics 49B+50B)

## Context

A page knows before it paints what the signed-in person may do: the role level from the token, the
project role the backend answered, whether a repository is ungoverned. The surfaces used to disagree
on what to show for a capability the caller lacks: a greyed button with a tooltip, a per-row "you
need Owner" hint, a tab strip offering one tab, a nag repeated once per template card. Each is a
control that can do nothing, and each invites the reader to go asking for privilege. The rule lived
as a comment on every module that applied it, so a new surface copied whichever neighbour it opened.

## Decision

A capability the caller does not hold is absent, not decoratively present: no disabled control, no
tooltip on the missing control, no per-row privilege hint, no tab strip when one tab is visible.
`disabled` is reserved for the request in flight. Where a page would otherwise instruct the reader to
do what they cannot, one sentence replaces the instruction in that one place; it states the page's
standing, never per row and never beside a greyed control. Each module decides its half in pure
code, under `platform/control_plane/frontend/src/components/operations/`, pinned by its test:

- `templatesView.ts`: `EMPTY_CATALOG_READ_ONLY_BODY` replaces the imperative under the gated Upload
  and the cards carry no nag; `templates.test.ts` holds ``keeps `disabled` for the in-flight request
  ONLY``.
- `settingsSections.ts`: `visibleTabs` omits the Admin tab for a viewer and `showTabStrip` renders no
  strip at one tab; `settingsSections.test.ts` holds `a viewer gets NO strip: one tab is a choice
  that does not exist`.
- `repo-tabs/deploymentsTabModel.ts`: `rollbackEligibility` answers `not-owner` before every other
  reason and `ROLLBACK_BLOCKED_NOTE['not-owner']` is `null`; `deploymentsTabModel.test.ts` holds
  `HIDES rollback from a non-OWNER — nothing rendered, no privilege hint`.
- `repoRowModel.ts`: `approval-pending` is a fact about the repository, shown to every role, and its
  label offers no verb, because the Promote control lives on the detail page; `repoRowModel.test.ts`
  asserts `REPO_ACTION_LABEL['approval-pending']` matches neither approve nor promote.
- `repo-tabs/pullRequestsTabModel.ts`: `prRowActions` answers booleans the tab renders conditionally
  and `prReadOnlyNote` is the one sentence for a reader without the Maintainer role;
  `pullRequestsTabModel.test.ts` holds `conditionally RENDERS the write affordances rather than
  disabling them`.
- `projectRoles.ts`: `promoteBlockedReason` answers `not-owner` before every other reason, and
  `canManageRoles` and `mayOfferGrant` answer the booleans their pages render Grant, Revoke and
  Promote behind, conditionally and never `disabled`; `projectRoles.test.ts` holds `does not hand a
  maintainer owner affordances: a maintainer on a project with a group grant once saw Grant and Revoke
  and got a 403 on click`.

## Consequences

- A new write control renders behind a boolean its model answers, never `disabled` by role.
- A backend refusal reaching a reader who was never offered the control is a defect in the gate,
  not a message to add.
- A caller without standing sees the page as a reader does; the route to privilege is a person, not
  a hint on the page.
