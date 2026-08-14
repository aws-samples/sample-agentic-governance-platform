#!/usr/bin/env bash
# Authenticate the local container engine (docker OR finch) to our ECR registry. This is the
# ONLY place in the module that writes registry credentials.
#
# Usage: ecr-login.sh <ecr_registry> <region>
#
# Called from two places, on purpose:
#   1. null_resource.ecr_login (ecr.tf) — ONCE per apply, before any mirror starts.
#   2. mirror-image.sh — defensively, because a resumed apply can re-run a single mirror
#      without re-running the login resource (unchanged triggers), and an ECR authorization
#      token only lives 12h. The freshness check below makes that call a no-op in the normal
#      from-zero path, so exactly one login happens.
#
# Why the ceremony: `for_each` runs the three push_images copies in PARALLEL, and on macOS
# `docker login` stores the credential in the login keychain. Concurrent writes to the same
# registry entry race, and the losers die with
#   error saving credentials - err: exit status 1, out: `The specified item already exists
#   in the keychain. (-25299)`
# which failed a real from-zero `terraform apply` (2 of 3 mirrors succeeded, the apply
# exited 1). Retrying that login just races again, so instead the concurrency is removed:
#   * a mkdir mutex — at most one login per engine+registry runs at a time on this host;
#   * a freshness stamp — a sibling arriving after a successful login reads one file and
#     skips, so it never touches the credential store and has nothing to race with.
# Both guards are plain POSIX filesystem operations: no keychain, no docker internals, so the
# finch/nerdctl path behaves identically.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $(basename "$0") <ecr_registry> <region>" >&2
  exit 2
fi
ECR_REGISTRY="$1"; REGION="$2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# container-engine.sh tests $CONTAINER_ENGINE unquoted, which trips `set -u` when it is
# unset — default it to empty first (empty = "no explicit override", so auto-detect runs).
# When mirror-image.sh calls us it has already exported CONTAINER_ENGINE, so both scripts
# provably talk to the same engine (and its credential store).
CONTAINER_ENGINE="${CONTAINER_ENGINE:-}"
# shellcheck source=../../scripts/container-engine.sh
source "$SCRIPT_DIR/../../scripts/container-engine.sh"
resolve_container_engine || exit 1
CE="$CONTAINER_ENGINE"

STATE_DIR="${TMPDIR:-/tmp}"
STATE_DIR="${STATE_DIR%/}/agp-ecr-login"
KEY="$CE-${ECR_REGISTRY//[^A-Za-z0-9._-]/_}"
LOCK_DIR="$STATE_DIR/$KEY.lock"
STAMP_FILE="$STATE_DIR/$KEY.stamp"
# Well under the 12h life of an ECR authorization token, so a stamp never outlives the
# credential it vouches for.
LOGIN_TTL_SECONDS="${AGP_ECR_LOGIN_TTL_SECONDS:-3600}"
LOCK_STALE_SECONDS="${AGP_ECR_LOGIN_LOCK_STALE_SECONDS:-120}"
LOCK_WAIT_SECONDS="${AGP_ECR_LOGIN_LOCK_WAIT_SECONDS:-180}"
mkdir -p "$STATE_DIR"

now() { date +%s; }

# Echo the epoch stored in $1, or fail. The epoch is written into the file (rather than read
# from its mtime) so no `stat` flag differences between BSD and GNU can bite.
epoch_in() {
  local value
  value="$(cat "$1" 2>/dev/null || true)"
  case "$value" in
    '' | *[!0-9]*) return 1 ;;
  esac
  printf '%s' "$value"
}

credential_is_fresh() {
  local stamped age
  stamped="$(epoch_in "$STAMP_FILE")" || return 1
  age=$(( $(now) - stamped ))
  [ "$age" -ge 0 ] && [ "$age" -lt "$LOGIN_TTL_SECONDS" ]
}

if credential_is_fresh; then
  echo "ECR login: $CE already authenticated to $ECR_REGISTRY less than ${LOGIN_TTL_SECONDS}s ago — skipping"
  exit 0
fi

LOCK_HELD=false
AWS_ERR=""
cleanup() {
  if [ -n "$AWS_ERR" ]; then rm -f "$AWS_ERR"; fi
  if [ "$LOCK_HELD" = true ]; then rm -rf "$LOCK_DIR"; fi
  return 0
}
trap cleanup EXIT INT TERM

waited=0
until mkdir "$LOCK_DIR" 2>/dev/null; do
  held_since="$(epoch_in "$LOCK_DIR/at" || true)"
  if [ -n "$held_since" ] && [ $(( $(now) - held_since )) -ge "$LOCK_STALE_SECONDS" ]; then
    echo "ECR login: clearing a stale lock left by an interrupted apply ($LOCK_DIR)"
    rm -rf "$LOCK_DIR"
    continue
  fi
  waited=$(( waited + 1 ))
  if [ "$waited" -gt "$LOCK_WAIT_SECONDS" ]; then
    echo "ERROR: waited ${LOCK_WAIT_SECONDS}s for the ECR login lock and it never freed." >&2
    echo "       If no other 'terraform apply' is running, delete it and re-apply:" >&2
    echo "         rm -rf '$LOCK_DIR'" >&2
    exit 1
  fi
  sleep 1
done
LOCK_HELD=true
now > "$LOCK_DIR/at"

# Re-check under the lock: a sibling may have logged in while we were queued, in which case
# there is nothing left to do and no second write to the keychain.
if credential_is_fresh; then
  echo "ECR login: a parallel mirror just authenticated $CE to $ECR_REGISTRY — skipping"
  exit 0
fi

# AWS side. Failing here is not an engine problem, and saying so is the difference between a
# 2-minute fix and an hour of guessing.
AWS_ERR="$(mktemp "${TMPDIR:-/tmp}/agp-ecr-login-aws-err.XXXXXX")"
if ! PASSWORD="$(aws ecr get-login-password --region "$REGION" 2>"$AWS_ERR")"; then
  echo "ERROR: could not get an ECR authorization token in $REGION. The AWS CLI said:" >&2
  cat "$AWS_ERR" >&2
  echo "       The apply principal needs ecr:GetAuthorizationToken and unexpired credentials." >&2
  exit 1
fi

# Engine side. The login's own exit status is the authoritative answer — no manifest probe,
# which cannot tell "authenticated, tag absent" from "not authenticated" and previously read
# the wrong way round.
if LOGIN_OUT="$(printf '%s' "$PASSWORD" | "$CE" login --username AWS --password-stdin "$ECR_REGISTRY" 2>&1)"; then
  if [ -n "$LOGIN_OUT" ]; then echo "$LOGIN_OUT"; fi
  now > "$STAMP_FILE"
  echo "ECR login: $CE authenticated to $ECR_REGISTRY"
  exit 0
fi

# Always print what the engine actually said. The previous implementation swallowed it, so
# the operator saw "cannot authenticate" with no cause.
echo "ERROR: '$CE login' to $ECR_REGISTRY failed. The engine said:" >&2
echo "$LOGIN_OUT" >&2

case "$LOGIN_OUT" in
  *-25299* | *"already exists in the keychain"*)
    # The mutex above should make this unreachable, but a keychain entry left stuck by an
    # earlier crashed login blocks every future write, so keep tolerating it: an entry for
    # this registry DOES exist, and the push either works or fails loudly on its own.
    # Deliberately NOT stamped — we did not prove a fresh credential was written, so the next
    # mirror retries the login instead of trusting this one.
    cat >&2 <<EOF
       That is the macOS keychain refusing to overwrite an existing credential for this
       registry (errSecDuplicateItem). A credential for $ECR_REGISTRY is stored, so the
       mirror will continue with it. If the push then reports "authentication required" or
       "no basic auth credentials", the stored entry is stale — clear it and re-apply:
         security delete-internet-password -s "$ECR_REGISTRY"
         terraform apply
EOF
    exit 0
    ;;
esac

cat >&2 <<EOF
       Nothing can be mirrored into $ECR_REGISTRY without this login, so the apply stops here.
       Check, in this order:
         1. the engine is up:            $CE info
         2. ECR is reachable for you:    aws ecr describe-repositories --region $REGION
         3. macOS only — a stuck keychain entry for this registry blocks every login:
              security delete-internet-password -s "$ECR_REGISTRY"
            then re-run terraform apply.
EOF
exit 1
