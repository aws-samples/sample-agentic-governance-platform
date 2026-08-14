#!/usr/bin/env bash
#
# databricks-onboard.sh — instrument a Databricks account for AGP governance.
#
# Creates the service principal AGP authenticates to your workspace as, mints its OAuth
# secret, grants it the account's `account_admin` role, assigns it to the workspace, and prints
# the exact values to paste into AGP's "New tenant → Databricks" form. The printed values
# supply the ACCOUNT-ADMIN half of a FEDERATION tenant — the account-admin role is what lets AGP
# amend the account federation policy so a real Entra user's token can be exchanged at the
# workspace. The other half is user sync (Entra SSO/AIM, i.e. an Entra-issuer account federation
# policy), which this script cannot create — see the guide's Prerequisites. Without both halves
# AGP refuses invoke rather than falling back to a shared identity (design §3B).
# Idempotent on the SP (reuses one with the same name) and on the role grant;
# always mints a fresh secret (a secret is shown once and cannot be re-read).
#
# Prerequisites:
#   - the `databricks` CLI (https://docs.databricks.com/dev-tools/cli)
#   - an ACCOUNT-level login as an account admin:
#       databricks auth login --host https://accounts.cloud.databricks.com --account-id <ID>
#   - the workspace id you want AGP to govern (Account console → Workspaces)
#
# Usage:
#   ./databricks-onboard.sh --account-profile <p> --workspace-id <id> [--account-id <id>]
#                           [--name agp-control-plane] [--scope admin|discover]
#
#   --scope admin     (default) SP becomes a workspace admin — sees every app/endpoint for
#                     discovery AND can assert app ACLs (T13). The straightforward choice for
#                     a governance control plane.
#   --scope discover  SP gets USER on the workspace only. Discovery sees just what the SP is
#                     granted, and an operator must grant the SP CAN_MANAGE on each governed
#                     app before provisioning. This narrows the SP's WORKSPACE rights only — it
#                     still holds the account's account_admin role (federation requires it), so
#                     it is least privilege on the data plane, not on the account.
#
set -euo pipefail

# ---- pretty ---------------------------------------------------------------- #
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'; else B= G= Y= R= D= N=; fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '%s•%s %s\n' "$B" "$N" "$*"; }
die()  { printf '%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# ---- args ------------------------------------------------------------------ #
ACCOUNT_PROFILE=""; WORKSPACE_ID=""; ACCOUNT_ID=""; SP_NAME="agp-control-plane"; SCOPE="admin"
while [ $# -gt 0 ]; do
    case "$1" in
        --account-profile) ACCOUNT_PROFILE="$2"; shift 2 ;;
        --workspace-id)    WORKSPACE_ID="$2";    shift 2 ;;
        --account-id)      ACCOUNT_ID="$2";      shift 2 ;;
        --name)            SP_NAME="$2";         shift 2 ;;
        --scope)           SCOPE="$2";           shift 2 ;;
        -h|--help)         grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$ACCOUNT_PROFILE" ] || die "--account-profile is required (an account-admin CLI profile)"
[ -n "$WORKSPACE_ID" ]    || die "--workspace-id is required (Account console → Workspaces)"
[ "$SCOPE" = "admin" ] || [ "$SCOPE" = "discover" ] || die "--scope must be 'admin' or 'discover'"
command -v databricks >/dev/null 2>&1 || die "the 'databricks' CLI is not on PATH"
command -v python3    >/dev/null 2>&1 || die "python3 is required (for JSON parsing)"

DBX() { databricks "$@" --profile "$ACCOUNT_PROFILE" -o json; }
jget() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))"; }

# ---- preflight: prove account-admin access -------------------------------- #
info "Checking account access via profile '${ACCOUNT_PROFILE}'…"
WS_JSON="$(DBX account workspaces list 2>/dev/null || true)"
# Pass JSON via env (NOT stdin): a `python3 - <<HEREDOC` would make the heredoc and the piped
# data fight over stdin, and the data loses.
WS_SUMMARY="$(_WS="$WS_JSON" _ID="$WORKSPACE_ID" python3 -c "
import os, json, sys
try:
    rows = json.loads(os.environ['_WS'])
except Exception:
    sys.exit(1)
m = [w for w in rows if str(w.get('workspace_id')) == os.environ['_ID']]
if not m:
    sys.exit(1)
w = m[0]
print('%s|https://%s.cloud.databricks.com|%s' % (w.get('workspace_name'), w.get('deployment_name'), w.get('aws_region')))
" || true)"
[ -n "$WS_SUMMARY" ] || die "workspace id not found in this account (or the profile is not an account-admin login). Run: databricks auth login --host https://accounts.cloud.databricks.com --account-id <ID>"
WS_HOST="$(printf '%s' "$WS_SUMMARY" | cut -d'|' -f2)"
WS_REGION="$(printf '%s' "$WS_SUMMARY" | cut -d'|' -f3)"
say "  ${D}workspace:${N} $(printf '%s' "$WS_SUMMARY" | cut -d'|' -f1)  (${WS_HOST}, ${WS_REGION})"
ok "Account admin confirmed; target workspace resolved."

# ---- account id (for the tenant form) ------------------------------------- #
if [ -z "$ACCOUNT_ID" ]; then
    ACCOUNT_ID="$(databricks auth describe --profile "$ACCOUNT_PROFILE" -o json 2>/dev/null | jget account_id || true)"
fi
[ -n "$ACCOUNT_ID" ] || info "Could not auto-detect account id; pass --account-id to include it in the summary."

# ---- 1. service principal (idempotent by display name) -------------------- #
info "Ensuring service principal '${SP_NAME}'…"
SP_LIST="$(DBX account service-principals list 2>/dev/null || echo '{}')"
SP_ID="$(_SP="$SP_LIST" _NAME="$SP_NAME" python3 -c "
import os,json
d=json.loads(os.environ['_SP'] or '{}')
rows=d.get('Resources') or d.get('service_principals') or (d if isinstance(d,list) else [])
for sp in rows:
    if sp.get('displayName')==os.environ['_NAME'] or sp.get('display_name')==os.environ['_NAME']:
        print(sp.get('id') or sp.get('sp_id') or ''); break
" 2>/dev/null || true)"

if [ -n "$SP_ID" ]; then
    APP_ID="$(DBX account service-principals get "$SP_ID" 2>/dev/null | jget applicationId)"
    [ -n "$APP_ID" ] || APP_ID="$(DBX account service-principals get "$SP_ID" 2>/dev/null | jget application_id)"
    ok "Reusing existing service principal (id ${SP_ID})."
else
    CREATE="$(DBX account service-principals create --display-name "$SP_NAME" --active)"
    SP_ID="$(printf '%s' "$CREATE" | jget id)"
    APP_ID="$(printf '%s' "$CREATE" | jget applicationId)"
    [ -n "$SP_ID" ] && [ -n "$APP_ID" ] || die "service principal create returned no id/application_id"
    ok "Created service principal (id ${SP_ID}, client id ${APP_ID})."
fi

# ---- 2. OAuth secret (always fresh) --------------------------------------- #
info "Minting an OAuth secret…"
SECRET_JSON="$(DBX account service-principal-secrets create "$SP_ID")"
SP_SECRET="$(printf '%s' "$SECRET_JSON" | jget secret)"
[ -n "$SP_SECRET" ] || die "secret create returned no secret value"
ok "Secret minted (shown once, below)."

# ---- 3. account_admin role (what makes the tenant federation-capable) ------ #
# SCIM PatchOp on the ACCOUNT service principal; "add" on an already-held role is a no-op, so
# this stays re-runnable. Verified by re-reading the SP rather than trusting the exit code.
info "Granting the SP the account's 'account_admin' role…"
ADMIN_PATCH='{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"add","path":"roles","value":[{"value":"account_admin"}]}]}'
DBX account service-principals patch "$SP_ID" --json "$ADMIN_PATCH" >/dev/null 2>&1 || true
SP_AFTER="$(DBX account service-principals get "$SP_ID" 2>/dev/null || echo '{}')"
ACCOUNT_ADMIN_OK="$(_SP="$SP_AFTER" python3 -c "
import os,json
try:
    d=json.loads(os.environ['_SP'] or '{}')
except Exception:
    d={}
roles=d.get('roles') or []
print('yes' if any((r or {}).get('value')=='account_admin' for r in roles) else '')
" 2>/dev/null || true)"
if [ -n "$ACCOUNT_ADMIN_OK" ]; then
    ok "Account admin role granted (AGP can amend the account federation policy)."
else
    ACCOUNT_ADMIN_OK=""
    printf '%s!%s %s\n' "$Y" "$N" "Could not confirm the 'account_admin' role on this SP."
fi

# ---- 4. workspace assignment ---------------------------------------------- #
if [ "$SCOPE" = "admin" ]; then PERMS='["ADMIN"]'; ROLE="workspace admin"; else PERMS='["USER"]'; ROLE="workspace user"; fi
info "Assigning the SP to the workspace as ${ROLE}…"
DBX account workspace-assignment update "$WORKSPACE_ID" "$SP_ID" --json "{\"permissions\": ${PERMS}}" >/dev/null
ok "Workspace assignment set (${ROLE})."

# ---- summary --------------------------------------------------------------- #
say ""
say "${G}${B}Done — Databricks is instrumented for AGP.${N}"
say ""
say "${B}Paste these into AGP → Admin → Tenants → New tenant → platform = Databricks:${N}"
say "  ${D}Workspace URL${N}        ${WS_HOST}"
say "  ${D}Workspace ID${N}         ${WORKSPACE_ID}"
say "  ${D}Cloud / Region${N}       AWS / ${WS_REGION}"
[ -n "$ACCOUNT_ID" ] && say "  ${D}Account ID${N}           ${ACCOUNT_ID}"
say "  ${D}SP client id${N}         ${APP_ID}"
say "  ${Y}SP client secret${N}     ${SP_SECRET}"
say ""
if [ -n "$ACCOUNT_ADMIN_OK" ]; then ADMIN_NOTE=""; else ADMIN_NOTE=" ${Y}(not usable yet — see the role-grant warning below)${N}"; fi
say "${B}…and the same pair into the account-admin credential fields:${N}${ADMIN_NOTE}"
say "  ${D}Account-admin client id${N}      ${APP_ID}"
say "  ${Y}Account-admin client secret${N}  ${SP_SECRET}"
say ""
say "${Y}The secret is shown only now — copy it before closing this terminal.${N}"
if [ "$SCOPE" = "discover" ]; then
    say ""
    say "${D}Scope=discover: grant this SP CAN_MANAGE on each app you register in AGP before${N}"
    say "${D}provisioning, and CAN_VIEW on serving endpoints you want discovered.${N}"
fi
say ""
if [ -n "$ACCOUNT_ADMIN_OK" ]; then
    say "${D}These values complete the ACCOUNT-ADMIN half of a FEDERATION tenant: the account-admin${N}"
    say "${D}role lets AGP amend the account federation policy, so agents act as the real Entra${N}"
    say "${D}caller (design §3B). The other half is user sync — an Entra-issuer account federation${N}"
    say "${D}policy from Entra SSO/AIM, which this script cannot create; AGP probes it as${N}"
    say "${D}'user_sync' and still refuses invoke without it (see the guide's Prerequisites).${N}"
else
    say "${Y}Federation is NOT complete: the 'account_admin' role is not confirmed on this SP.${N}"
    say "${D}Grant it in the account console (User management → Service principals → ${SP_NAME} →${N}"
    say "${D}Roles → Account admin) and re-run this script. Until then AGP will connect the tenant${N}"
    say "${D}for discovery/registration/observability but REFUSE invoke.${N}"
fi
