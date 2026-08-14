#!/bin/bash
# Deploy ALL three Acme example agents to Bedrock AgentCore — ONE non-interactive command.
# (Epic 7 — companion to applications/acme_reference_agent/deploy.sh.)
#
# WHAT THIS DOES
#   For each of: acme_contact_center_agent, acme_fnol_agent, acme_onboarding_agent
#   it runs the AgentCore "paved path" with EVERY prompt pre-answered, so the user types
#   nothing. The pinned answers (user-decided) reproduce the proven reference runtime exactly
#   (see applications/acme_reference_agent/.bedrock_agentcore.yaml):
#       name            = the agent's directory name
#       entrypoint      = agent.py
#       region          = us-east-1                (override via AWS_REGION)
#       deployment      = direct_code_deploy       (matches the reference yaml)
#       python runtime  = PYTHON_3_12              (the reference runtime_type)
#       memory          = DISABLED (NO_MEMORY)     (--disable-memory)
#       authentication  = NONE / IAM default       (NO inbound OAuth/CUSTOM_JWT authorizer)
#       execution role  = auto-create              (--non-interactive default)
#       S3 source bucket= auto-create              (--non-interactive default)
#
#   AUTH = NONE ON PURPOSE. We deliberately create each runtime WITHOUT an inbound CUSTOM_JWT
#   authorizer. The platform sets the real CUSTOM_JWT/Entra authorizer when the agent is
#   REGISTERED (the E6/E7 provisioning path). So a brand-new runtime starts open and is locked
#   down at registration — do NOT configure an authorizer here.
#
# FIRST-DEPLOY vs REDEPLOY (per agent, decided automatically)
#   FIRST deploy  (runtime does NOT exist yet): run a fully non-interactive `agentcore configure`
#                 with the pinned flags above, then `agentcore launch`. Nothing to preserve
#                 (auth=none, no prior runtime).
#   REDEPLOY      (runtime ALREADY exists): do NOT re-run configure here — `agentcore configure`
#                 rewrites .bedrock_agentcore.yaml and defaults the inbound authorizer back to
#                 IAM, and a subsequent full-replace `agentcore launch` would then DROP the
#                 CUSTOM_JWT/Entra gate the platform set at registration (research §12.7).
#                 Instead we DELEGATE to that agent's own deploy.sh, which detects the existing
#                 runtime, SKIPS its own configure, launches, and CAPTURES-then-RESTORES the
#                 inbound authorizer + the backend-injected env across launch. This reuses the
#                 proven preserve-on-redeploy logic without duplicating it.
#
# HOW WE PRE-ANSWER EVERY PROMPT (all VERIFIED against `agentcore configure --help` +
# the starter-toolkit source, v0.3.9):
#   * --non-interactive : the master switch. ConfigurationManager then returns, with NO prompt:
#                         exec-role -> auto-create, S3 -> auto-create, OAuth -> None (IAM/auth=none),
#                         request headers -> default. (configuration_manager.py)
#   * --name            : the agent name (FLAG).
#   * --entrypoint      : agent.py (FLAG).
#   * --region          : us-east-1 (FLAG).
#   * --deployment-type direct_code_deploy : matches the reference yaml (FLAG).
#   * --runtime PYTHON_3_12 : the Python version. ONLY valid with direct_code_deploy and
#                         incompatible with --ecr; with --non-interactive it would otherwise
#                         default to the host's Python (3.10 here), so we pin it explicitly (FLAG).
#   * --disable-memory  : memory -> NO_MEMORY (FLAG).
#   No stdin/heredoc piping is needed — every pinned answer is a real flag. (If you ever switch
#   to container deployment, drop --runtime: the Python version then comes from the base image,
#   and --runtime + container is rejected by the CLI.)
#
# OFFLINE-SAFE: --dry-run prints the exact commands (with the pinned answers visible) and makes
# NO agentcore/aws calls. The live deploy is the USER's step.
#
# PREREQS for the FIRST-deploy path (direct_code_deploy): `uv` and `zip` on PATH (the toolkit
# builds the dependency zip with them). `agentcore` is auto-installed if missing (same as the
# reference deploy.sh). `aws` + `jq` are needed for the existing-runtime detection and the
# delegated redeploy's preserve step.
set -euo pipefail

# ---- the three example agents (directory names == agent names) ------------
ALL_AGENTS=(acme_contact_center_agent acme_fnol_agent acme_onboarding_agent)

# ---- config (override via env) --------------------------------------------
AWS_REGION="${AWS_REGION:-us-east-1}"
ENTRYPOINT="${ENTRYPOINT:-agent.py}"
DEPLOYMENT_TYPE="${DEPLOYMENT_TYPE:-direct_code_deploy}"
RUNTIME_VERSION="${RUNTIME_VERSION:-PYTHON_3_12}"
# Force `agentcore configure` even on an EXISTING runtime (a full reconfigure). Default OFF.
# WARNING: on an existing runtime this re-runs configure which resets the inbound authorizer to
# IAM in the local yaml; the per-agent deploy.sh we delegate to then restores it after launch,
# but you are deliberately taking the heavier reconfigure path. Leave 0 for a plain code redeploy.
FORCE_CONFIGURE="${FORCE_CONFIGURE:-0}"
# AgentCore CLI config file name (per agent dir). configure/launch read/write it; it carries the
# created runtime arn at agents.<name>.bedrock_agentcore.agent_arn (same as the reference script).
AGENTCORE_CONFIG="${AGENTCORE_CONFIG:-.bedrock_agentcore.yaml}"

DRY_RUN=0

# Repo layout: this script lives in applications/, each agent is a sibling subdir.
APPLICATIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- usage ----------------------------------------------------------------
usage() {
  cat <<USAGE
Deploy ALL three Acme example agents to Bedrock AgentCore — non-interactively.

USAGE:
  deploy_all_agents.sh [--dry-run] [agent_dir ...]
  deploy_all_agents.sh --help

ARGS:
  agent_dir ...   Optional subset to deploy (directory names). Default: all three.
                  Valid: ${ALL_AGENTS[*]}

OPTIONS:
  --dry-run, -n   Print exactly what WOULD run for each agent (the full non-interactive
                  'agentcore configure' command + its pinned answers, and the launch /
                  delegate decision). Makes NO agentcore/aws calls. Exit 0.
  --help, -h      Show this help and exit 0.

PINNED ANSWERS (user-decided; reproduce the proven reference runtime):
  name            = each agent's directory name
  entrypoint      = ${ENTRYPOINT}
  region          = ${AWS_REGION}          (env: AWS_REGION)
  deployment-type = ${DEPLOYMENT_TYPE}     (env: DEPLOYMENT_TYPE)
  python runtime  = ${RUNTIME_VERSION}     (env: RUNTIME_VERSION)
  memory          = DISABLED (NO_MEMORY)
  authentication  = NONE / IAM default (NO inbound authorizer — the platform sets the real
                    CUSTOM_JWT/Entra authorizer at agent registration)
  execution role  = auto-create
  S3 source bucket= auto-create

ENV OVERRIDES:
  AWS_REGION        deploy region (default us-east-1)
  ENTRYPOINT        agent entry file (default agent.py)
  DEPLOYMENT_TYPE   direct_code_deploy | container (default direct_code_deploy)
  RUNTIME_VERSION   PYTHON_3_10|_3_11|_3_12|_3_13 (default PYTHON_3_12; direct_code_deploy only)
  FORCE_CONFIGURE=1 re-run 'agentcore configure' even on an existing runtime (heavier path;
                    can reset the inbound authorizer — the per-agent deploy.sh restores it).

BEHAVIOR:
  * FIRST deploy (runtime absent): non-interactive 'agentcore configure' + 'agentcore launch'.
  * REDEPLOY (runtime exists): delegate to applications/<agent>/deploy.sh, which skips its own
    configure and PRESERVES the inbound authorizer + env across launch (research §12.7).
  * Continue-on-failure: a failing agent is logged; the rest still run. A summary table prints
    at the end. Exit non-zero iff any agent failed.
USAGE
}

# ---- arg parsing ----------------------------------------------------------
SELECTED_AGENTS=()
for arg in "$@"; do
  case "${arg}" in
    --help|-h)
      usage
      exit 0
      ;;
    --dry-run|-n)
      DRY_RUN=1
      ;;
    -*)
      echo "ERROR: unknown option '${arg}'. Try --help." >&2
      exit 2
      ;;
    *)
      # Validate the requested agent against the known set (reject unknown names).
      _ok=0
      for known in "${ALL_AGENTS[@]}"; do
        if [ "${arg}" = "${known}" ]; then _ok=1; break; fi
      done
      if [ "${_ok}" != "1" ]; then
        echo "ERROR: unknown agent '${arg}'. Valid: ${ALL_AGENTS[*]}" >&2
        exit 2
      fi
      SELECTED_AGENTS+=("${arg}")
      ;;
  esac
done
# Default to all three when no subset was given.
if [ "${#SELECTED_AGENTS[@]}" -eq 0 ]; then
  SELECTED_AGENTS=("${ALL_AGENTS[@]}")
fi

# ---- dry-run guard --------------------------------------------------------
# Every live agentcore/aws call below sits in the `else` branch of an explicit
# `if [ "${DRY_RUN}" = "1" ]; then ...print and return... fi`, because each live call needs a
# `( cd <agent_dir> && ... )` subshell so configure/launch read the per-agent .bedrock_agentcore.yaml.
# In --dry-run we print the exact command (quoted via printf %q) and make NO agentcore/aws call.

# ---- preconditions --------------------------------------------------------
# In dry-run we skip live tool checks (offline-safe) but still surface what's needed.
preconditions() {
  if [ "${DRY_RUN}" = "1" ]; then
    echo "=== Preconditions (dry-run: not executed) ==="
    echo "    needs on PATH for a live run: agentcore (auto-installed if missing), aws, jq;"
    echo "    plus uv + zip for the first-deploy direct_code_deploy build."
    return 0
  fi

  if ! command -v agentcore >/dev/null 2>&1; then
    echo "Installing the AgentCore starter toolkit (provides the 'agentcore' CLI)..."
    pip install bedrock-agentcore-starter-toolkit
  fi
  if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: 'aws' CLI not found on PATH — needed to detect existing runtimes and for the" >&2
    echo "       delegated redeploy's authorizer/env preserve step. Install the AWS CLI." >&2
    exit 1
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "WARNING: 'jq' not found — the delegated redeploy path (existing runtime) needs it to"
    echo "         capture/restore the inbound authorizer + env. Install jq before redeploying an"
    echo "         already-registered agent, or the per-agent deploy.sh will warn and skip preserve."
  fi
  if [ "${DEPLOYMENT_TYPE}" = "direct_code_deploy" ]; then
    if ! command -v uv >/dev/null 2>&1; then
      echo "WARNING: 'uv' not found — direct_code_deploy needs it to build the dependency zip."
      echo "         Install uv (https://docs.astral.sh/uv/) or set DEPLOYMENT_TYPE=container."
    fi
    if ! command -v zip >/dev/null 2>&1; then
      echo "WARNING: 'zip' not found — direct_code_deploy needs it. Install zip or set DEPLOYMENT_TYPE=container."
    fi
  fi
}

# ---- per-agent detection: does the runtime already exist? -----------------
# Mirrors the reference deploy.sh Step-0 detection: try the on-disk .bedrock_agentcore.yaml
# (grep the agent_arn key — the file is YAML, not JSON), else fall back to a list-agent-runtimes
# lookup by name. Echoes the runtime id on stdout ("" if none / unknown). Never fatal.
# In --dry-run we make NO aws call (offline-safe): we report existence from the local yaml ONLY.
detect_runtime_id() {
  local agent_dir="$1"
  local cfg="${APPLICATIONS_DIR}/${agent_dir}/${AGENTCORE_CONFIG}"
  local rid=""

  if [ -f "${cfg}" ]; then
    rid="$(grep -E '^[[:space:]]*agent_arn:[[:space:]]*' "${cfg}" 2>/dev/null \
           | head -n1 \
           | sed -E "s/^[[:space:]]*agent_arn:[[:space:]]*//;s/[\"']//g" \
           || echo "")"
    if [ -n "${rid}" ] && [ "${rid}" != "null" ]; then
      echo "${rid##*/}"
      return 0
    fi
  fi

  # No usable local yaml. In dry-run we do NOT hit aws — treat as "unknown -> first deploy".
  if [ "${DRY_RUN}" = "1" ]; then
    echo ""
    return 0
  fi

  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
          --region "${AWS_REGION}" \
          --query "agentRuntimes[?agentRuntimeName=='${agent_dir}'].agentRuntimeId | [0]" \
          --output text 2>/dev/null || echo "")"
  if [ "${rid}" = "None" ]; then rid=""; fi
  echo "${rid}"
}

# ---- deploy ONE agent -----------------------------------------------------
# Returns 0 and sets AGENT_RESULT to DEPLOYED/SKIPPED; non-zero with AGENT_RESULT=FAILED.
AGENT_RESULT=""
AGENT_REASON=""
deploy_one_agent() {
  local agent_dir="$1"
  local agent_path="${APPLICATIONS_DIR}/${agent_dir}"
  AGENT_RESULT=""
  AGENT_REASON=""

  echo ""
  echo "############################################################"
  echo "### Agent: ${agent_dir}"
  echo "############################################################"

  if [ ! -d "${agent_path}" ]; then
    AGENT_RESULT="FAILED"
    AGENT_REASON="directory not found: ${agent_path}"
    echo "ERROR: ${AGENT_REASON}" >&2
    return 1
  fi
  if [ ! -f "${agent_path}/${ENTRYPOINT}" ]; then
    AGENT_RESULT="FAILED"
    AGENT_REASON="entrypoint '${ENTRYPOINT}' not found in ${agent_dir}"
    echo "ERROR: ${AGENT_REASON}" >&2
    return 1
  fi

  local runtime_id
  runtime_id="$(detect_runtime_id "${agent_dir}")"
  local local_cfg_present=0
  [ -f "${agent_path}/${AGENTCORE_CONFIG}" ] && local_cfg_present=1

  # Decide: REDEPLOY (existing runtime, not forcing reconfigure) -> delegate; else FIRST deploy.
  if [ -n "${runtime_id}" ] && [ "${FORCE_CONFIGURE}" != "1" ]; then
    echo "=== REDEPLOY: existing runtime detected (id=${runtime_id}) ==="
    echo "    Delegating to ${agent_dir}/deploy.sh — it SKIPS configure (keeps the registered"
    echo "    authorizer) and PRESERVES the inbound authorizer + env across launch (research §12.7)."
    # Delegate to the agent's own proven deploy.sh from inside its dir (it reads ./agent.py and
    # ./.bedrock_agentcore.yaml relative to cwd). We export the same env knobs it honors.
    if [ "${DRY_RUN}" = "1" ]; then
      printf '    [dry-run] would run: (cd %q && AGENT_NAME=%q AWS_REGION=%q ENTRYPOINT=%q ./deploy.sh)\n' \
        "${agent_path}" "${agent_dir}" "${AWS_REGION}" "${ENTRYPOINT}"
      AGENT_RESULT="DEPLOYED"
      AGENT_REASON="redeploy via ${agent_dir}/deploy.sh (preserve authorizer+env)"
      return 0
    fi
    if ( cd "${agent_path}" && AGENT_NAME="${agent_dir}" AWS_REGION="${AWS_REGION}" ENTRYPOINT="${ENTRYPOINT}" ./deploy.sh ); then
      AGENT_RESULT="DEPLOYED"
      AGENT_REASON="redeployed via ${agent_dir}/deploy.sh (authorizer+env preserved)"
      return 0
    else
      AGENT_RESULT="FAILED"
      AGENT_REASON="${agent_dir}/deploy.sh exited non-zero on redeploy"
      echo "ERROR: ${AGENT_REASON}" >&2
      return 1
    fi
  fi

  # FIRST deploy (or FORCE_CONFIGURE=1): fully non-interactive configure + launch.
  if [ -n "${runtime_id}" ] && [ "${FORCE_CONFIGURE}" = "1" ]; then
    echo "=== FORCE_CONFIGURE=1: reconfiguring an EXISTING runtime (id=${runtime_id}) on purpose ==="
    echo "    WARNING: configure resets the inbound authorizer to IAM in the local yaml; launch"
    echo "    would push that. Only do this if you intend to re-establish the authorizer afterward."
  else
    echo "=== FIRST deploy: no existing runtime detected ==="
    [ "${DRY_RUN}" = "1" ] && [ "${local_cfg_present}" = "0" ] && \
      echo "    (dry-run note: existence checked from the local ${AGENTCORE_CONFIG} only — no aws call)"
  fi

  echo "    Pinned: name=${agent_dir} entrypoint=${ENTRYPOINT} region=${AWS_REGION}"
  echo "            deployment=${DEPLOYMENT_TYPE} runtime=${RUNTIME_VERSION} memory=DISABLED auth=NONE"
  echo "            exec-role=auto-create  S3-source=auto-create"

  # Build the non-interactive configure command. --runtime is ONLY valid with direct_code_deploy
  # (and incompatible with --ecr/container), so include it only for that deployment type.
  local -a configure_cmd=(
    agentcore configure
      --non-interactive
      --name "${agent_dir}"
      --entrypoint "${ENTRYPOINT}"
      --region "${AWS_REGION}"
      --deployment-type "${DEPLOYMENT_TYPE}"
      --disable-memory
  )
  if [ "${DEPLOYMENT_TYPE}" = "direct_code_deploy" ]; then
    configure_cmd+=(--runtime "${RUNTIME_VERSION}")
  fi
  # NOTE: auth=NONE is the --non-interactive default (no --authorizer-config => IAM, the platform
  # sets CUSTOM_JWT at registration). exec-role & S3 auto-create are the --non-interactive defaults.

  echo "--- Step 1/2: agentcore configure (non-interactive) ---"
  # configure/launch read+write ./.bedrock_agentcore.yaml relative to cwd, so run from the agent dir.
  if [ "${DRY_RUN}" = "1" ]; then
    printf '    [dry-run] would run: (cd %q && ' "${agent_path}"
    printf '%q ' "${configure_cmd[@]}"
    printf ')\n'
    echo "--- Step 2/2: agentcore launch (cloud direct_code_deploy / ARM64) ---"
    printf '    [dry-run] would run: (cd %q && ' "${agent_path}"
    printf '%q ' agentcore launch
    printf ')\n'
    AGENT_RESULT="DEPLOYED"
    AGENT_REASON="first deploy (configure --non-interactive + launch)"
    return 0
  fi

  if ! ( cd "${agent_path}" && "${configure_cmd[@]}" ); then
    AGENT_RESULT="FAILED"
    AGENT_REASON="agentcore configure failed"
    echo "ERROR: ${AGENT_REASON}" >&2
    return 1
  fi

  echo "--- Step 2/2: agentcore launch (cloud direct_code_deploy / ARM64) ---"
  if ! ( cd "${agent_path}" && agentcore launch ); then
    AGENT_RESULT="FAILED"
    AGENT_REASON="agentcore launch failed"
    echo "ERROR: ${AGENT_REASON}" >&2
    return 1
  fi

  echo "    NOTE: this runtime is NOT gated yet (auth=NONE). The platform sets the CUSTOM_JWT/Entra"
  echo "          authorizer when the agent is REGISTERED. Do not route real user traffic before that."
  AGENT_RESULT="DEPLOYED"
  AGENT_REASON="first deploy (configure --non-interactive + launch)"
  return 0
}

# ---- main -----------------------------------------------------------------
echo "=== Deploy ALL Acme example agents — AgentCore (non-interactive) ==="
echo "Agents:      ${SELECTED_AGENTS[*]}"
echo "Region:      ${AWS_REGION}"
echo "Deployment:  ${DEPLOYMENT_TYPE}  (runtime ${RUNTIME_VERSION} for direct_code_deploy)"
echo "Mode:        $([ "${DRY_RUN}" = "1" ] && echo 'DRY-RUN (no agentcore/aws calls)' || echo 'LIVE')"
[ "${FORCE_CONFIGURE}" = "1" ] && echo "FORCE_CONFIGURE=1 (will reconfigure existing runtimes — can reset the authorizer)"

preconditions

# Per-agent results for the summary (parallel arrays — bash 3.2 compatible, macOS default).
RESULT_AGENTS=()
RESULT_STATUS=()
RESULT_REASON=()
ANY_FAILED=0

for agent in "${SELECTED_AGENTS[@]}"; do
  # Continue-on-failure: never let one agent abort the loop. Temporarily relax set -e around
  # the per-agent call so a non-zero return is captured, logged, and we move on.
  set +e
  deploy_one_agent "${agent}"
  set -e
  RESULT_AGENTS+=("${agent}")
  RESULT_STATUS+=("${AGENT_RESULT:-FAILED}")
  RESULT_REASON+=("${AGENT_REASON:-unknown}")
  if [ "${AGENT_RESULT:-FAILED}" = "FAILED" ]; then
    ANY_FAILED=1
  fi
done

# ---- summary --------------------------------------------------------------
echo ""
echo "============================================================"
echo "=== SUMMARY$([ "${DRY_RUN}" = "1" ] && echo ' (dry-run)') ==="
echo "============================================================"
i=0
while [ "${i}" -lt "${#RESULT_AGENTS[@]}" ]; do
  printf '  %-32s %-10s %s\n' "${RESULT_AGENTS[$i]}" "${RESULT_STATUS[$i]}" "${RESULT_REASON[$i]}"
  i=$((i + 1))
done
echo "============================================================"

if [ "${ANY_FAILED}" = "1" ]; then
  echo "RESULT: one or more agents FAILED (see above)." >&2
  exit 1
fi
echo "RESULT: all selected agents OK$([ "${DRY_RUN}" = "1" ] && echo ' (dry-run — nothing was deployed)')."
exit 0
