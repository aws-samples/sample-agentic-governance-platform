#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <terraform-show-json>" >&2
  exit 2
}

[ $# -eq 1 ] || usage
SHOW_JSON="$1"

jq -c '
  [.resource_changes[]?.change.actions[]?] as $actions
  | {
      create: ($actions | map(select(. == "create")) | length),
      update: ($actions | map(select(. == "update")) | length),
      delete: ($actions | map(select(. == "delete")) | length)
    }
' "$SHOW_JSON"
