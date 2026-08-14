// Pure helpers for the Tenants admin tab (Epic 24; platform-typed in E29). Kept
// framework-free so vitest (src/**/*.test.ts only) can unit-test them;
// TenantsAdmin.tsx / TenantModal.tsx import these. Mirrors the usersAdminForm.ts split.
//
// E29 — a tenant is platform-typed (aws | databricks) and EVERY decision about the
// Databricks branch lives here, not in the modal: which fields gate submit, what a
// workspace URL may look like, whether a stored SP secret already exists, and what the
// capability/binding-mode badges read. The modal binds and renders; it judges nothing.
//
// The AWS branch is a FENCE, not merely "also supported": its draft shape, its validation
// and its payload are byte-identical to pre-E29, and the pre-E29 tests assert that
// unmodified. A Databricks-shaped change that alters an AWS payload is a bug by definition.
import type {
  DatabricksStageConfig,
  PrincipalHit,
  TenantBindingMode,
  TenantCapabilities,
  TenantCreate,
  TenantPlatform,
  TenantStageConfig,
} from '../../../api/client';

// A linked Entra group chip in the modal. `display_name` is best-effort — when a
// draft is seeded from an existing tenant's raw `entra_group_ids` we only have the
// id (see groupsFromIds); a fresh directory pick carries the real name.
export interface TenantGroup {
  id: string;
  display_name: string;
}

// One AWS stage's working state (E25 cross-account CICD config). All string-typed and all
// REQUIRED — a form always binds a string, even an empty one. Declared explicitly rather
// than aliasing `TenantStageConfig`, whose three CICD fields became optional in E29 (they
// are populated by provisioning, so a READ may omit them); a draft never may.
export interface StageDraft {
  account_id: string;
  region: string;
  ecr_repo_uri: string;
  push_role_arn: string;
  deploy_role_arn: string;
}

// One Databricks stage's working state. `sp_client_secret` is WRITE-ONLY: it holds what the
// admin just typed and is never seeded from a record, because the secret does not travel on
// a read — only the POINTER to it does. On edit, an EMPTY secret box means "keep the stored
// one" and typing into it means "replace it".
//
// `sp_client_secret_arn` is carried through the draft, unread by any input, and that is
// load-bearing rather than incidental (fix round 1). The tenant update REPLACES the whole
// stage object server-side (`tenant_service.update`: `changes["stages"] = dict(data.stages)`),
// so a payload that omits the ARN — or sends "" for it — DESTROYS the pointer to the stored
// secret. Reproduced against the landed backend: a no-op edit turned a real ARN into "".
// Keeping only the derived `has_sp_secret` boolean threw away the value it was derived FROM,
// which is what made "blank = keep" a lie. Both are kept now: the boolean drives the UI copy,
// the ARN is re-emitted on the wire. The client never invents or clears an ARN — when a new
// secret IS typed, the old ARN still travels and the backend decides what replaces it.
export interface DatabricksStageDraft {
  workspace_url: string;
  workspace_id: string;
  cloud: string;
  region: string;
  account_id: string;
  sp_client_id: string;
  sp_client_secret: string;
  sp_client_secret_arn: string;
  has_sp_secret: boolean;
}

// The TenantModal's working state — one draft for both create and edit, and for both
// platforms.
//
// Both platforms' stage state is held SIDE BY SIDE rather than as a discriminated union on
// `platform`, for two reasons. The narrow one: a union makes `d.stages.dev` a union-typed
// read, which breaks callers that spread it (including the pre-E29 tests that must keep
// compiling unmodified). The useful one: toggling the picker back and forth never destroys
// what the admin already typed on the other branch. `platform` alone decides which half is
// read — see `canSubmit` and `buildCreatePayload`, the only two functions that care.
//
// Every E29 field is OPTIONAL, which is a contract rather than laziness: a draft carrying no
// `platform` IS an AWS draft and behaves exactly as it did pre-E29. That is the same
// zero-migration rule the backend applies to records written before platform typing
// (`hydrate_tenant_item` defaults an absent platform to "aws"), and it is what keeps the
// pre-E29 tests type-checking against this shape UNMODIFIED — so the AWS fence holds at the
// type level, not merely at runtime. Read the platform via `draftPlatform`, never directly.
export interface TenantDraft {
  name: string;
  line_of_business: string;
  description: string;
  platform?: TenantPlatform;
  stages: { dev: StageDraft; prod: StageDraft };
  databricks?: { dev: DatabricksStageDraft; prod: DatabricksStageDraft };
  // Write-only account-admin credential (Databricks only) — a client id + secret PAIR, not an
  // account login. REQUIRED for a Databricks tenant since E29/T14 (§3B, tenet 4): federation is
  // the model, not a mode, so a tenant with no account-admin credential is not a lesser tenant,
  // it is an incomplete form. Still ALL-OR-NOTHING — half a pair is a typo, not a choice, so
  // `canSubmit` refuses it rather than silently probing with an unusable credential and
  // reporting the capability as absent.
  //
  // Optional at the TYPE level, deliberately, on the same zero-migration rule as every other
  // E29 field: a draft carrying no `platform` is an AWS draft and must keep type-checking
  // unmodified. The requirement is enforced by `canSubmit` on the Databricks branch only.
  account_admin_client_id?: string;
  account_admin_secret?: string;
  // "A credential is already stored for this tenant" — the derived twin of `has_sp_secret`, and
  // derived from the same kind of evidence: the record's Secrets Manager POINTER
  // (`account_admin_secret_arn`), because the credential itself never travels on a read.
  //
  // It exists so the T14 requirement does not become "re-type your secret on every edit". On
  // CREATE it is absent/false and the pair must be typed. On EDIT the pair is never required at
  // all (see `canSubmit`), so this flag's job there is COPY, not gating: it is what lets the modal
  // say "a credential is already stored — leave both boxes blank to keep it" instead of the
  // "federation is not unlocked yet" notice. Blank always means "keep the stored one"
  // (`buildCreatePayload` omits both halves when they are blank). Never seeded true by guesswork —
  // an absent ARN is "none stored".
  has_account_admin_credential?: boolean;
  groups: TenantGroup[];
}

// The draft's platform, defaulting an absent one to AWS (see the note above). Every reader
// goes through this so the default lives in exactly one place.
export function draftPlatform(d: TenantDraft): TenantPlatform {
  return d.platform ?? 'aws';
}

// The draft's Databricks stage state, materialised on demand. A pre-E29 (or freshly toggled)
// draft has none, and a blank pair is the honest reading of that — not an error.
export function draftDatabricksStages(d: TenantDraft): {
  dev: DatabricksStageDraft;
  prod: DatabricksStageDraft;
} {
  return d.databricks ?? { dev: emptyDatabricksStage(), prod: emptyDatabricksStage() };
}

// A blank AWS stage, seeded with the platform-default region.
function emptyStage(): StageDraft {
  return { account_id: '', region: 'us-east-1', ecr_repo_uri: '', push_role_arn: '', deploy_role_arn: '' };
}

// A blank Databricks stage. `cloud` defaults to the backend model's default; `region` is
// left blank deliberately — a Databricks workspace region is not an AWS region and guessing
// one would be a fabricated value on a governance record.
function emptyDatabricksStage(): DatabricksStageDraft {
  return {
    workspace_url: '',
    workspace_id: '0',
    cloud: 'aws',
    region: '',
    account_id: '',
    sp_client_id: '',
    sp_client_secret: '',
    sp_client_secret_arn: '',
    has_sp_secret: false,
  };
}

export const EMPTY_TENANT_DRAFT: TenantDraft = {
  name: '',
  line_of_business: '',
  description: '',
  platform: 'aws',
  stages: { dev: emptyStage(), prod: emptyStage() },
  databricks: { dev: emptyDatabricksStage(), prod: emptyDatabricksStage() },
  account_admin_client_id: '',
  account_admin_secret: '',
  has_account_admin_credential: false,
  groups: [],
};

// The stage axis the admin form works on. E29 keeps the pre-existing two-stage assumption
// for BOTH platforms rather than fixing it here: making this surface iterate an open stage
// map is its own migration (recorded on `TenantInfo` in client.ts), and doing it inside the
// Databricks change would put two unrelated risks in one diff.
export const STAGE_KEYS = ['dev', 'prod'] as const;
export type StageKey = (typeof STAGE_KEYS)[number];

// A 12-digit AWS account id (mirrors the backend's ACCOUNT_ID_RE).
export function isValidAccountId(v: string): boolean {
  return /^\d{12}$/.test(v);
}

// A Databricks workspace origin — the mirror of the backend's WORKSPACE_URL_RE: https only,
// lowercase host, no port, path, query or fragment, and no trailing slash.
//
// The match is additionally verified to span the WHOLE input rather than trusting `$` alone,
// and the reason is worth stating precisely, because the tempting version of this comment is
// wrong. The backend's Python counterpart warns that `$` there matches just before a trailing
// newline, so "https://host\njavascript:…" needs `fullmatch`. JavaScript does NOT share that
// hole: measured, `/…$/.test("https://host\njavascript:alert(1)")` is already false without
// the `m` flag. What the length check buys is the NEXT edit — adding `m` (or `\n` slipping
// into a pattern built from an interpolated string) makes `$` match at any line end, and that
// same input starts passing. This is a security boundary on a governed record, so it is
// pinned so that the flag cannot silently become load-bearing. Every case is EXECUTED over
// hostile inputs in the test; none of it is asserted from reading the pattern.
const WORKSPACE_URL_RE = new RegExp('^https://[a-z0-9][a-z0-9.-]+[a-z0-9]$');

export function isValidWorkspaceUrl(v: string): boolean {
  const m = WORKSPACE_URL_RE.exec(v);
  return m !== null && m[0].length === v.length;
}

// A Databricks workspace id: a digits-string, NOT a number. "0" is a real value (a workspace
// URL that carries no `o=` parameter), so emptiness — not falsiness — is the failure.
// `[0-9]` rather than `\d`, matching the backend's `re.ASCII`: unqualified `\d` in some
// engines admits Unicode digits (e.g. Arabic-Indic), which no Databricks API would accept.
const WORKSPACE_ID_RE = new RegExp('^[0-9]+$');

export function isValidWorkspaceId(v: string): boolean {
  const m = WORKSPACE_ID_RE.exec(v);
  return m !== null && m[0].length === v.length;
}

// One Databricks stage is submittable: valid workspace origin + digits-only workspace id +
// an SP client id, and a secret that is either newly typed or already stored.
//
// `account_id` (the Databricks ACCOUNT UUID) is deliberately NOT gated, and E29/T14 narrows why.
// The old reason — "blocking submit on it would refuse a perfectly valid sp_secret-mode tenant" —
// is void: there is no sp_secret outcome any more (§3B). The reason that survives is the division
// of labour: which binding mode a tenant gets is the backend PROBE's verdict, never the form's,
// and the probe reads account-level facts that no field on this form can predict. So the field is
// labelled for what it unlocks and left optional, and a tenant whose account id is missing lands
// on `invoke_unavailable` with a badge that says exactly what to fix.
//
// NOTE the asymmetry with the account-admin credential, which IS gated in `canSubmit`: that one
// is a value AGP cannot obtain any other way and cannot probe without, whereas an absent account
// id degrades into a truthful badge rather than a silent one.
export function isDatabricksStageComplete(s: DatabricksStageDraft): boolean {
  return (
    isValidWorkspaceUrl(s.workspace_url.trim()) &&
    isValidWorkspaceId(s.workspace_id.trim()) &&
    s.sp_client_id.trim().length > 0 &&
    (s.sp_client_secret.length > 0 || s.has_sp_secret)
  );
}

// The account-admin credential is all-or-nothing: absent entirely, or both halves present.
// SHAPE only — this answers "is what was typed coherent", not "is a credential present". The
// two questions are separate because blank-blank is coherent (an edit keeping the stored one)
// while being absent; presence is `hasAccountAdminCredential`.
export function isAccountAdminCredentialUsable(d: TenantDraft): boolean {
  const id = (d.account_admin_client_id ?? '').trim();
  const secret = d.account_admin_secret ?? '';
  return (id.length === 0 && secret.length === 0) || (id.length > 0 && secret.length > 0);
}

// A credential the backend will actually be able to probe with: one typed here, or one already
// stored on the record (E29/T14). "Or already stored" is what keeps the new requirement from
// meaning "re-type your secret on every edit" — the secret does not travel on a read, so the
// only honest client-side evidence is the record's Secrets Manager pointer.
export function hasAccountAdminCredential(d: TenantDraft): boolean {
  const id = (d.account_admin_client_id ?? '').trim();
  const secret = d.account_admin_secret ?? '';
  return (id.length > 0 && secret.length > 0) || d.has_account_admin_credential === true;
}

// Confirm gate. The shared half (name + LoB + >= 1 linked group) holds for both platforms;
// the stage half branches on `platform`. The AWS branch is unchanged from pre-E29: both
// stage account ids must be 12 digits, and the ARNs/URI stay optional.
//
// The Databricks branch additionally REQUIRES an account-admin credential — but on CREATE ONLY
// (E29/T14). This inverts what stood here before, and the reason is a product decision rather
// than a validation tweak, so it is worth stating: the old comment declined to gate on it because
// doing so "would refuse a perfectly valid sp_secret-mode tenant". There is no such outcome any
// more — the backend probe emits `federation` or `invoke_unavailable`, never `sp_secret` (§3B), so
// the credential is no longer the thing that *upgrades* a tenant, it is the thing without which
// the tenant cannot invoke at all. Refusing CREATE is the honest place to say that, once, instead
// of accepting a new tenant and badging it broken afterwards.
//
// EDIT never requires it, and that is the same §3B reading rather than a softening: §3B makes
// `invoke_unavailable` a SUPPORTED, operable state ("discover, register, catalogue, observe —
// invoke refused"), and the backend's `TenantUpdate` documents "unlock federation later" as the
// real sequence, since Tier-3 account-admin access is often obtained after the workspace one. A
// tenant in a supported state must stay administrable: blocking a rename or an Entra-group add
// would be strictly stronger than invoke-refused, and would block precisely the edit (add the
// group that gets a team the inventory §3B grants them) that the state exists to allow. The badge
// and the modal notice already TELL the operator; refusing Save would add friction, not honesty.
// Supplying or rotating the pair on an edit stays possible — it is just not a gate.
//
// Two halves, kept apart on purpose: coherence (never half a pair, in either direction — enforced
// on BOTH paths, because half a pair is a typo) and presence (typed here, or already stored — see
// `hasAccountAdminCredential`), which is the create-only half.
export function canSubmit(d: TenantDraft, isEdit = false): boolean {
  const sharedOk =
    d.name.trim().length > 0 && d.line_of_business.trim().length > 0 && d.groups.length > 0;
  if (!sharedOk) return false;
  if (draftPlatform(d) === 'databricks') {
    const db = draftDatabricksStages(d);
    return (
      isAccountAdminCredentialUsable(d) &&
      (isEdit || hasAccountAdminCredential(d)) &&
      STAGE_KEYS.every((k) => isDatabricksStageComplete(db[k]))
    );
  }
  return STAGE_KEYS.every((k) => isValidAccountId(d.stages[k].account_id.trim()));
}

// Trim every field of one stage sub-draft.
function trimStage(s: StageDraft): TenantStageConfig {
  return {
    account_id: s.account_id.trim(),
    region: s.region.trim(),
    ecr_repo_uri: s.ecr_repo_uri.trim(),
    push_role_arn: s.push_role_arn.trim(),
    deploy_role_arn: s.deploy_role_arn.trim(),
  };
}

// Trim one Databricks stage sub-draft into its wire shape.
//
// Three rules, each of which had to be exactly this way:
//
//   • `sp_client_secret` is included ONLY when non-empty, so an untouched secret box on an
//     edit does not overwrite the stored secret with "".
//   • The secret is NOT trimmed. Leading/trailing whitespace can be significant in a
//     credential, and silently altering one produces an auth failure nobody can explain.
//   • `sp_client_secret_arn` is ECHOED BACK as read, never hardcoded. It is the backend's
//     value to mint, but the update path replaces the whole stage object, so failing to
//     return it deletes it (see `DatabricksStageDraft`). On create the draft's ARN is "",
//     which is exactly right; on edit it is the stored pointer. When a new secret IS typed
//     the OLD ARN still travels — the backend decides what supersedes it, because a client
//     that guessed an ARN would be asserting a fact it cannot know.
function trimDatabricksStage(
  s: DatabricksStageDraft,
): DatabricksStageConfig & { sp_client_secret?: string } {
  const out: DatabricksStageConfig & { sp_client_secret?: string } = {
    workspace_url: s.workspace_url.trim(),
    workspace_id: s.workspace_id.trim(),
    cloud: s.cloud.trim(),
    region: s.region.trim(),
    account_id: s.account_id.trim(),
    sp_client_id: s.sp_client_id.trim(),
    sp_client_secret_arn: s.sp_client_secret_arn.trim(),
  };
  if (s.sp_client_secret.length > 0) out.sp_client_secret = s.sp_client_secret;
  return out;
}

// POST body — trims text fields, nests the per-stage config, and flattens the
// group chips to entra_group_ids.
//
// The AWS branch emits EXACTLY the pre-E29 object, with no `platform` key: the backend
// defaults an absent platform to "aws", so an AWS tenant's wire shape is untouched by this
// epic (asserted by the unmodified pre-E29 payload test).
export function buildCreatePayload(d: TenantDraft): TenantCreate {
  const base = {
    name: d.name.trim(),
    line_of_business: d.line_of_business.trim(),
    description: d.description.trim(),
    entra_group_ids: d.groups.map((g) => g.id),
  };
  if (draftPlatform(d) === 'databricks') {
    const db = draftDatabricksStages(d);
    const payload: TenantCreate = {
      ...base,
      platform: 'databricks',
      stages: { dev: trimDatabricksStage(db.dev), prod: trimDatabricksStage(db.prod) },
    };
    // Only sent when usable (both halves present) — `canSubmit` refuses a half-pair, so
    // this never silently drops something the admin meant to supply.
    const adminId = (d.account_admin_client_id ?? '').trim();
    const adminSecret = d.account_admin_secret ?? '';
    if (adminId.length > 0 && adminSecret.length > 0) {
      payload.account_admin_client_id = adminId;
      payload.account_admin_secret = adminSecret;
    }
    return payload;
  }
  return {
    ...base,
    stages: { dev: trimStage(d.stages.dev), prod: trimStage(d.stages.prod) },
  };
}

// PUT body — the backend PUT is a partial update, but the modal always edits the
// full draft, so we send every field (same shape as create) MINUS `platform`: a tenant's
// platform is immutable after create. The backend's `TenantUpdate` model carries no such
// field and drops it, so stripping it here is the second lock on the same rule, not the
// only one.
export function buildUpdatePayload(d: TenantDraft): TenantCreate {
  const payload = buildCreatePayload(d);
  delete payload.platform;
  return payload;
}

// Append a directory hit to the chip list, deduping by id. Non-mutating.
export function addGroup(groups: TenantGroup[], hit: PrincipalHit): TenantGroup[] {
  if (groups.some((g) => g.id === hit.id)) return groups;
  return [...groups, { id: hit.id, display_name: hit.display_name }];
}

// Remove a chip by id. Non-mutating.
export function removeGroup(groups: TenantGroup[], id: string): TenantGroup[] {
  return groups.filter((g) => g.id !== id);
}

// Seed chips from an existing tenant's raw entra_group_ids — we don't have the
// group display names without a Graph round-trip, so the id doubles as the label.
export function groupsFromIds(ids: string[]): TenantGroup[] {
  return ids.map((id) => ({ id, display_name: id }));
}

// Directory results → group-only, not-already-linked hits. principalsApi.search
// has NO kind param (returns users+groups+agents mixed), so the groups-only view
// is a client-side filter (research §4).
export function filterGroupHits(hits: PrincipalHit[], selected: TenantGroup[]): PrincipalHit[] {
  const selectedIds = new Set(selected.map((g) => g.id));
  return hits.filter((h) => h.type === 'group' && !selectedIds.has(h.id));
}

// ---------------------------------------------------------------------------
// E29 — platform + capability display. Pure, so the badges are unit-tested rather
// than eyeballed.
// ---------------------------------------------------------------------------

// The platform picker's options, and the label a platform reads as. Databricks is spelled
// with its own capitalisation; AWS is an initialism.
export const PLATFORM_OPTIONS: { value: TenantPlatform; label: string; hint: string }[] = [
  { value: 'aws', label: 'AWS', hint: 'Agents run on Bedrock AgentCore in the tenant’s AWS accounts.' },
  {
    value: 'databricks',
    label: 'Databricks',
    hint: 'Agents run as Databricks Apps or serving endpoints in the tenant’s workspaces.',
  },
];

export function platformLabel(p: TenantPlatform): string {
  return PLATFORM_OPTIONS.find((o) => o.value === p)?.label ?? p;
}

// The binding-mode badge text, or null when there is nothing to badge.
//
// Copy is VERBATIM per contract C-6 — `Federation` / `SP secret`, title-case, no expansion —
// joined by `Invoke unavailable — federation required` (E29/T14; the onboarding doc quotes that
// string word for word, so it is contract too). Do not "clarify" any of them: these are the same
// strings the agent-detail badge renders, and two spellings of one mode is exactly the drift the
// contract pins down.
//
// null for an AWS tenant (whose mode is "") and for any value we do not recognise: a badge
// that renders a raw wire value teaches the reader a vocabulary the product does not have.
//
// A RECORD, not an if-chain, and that is the load-bearing choice: `BINDING_MODE_HINT` and
// `platformLabels`' `BINDING_MODE_TINT` are keyed by this same union, so a fourth mode is a
// compile error in all three places at once. The if-chain it replaced was compile-linked to
// neither map — adding a branch there while forgetting the hint made `bindingModeBadge` return
// null, i.e. a silently missing badge, which is exactly what §3B forbids for a legacy sp_secret
// record.
export type BadgedBindingMode = 'federation' | 'invoke_unavailable' | 'sp_secret';

export const BINDING_MODE_LABEL: Record<BadgedBindingMode, string> = {
  federation: 'Federation',
  invoke_unavailable: 'Invoke unavailable — federation required',
  sp_secret: 'SP secret',
};

export function bindingModeLabel(mode: TenantBindingMode | string | undefined): string | null {
  return BINDING_MODE_LABEL[mode as BadgedBindingMode] ?? null;
}

// What each binding mode MEANS for the caller, for the badge's title. Every mode explains itself
// — the badge is a claim about how a call is attributed, and a claim with no consequence attached
// is decoration.
//
// `invoke_unavailable` (E29/T14) is the mode a tenant lands in when federation could not be
// established, and its line is written to be ACTIONABLE rather than a verdict: it names the state
// (inventory works, invoke does not) and then the two things federation needs, so an operator
// reading the badge knows what to go and do. Per §3B this is honesty, not a downgrade — the
// tenant keeps real discover/register/catalogue/observe value.
//
// `sp_secret` is DORMANT, and its line is kept verbatim on purpose: no connect flow can produce
// that mode any more, but records written before T14 still carry it, and those are exactly the
// records whose cost must not go quiet. Same amber, same sentence.
export const BINDING_MODE_HINT: Record<BadgedBindingMode, string> = {
  federation:
    'Databricks and Unity Catalog see the real caller — per-caller audit and tools are possible.',
  invoke_unavailable:
    'Invoke is refused for this tenant — discovery, registration and observability still work. Federation needs an account-admin credential (client id + secret) on the tenant and user sync from Entra to Databricks.',
  sp_secret:
    'Calls are attributed to the platform’s service principal in Databricks audit logs — not the individual caller.',
};

// One capability badge: what was probed, what it is called, and what came back.
//
// `on` is a THREE-state value collapsed carefully: `undefined` means the probe never ran (the
// key is absent), which must not read as a failure. Renderers show absent probes as unknown,
// not as "no" — claiming a capability is missing when nobody looked is the same class of
// dishonesty as the environment strip's "no environments configured".
export interface CapabilityBadge {
  key: 'can_discover' | 'account_admin' | 'user_sync';
  label: string;
  on: boolean | undefined;
}

const CAPABILITY_LABELS: { key: CapabilityBadge['key']; label: string }[] = [
  { key: 'can_discover', label: 'Discovery' },
  { key: 'account_admin', label: 'Account admin' },
  { key: 'user_sync', label: 'User sync' },
];

// The capability badges for a tenant, in a FIXED order (discovery, account admin, user sync)
// so a tenant's badge row never reorders between renders or between the list and the modal.
// Empty for a non-Databricks tenant: AWS tenants are never probed, so there is nothing to say.
export function capabilityBadges(
  platform: TenantPlatform,
  capabilities: TenantCapabilities | undefined,
): CapabilityBadge[] {
  if (platform !== 'databricks') return [];
  return CAPABILITY_LABELS.map(({ key, label }) => ({ key, label, on: capabilities?.[key] }));
}

// Whether a Databricks tenant has been probed at all — i.e. whether any capability key is
// present. Distinguishes "probe said no" from "no probe has run", which the badges render
// differently.
export function hasBeenProbed(capabilities: TenantCapabilities | undefined): boolean {
  if (!capabilities) return false;
  return CAPABILITY_LABELS.some(({ key }) => capabilities[key] !== undefined);
}

// A record's stage config → a Databricks stage draft. The secret is NEVER seeded (it does not
// travel on a read); a non-empty Secrets Manager ARN is what tells us one is stored.
export function databricksStageDraftFromConfig(
  c: DatabricksStageConfig | undefined,
): DatabricksStageDraft {
  if (!c) return emptyDatabricksStage();
  return {
    workspace_url: c.workspace_url ?? '',
    workspace_id: c.workspace_id ?? '0',
    cloud: c.cloud || 'aws',
    region: c.region ?? '',
    account_id: c.account_id ?? '',
    sp_client_id: c.sp_client_id ?? '',
    // Never seeded — the secret does not travel on a read.
    sp_client_secret: '',
    // The stored POINTER, carried through so the update re-emits it instead of clearing it.
    sp_client_secret_arn: c.sp_client_secret_arn ?? '',
    has_sp_secret: (c.sp_client_secret_arn ?? '').length > 0,
  };
}

// An existing tenant record → the modal's working draft (edit mode).
//
// Lives here rather than in the modal because it makes two DECISIONS: how a stage the record
// does not carry is seeded (blank, never a guessed default), and which half of the draft the
// record's stages populate. Reads a stage map by the two conventional keys, tolerating an
// absent one — see STAGE_KEYS on why the open-stage-map migration is not in this task; the
// difference from pre-E29 is that a missing stage now yields a blank draft instead of
// throwing on `undefined.account_id`.
export function draftFromTenant(t: {
  name: string;
  line_of_business: string;
  description: string;
  platform?: TenantPlatform;
  stages: Record<string, TenantStageConfig | DatabricksStageConfig>;
  entra_group_ids: string[];
  // The POINTER to a stored account-admin credential, if the record carries one. Optional
  // because a pre-E29 record does not, and because a build older than the projection would not
  // send it — absent means "none stored", never an assumed one.
  account_admin_secret_arn?: string;
}): TenantDraft {
  const platform = t.platform ?? 'aws';
  const awsStage = (key: StageKey): StageDraft => {
    const c = t.stages?.[key];
    // Absent stage → a well-formed blank draft (E36/T1): `stages` is an OPEN map, so a
    // single-stage tenant lacks one of the two edited keys, and an undefined field here
    // would throw at render time on the first `.trim()`.
    if (!c || isDatabricksStage(c)) return emptyStage();
    return {
      account_id: c.account_id ?? '',
      region: c.region ?? '',
      ecr_repo_uri: c.ecr_repo_uri ?? '',
      push_role_arn: c.push_role_arn ?? '',
      deploy_role_arn: c.deploy_role_arn ?? '',
    };
  };
  const dbStage = (key: StageKey): DatabricksStageDraft => {
    const c = t.stages?.[key];
    return databricksStageDraftFromConfig(isDatabricksStage(c) ? c : undefined);
  };
  return {
    name: t.name,
    line_of_business: t.line_of_business,
    description: t.description,
    platform,
    stages: { dev: awsStage('dev'), prod: awsStage('prod') },
    databricks: { dev: dbStage('dev'), prod: dbStage('prod') },
    // Never seeded: a write-only credential has no read counterpart. Left blank on an edit,
    // it is simply not sent, and the stored one stays as it is.
    account_admin_client_id: '',
    account_admin_secret: '',
    // What CAN be seeded is the fact that one exists — from the pointer, exactly as
    // `has_sp_secret` is. This is what lets the T14 requirement be satisfied by an edit that
    // types nothing.
    has_account_admin_credential: (t.account_admin_secret_arn ?? '').length > 0,
    groups: groupsFromIds(t.entra_group_ids),
  };
}

// True when a stage config carries the Databricks shape. A runtime check, not a cast: the
// stage union is discriminated by the TENANT's platform, so a reader that has a stage value
// in hand needs a way to narrow it. `workspace_url` is the field only the Databricks shape
// has and never legitimately omits.
//
// TAKES `unknown`, AND GUARDS TRUTHINESS — both widened by E29/T11 fix round 1, after a review
// EXECUTED `{dev: null}` through it and got `TypeError: Cannot read properties of null`. Two
// mistakes met:
//
//   • The old signature accepted `… | undefined` and the body tested `!== undefined`, so `null`
//     slipped past the guard and the property read crashed. `null` is not hypothetical on this
//     wire: `stages` arrives as JSON off `/users/me`, where a stage key with a null value is a
//     perfectly ordinary shape — and one crash here blanked an entire page for EVERY platform,
//     not only the one being narrowed.
//   • The old signature is also what forced callers holding a genuinely-unvalidated value to
//     write `as never` to call this at all, and that cast is what stopped `tsc` from reporting
//     the null in the first place. A type predicate's whole job is to be the safe entry point
//     from unvalidated data, so accepting `unknown` is the honest parameter type: it removes the
//     casts rather than tolerating them, and every caller keeps its narrowed result.
//
// A non-object (string, number, boolean) is answered by the `typeof` on a property read, which is
// safe on any of them — only `null`/`undefined` throw, and both are excluded first.
export function isDatabricksStage(c: unknown): c is DatabricksStageConfig {
  return !!c && typeof (c as DatabricksStageConfig).workspace_url === 'string';
}
