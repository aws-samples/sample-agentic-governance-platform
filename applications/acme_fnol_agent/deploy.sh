#!/bin/bash
# Acme reference agent — deploy to Bedrock AgentCore Runtime (Epic 7, Task T-REF-AGENT).
#
# Wraps the AgentCore "paved path": `agentcore configure` (writes .bedrock_agentcore.yaml:
# ECR repo, exec role, CodeBuild) then `agentcore launch` (builds linux/arm64, pushes ECR,
# creates/updates the runtime). After launch, the runtime's inbound JWT authorizer must
# trust Entra so only the platform's user-minted tokens get in — this is set EITHER by our
# control-plane's agent provisioning (the E6 path) OR by the optional post-create
# UpdateAgentRuntime step below (run with SET_AUTHORIZER=1).
#
# PRESERVE-ON-REDEPLOY (T-REFAGENT-DEPLOY-PRESERVE): a code redeploy of an EXISTING runtime
# must never leave it weaker than it found it. Two independent safeguards:
#
#   (A) SKIP `agentcore configure` on an existing runtime. `agentcore configure` is interactive
#       and ALWAYS re-runs: on an existing runtime it re-prompts (python version / S3 / IAM-auth)
#       and REWRITES .bedrock_agentcore.yaml, defaulting the inbound authorizer to IAM
#       (authorizer_configuration: null). So on a redeploy we SKIP configure (code-only redeploy)
#       and let `agentcore launch` read the on-disk yaml standalone, MAINTAINING all existing
#       runtime settings. Configure still runs on a first deploy, or when the local yaml is absent
#       (id resolved via the list fallback — configure regenerates it). Escape hatch:
#       FORCE_CONFIGURE=1 reconfigures an existing runtime on purpose. (See step 1.)
#
#   (B) CAPTURE-then-RESTORE around launch. `agentcore launch` does a full-replace
#       UpdateAgentRuntime under the hood (research §12.7: a field NOT replayed is silently
#       DROPPED), which can RESET two things the platform's provisioning set on the runtime:
#       (1) the inbound CUSTOM_JWT/Entra authorizer — on the AgentRuntime API this is expressed
#       SOLELY by authorizerConfiguration.customJWTAuthorizer (botocore 1.43.16: there is NO
#       `authorizerType` field on GetAgentRuntime/UpdateAgentRuntime — that lives only on
#       Gateways), so we KEY capture/restore on that configuration, never on a phantom type;
#       and (2) the environmentVariables the backend injects at GRANT time (CREDENTIAL_PROVIDER_NAME
#       / MCP_GATEWAY_URL / MCP_AUDIENCE / AWS_REGION — see set_runtime_environment); dropping those
#       makes agent.py KeyError on its next invoke. So when the runtime ALREADY exists, this script
#       CAPTURES its config (inbound customJWTAuthorizer + environmentVariables) BEFORE launch and,
#       if launch reset EITHER, RESTORES them AFTER launch in ONE full-replace UpdateAgentRuntime
#       that replays the FRESH post-launch code artifact + ONLY the pre-launch authorizerConfiguration
#       (no authorizerType — the input shape has no such member) + a MERGE of the environment
#       (post-launch env overlaid by the pre-launch env, so the backend-injected vars survive while
#       any env a new launch legitimately introduced is kept).
#
# The authorizer AND the env are preserved (launch didn't touch them), restored (launch reset them
# → we put them back), or the user is LOUDLY warned to fix it manually (capture/restore failed →
# re-grant). They are preserved INDEPENDENTLY — a runtime with an authorizer but no env, env but no
# authorizer, or both, are all handled. First deploy (no runtime yet) behaves as before — nothing
# to preserve.
#
# The live deploy + invoke is the USER's step. See README.md for the full runbook
# (prereqs: terraform apply the IAM; grant the agent the MCP in the UI; invoke as an
# assigned user). This script performs the build/launch; it does NOT create the Entra
# credential provider (the backend does that at grant time, T-CRED-PROVIDER).
#
# RUNTIME EXEC-ROLE IAM (research §3.5 / §7, T-INFRA-DEPS): the runtime's execution role
# (created by `agentcore configure`) needs the AgentCore Identity data-plane token utils
# for the OBO call in agent.py:
#     bedrock-agentcore:GetWorkloadAccessToken
#     bedrock-agentcore:GetWorkloadAccessTokenForJWT
#     bedrock-agentcore:GetResourceOauth2Token
# scoped to token-vault/default/oauth2credentialprovider/* + workload-identity-directory/default/*.
# These are documented in README.md; attach them to the runtime exec role after configure.
#
# LANGFUSE OBSERVABILITY — RUNTIME ENV (research 2026-06-05-langfuse-strands-agent-observability §5):
#   The agent emits full Strands traces + token usage + per-user (oid) attribution to Langfuse.
#   ALL of the OTEL wiring is set IN-CODE at module top in agent.py (the PRIMARY path) — the four
#   OTEL vars below are read LAZILY by the exporter at construction, so hardcoding them in agent.py
#   is reliable, and they are DELIBERATELY kept OUT of this deploy script (sending the
#   secret-bearing OTEL_EXPORTER_OTLP_HEADERS through an UpdateAgentRuntime call would needlessly
#   widen secret exposure):
#       OTEL_EXPORTER_OTLP_ENDPOINT = <langfuse-host>/api/public/otel   (BASE form, no /v1/traces)
#       OTEL_EXPORTER_OTLP_HEADERS  = Authorization=Basic <base64(pk:sk)>
#       OTEL_EXPORTER_OTLP_PROTOCOL = http/protobuf
#       OTEL_SEMCONV_STABILITY_OPT_IN = gen_ai_latest_experimental,gen_ai_tool_definitions
#   THE ONE TRAP (§0.2/§5): AgentCore auto-instruments with ADOT and OWNS the global OTEL
#   TracerProvider FIRST (set-once) — a later StrandsTelemetry() is silently no-op'd and Langfuse
#   gets NOTHING. The fix is DISABLE_ADOT_OBSERVABILITY=true. agent.py sets it at module top as a
#   BEST-EFFORT belt-and-suspenders fallback, but ADOT may bootstrap before that line runs, so the
#   RELIABLE place is a DEPLOY-TIME runtime env var — and THIS SCRIPT NOW SETS IT for you (no longer
#   a manual step). Of all the OTEL knobs above, DISABLE_ADOT_OBSERVABILITY is the ONLY one that
#   must be a real runtime env var; the rest stay in agent.py. We set it on the runtime two ways
#   that reinforce each other:
#     - `agentcore launch --env DISABLE_ADOT_OBSERVABILITY=true` (the toolkit's native runtime-env
#       flag — verified present on this CLI) puts it on the runtime as part of launch itself, so
#       it lands on the FIRST deploy directly; AND
#     - the preserve-on-redeploy step (2b) ALWAYS force-injects DISABLE_ADOT_OBSERVABILITY=true into
#       the merged environmentVariables it replays, so on a REDEPLOY it survives launch's
#       full-replace alongside the backend-injected governance vars (CREDENTIAL_PROVIDER_NAME /
#       MCP_GATEWAY_URL / MCP_AUDIENCE / AWS_REGION) — merged, never clobbering them, and never
#       dropping the inbound CUSTOM_JWT authorizer (research §12.7).
#   We export it here too as a harmless best-effort for any local `agentcore launch --local` run
#   (no-op on a cloud deploy if the toolkit ignores shell env — the --env flag is the real path).
#   The DISABLE_ADOT_OBSERVABILITY knob below is overridable (set it to "false" to keep ADOT/CloudWatch).
export DISABLE_ADOT_OBSERVABILITY="${DISABLE_ADOT_OBSERVABILITY:-true}"
# LANGFUSE KEY SOURCING (E26/T10 — closes E12 gate #2): the Langfuse project key is NO LONGER a
# hardcoded literal in agent.py. agent.py reads its per-agent project key from AWS Secrets Manager
# at import, given the secret NAME + host as runtime env. Both values below are NON-SECRET (a URL +
# a Secrets Manager NAME — the {public_key,secret_key} VALUE stays in Secrets Manager and NEVER
# transits this script), operator-supplied via the environment (no hardcoding). When either is
# empty, telemetry is simply disabled (the agent still runs). Passed to the runtime two reinforcing
# ways below: on `agentcore launch --env` (first deploy) AND force-injected into the merged env on
# a redeploy (survives launch's full-replace, alongside DISABLE_ADOT / the backend MCP env).
export LANGFUSE_HOST="${LANGFUSE_HOST:-}"
export LANGFUSE_SECRET_NAME="${LANGFUSE_SECRET_NAME:-}"
set -euo pipefail

# ---- config (override via env) --------------------------------------------
AGENT_NAME="${AGENT_NAME:-acme_fnol_agent}"
ENTRYPOINT="${ENTRYPOINT:-agent.py}"
AWS_REGION="${AWS_REGION:-us-east-1}"
# Optional post-create authorizer set. Default OFF — our control-plane provisioning
# normally sets the runtime authorizer (the E6 UpdateAgentRuntime path). Set to 1 only if
# you want this script to flip the authorizer to CUSTOM_JWT/Entra directly.
SET_AUTHORIZER="${SET_AUTHORIZER:-0}"
# Force `agentcore configure` even on an EXISTING runtime. Default OFF. On a redeploy of an
# existing runtime this script SKIPS `agentcore configure` (code-only redeploy: launch reads
# the on-disk .bedrock_agentcore.yaml standalone and we keep all other runtime settings as
# they are — see step 1). `agentcore configure` is interactive and ALWAYS re-runs: on an
# existing runtime it re-prompts (python version / S3 / IAM-auth) and REWRITES
# .bedrock_agentcore.yaml, defaulting the inbound authorizer to IAM (authorizer_configuration:
# null) — which a subsequent full-replace launch would then push, DROPPING the CUSTOM_JWT/Entra
# gate. Set FORCE_CONFIGURE=1 only when you DELIBERATELY want to reconfigure an existing runtime
# (re-run the wizard); a plain code redeploy must leave it 0.
FORCE_CONFIGURE="${FORCE_CONFIGURE:-0}"
# AgentCore CLI config file — `agentcore configure`/`launch` read/write this; it carries the
# created runtime arn (under agents.<name>.bedrock_agentcore.agent_arn). We attempt a plain
# grep/sed YAML parse to extract the arn first (no YAML parser dep), then fall back to a
# list-agent-runtimes lookup by name when the file is absent or the key is not found.
AGENTCORE_CONFIG="${AGENTCORE_CONFIG:-.bedrock_agentcore.yaml}"

echo "=== Acme reference agent — AgentCore deploy ==="
echo "Agent name:  ${AGENT_NAME}"
echo "Entrypoint:  ${ENTRYPOINT}"
echo "Region:      ${AWS_REGION}"

# ---- prerequisites --------------------------------------------------------
if ! command -v agentcore >/dev/null 2>&1; then
  echo "Installing the AgentCore starter toolkit (provides the 'agentcore' CLI)..."
  pip install bedrock-agentcore-starter-toolkit
fi

# ---- 0. detect an existing runtime + capture its inbound authorizer -------
# (T-REFAGENT-DEPLOY-PRESERVE) BEFORE configure/launch we work out whether this runtime
# already exists. If it does, we capture its current inbound authorizer so we can restore it
# after launch (launch does a full-replace UpdateAgentRuntime that can reset it). On a first
# deploy (no runtime yet) there is nothing to preserve — fall through to the original flow.
#
# When SET_AUTHORIZER=1 the user is EXPLICITLY (re)setting the authorizer from this script
# (step 3 below), so capture/restore is moot for that run — skip it.
RUNTIME_EXISTS=0
EXISTING_RUNTIME_ID=""
CAPTURED_AUTHORIZER_TYPE=""
CAPTURED_HAS_AUTHORIZER=0   # 1 if the pre-launch snapshot has a non-default inbound authorizer
CAPTURED_HAS_ENV=0          # 1 if the pre-launch snapshot has any environmentVariables to keep
CAPTURED_ENV_KEYS=""        # comma-joined key NAMES of the captured env (non-secret breadcrumb)
CAPTURE_OK=0          # 1 once we have a usable pre-launch snapshot AND something worth
                      # preserving (authorizer OR env) — drives the post-launch restore.
PRELAUNCH_JSON=""     # tempfile: full GetAgentRuntime response captured BEFORE launch
POSTLAUNCH_JSON=""    # tempfile: GetAgentRuntime response captured AFTER launch
RESTORE_JSON=""       # tempfile: UpdateAgentRuntime full-replace input for the restore

# Ensure all tempfiles are cleaned up on any exit (normal, set -e abort, or signal).
trap 'rm -f "${PRELAUNCH_JSON:-}" "${POSTLAUNCH_JSON:-}" "${RESTORE_JSON:-}" 2>/dev/null || true' EXIT

if [ "${SET_AUTHORIZER}" != "1" ]; then
  # Resolve the existing runtime id. Try the AgentCore config file first (grep/sed YAML
  # parse — the file is YAML, not JSON, so jq cannot read it). The toolkit writes the
  # runtime ARN at agents.<name>.bedrock_agentcore.agent_arn; grep for the `agent_arn:` key
  # and strip the value. If the file is absent or the key is not found, fall back to a
  # list-agent-runtimes lookup by name. Either lookup failing is NOT fatal — we just treat it
  # as a first deploy (do not break the no-runtime-yet path).
  if [ -f "${AGENTCORE_CONFIG}" ]; then
    CFG_ARN="$(grep -E '^[[:space:]]*agent_arn:[[:space:]]*' "${AGENTCORE_CONFIG}" 2>/dev/null \
               | head -n1 \
               | sed -E "s/^[[:space:]]*agent_arn:[[:space:]]*//;s/[\"']//g" \
               || echo "")"
    if [ -n "${CFG_ARN}" ] && [ "${CFG_ARN}" != "null" ]; then
      EXISTING_RUNTIME_ID="${CFG_ARN##*/}"
    fi
  fi
  if [ -z "${EXISTING_RUNTIME_ID}" ]; then
    # Fall back to a control-plane list lookup filtered by name (repo script idiom:
    # --query/--output text, tolerant of failure). agentRuntimeName matches AGENT_NAME.
    EXISTING_RUNTIME_ID="$(aws bedrock-agentcore-control list-agent-runtimes \
      --region "${AWS_REGION}" \
      --query "agentRuntimes[?agentRuntimeName=='${AGENT_NAME}'].agentRuntimeId | [0]" \
      --output text 2>/dev/null || echo "")"
    # --output text renders an empty result as the literal "None".
    if [ "${EXISTING_RUNTIME_ID}" = "None" ]; then
      EXISTING_RUNTIME_ID=""
    fi
  fi

  if [ -n "${EXISTING_RUNTIME_ID}" ]; then
    RUNTIME_EXISTS=1
    echo "=== Step 0: existing runtime detected (id=${EXISTING_RUNTIME_ID}) — will preserve its inbound authorizer AND environment variables ==="
    if ! command -v jq >/dev/null 2>&1; then
      # The capture/restore replay needs structured JSON (authorizerConfiguration +
      # environmentVariables + agentRuntimeArtifact/roleArn/networkConfiguration);
      # --query/--output text cannot reconstruct nested JSON for re-submission. Without jq we
      # CANNOT safely restore EITHER the authorizer OR the env, so warn loud now and do NOT
      # pretend we will preserve them (an un-restored reset = an open runtime AND an agent that
      # KeyErrors on its env). The user must verify/restore both manually after launch.
      echo "WARNING: 'jq' not found — cannot capture/restore the inbound authorizer OR the environment"
      echo "         variables for an existing runtime."
      echo "WARNING: 'agentcore launch' may RESET this runtime's inbound authorizer AND wipe its"
      echo "         environmentVariables (CREDENTIAL_PROVIDER_NAME / MCP_GATEWAY_URL / MCP_AUDIENCE /"
      echo "         AWS_REGION). After launch you MUST verify the inbound is still CUSTOM_JWT/Entra and"
      echo "         that the env vars are present, and re-run platform provisioning / re-grant the agent"
      echo "         (or SET_AUTHORIZER=1) if either was reset. Install jq to enable automatic"
      echo "         preserve-on-redeploy."
    else
      # Capture the FULL current runtime config BEFORE launch (the pre-launch snapshot). We
      # keep the whole response so the restore can replay the authorizer AND the env verbatim
      # (both live in this single GET — no extra API call needed).
      PRELAUNCH_JSON="$(mktemp -t agp-refagent-prelaunch.XXXXXX)"
      if aws bedrock-agentcore-control get-agent-runtime \
            --agent-runtime-id "${EXISTING_RUNTIME_ID}" --region "${AWS_REGION}" \
            --output json >"${PRELAUNCH_JSON}" 2>/dev/null; then
        # --- authorizer: a non-default inbound (a CUSTOM_JWT customJWTAuthorizer) is the
        # security gate we must preserve. The AgentRuntime API expresses inbound auth SOLELY
        # via authorizerConfiguration.customJWTAuthorizer (botocore 1.43.16: GetAgentRuntime /
        # UpdateAgentRuntime have NO `authorizerType` field — that lives only on Gateways). The
        # default inbound (IAM) has authorizerConfiguration == null, so a present customJWTAuthorizer
        # IS the thing to restore. We KEY the capture on the configuration, not on a phantom type.
        if [ "$(jq -r 'if (.authorizerConfiguration != null and .authorizerConfiguration.customJWTAuthorizer != null) then "true" else "false" end' "${PRELAUNCH_JSON}" 2>/dev/null || echo false)" = "true" ]; then
          CAPTURED_HAS_AUTHORIZER=1
          # Static, non-API label for friendlier logs only (NOT a field we ever send on update).
          CAPTURED_AUTHORIZER_TYPE="CUSTOM_JWT"
          echo "Captured inbound authorizer: customJWTAuthorizer present (CUSTOM_JWT/Entra gate)."
        else
          echo "Existing runtime has no non-default inbound authorizer to preserve (no customJWTAuthorizer)."
        fi
        # --- environmentVariables: the backend injects the 4 governance vars at grant time
        # (CREDENTIAL_PROVIDER_NAME / MCP_GATEWAY_URL / MCP_AUDIENCE / AWS_REGION); launch's
        # full-replace would wipe them. Capture whether any env exists + its KEY names so we can
        # decide-and-restore independently of the authorizer. The key names are non-secret
        # breadcrumbs (research §12.9 — names/counts safe; we never print a value/token).
        if [ "$(jq -r '((.environmentVariables // {}) | length) > 0' "${PRELAUNCH_JSON}" 2>/dev/null || echo false)" = "true" ]; then
          CAPTURED_HAS_ENV=1
          CAPTURED_ENV_KEYS="$(jq -r '(.environmentVariables // {}) | keys | join(", ")' "${PRELAUNCH_JSON}" 2>/dev/null || echo "")"
          echo "Captured environment variables: $(jq -r '(.environmentVariables // {}) | length' "${PRELAUNCH_JSON}" 2>/dev/null || echo 0) var(s) [${CAPTURED_ENV_KEYS}]"
        else
          echo "Existing runtime has no environment variables to preserve."
        fi
        # We have a usable pre-launch snapshot AND something worth preserving (authorizer OR
        # env) -> arm the post-launch restore. The two are decoupled on purpose: a runtime with
        # only an authorizer, only env, or both, all set CAPTURE_OK.
        if [ "${CAPTURED_HAS_AUTHORIZER}" = "1" ] || [ "${CAPTURED_HAS_ENV}" = "1" ]; then
          CAPTURE_OK=1
        else
          echo "Nothing to preserve on this runtime (no non-default authorizer and no env) — nothing to restore after launch."
        fi
      else
        # Runtime exists but the GET failed — do NOT silently proceed as if preserved.
        echo "WARNING: could NOT capture the runtime config for existing runtime ${EXISTING_RUNTIME_ID} (GetAgentRuntime failed)."
        echo "WARNING: 'agentcore launch' may RESET the inbound authorizer AND wipe the environment variables."
        echo "         After launch you MUST verify the inbound is still CUSTOM_JWT/Entra and the env vars are"
        echo "         present, and re-run platform provisioning / re-grant the agent (or SET_AUTHORIZER=1) if"
        echo "         either was reset."
      fi
    fi
  else
    echo "=== Step 0: no existing runtime found — first deploy (nothing to preserve) ==="
  fi
fi

# ---- 1. configure ---------------------------------------------------------
# Writes .bedrock_agentcore.yaml (ECR repo, exec role, CodeBuild config).
#
# SKIP-ON-REDEPLOY (BUG-1 fix): `agentcore configure` is interactive and ALWAYS re-runs — on an
# EXISTING runtime it re-prompts (python version / S3 / IAM-auth) and REWRITES
# .bedrock_agentcore.yaml, defaulting the inbound authorizer to IAM (authorizer_configuration:
# null). `agentcore launch` then sources the authorizer from that yaml and its full-replace
# UpdateAgentRuntime DROPS the CUSTOM_JWT/Entra gate. The user's requirement: if the runtime
# EXISTS, just update the code (launch) and MAINTAIN ALL THE REST AS IT IS. So we run configure
# ONLY when there is something to configure:
#   - first deploy (RUNTIME_EXISTS=0)                          -> run configure (as before);
#   - existing runtime but the local yaml is ABSENT (id came   -> run configure (regenerate the
#     from the list-agent-runtimes fallback, no on-disk cfg)      yaml so launch can read it;
#                                                                  step 2b restores the authorizer
#                                                                  after launch);
#   - existing runtime AND the yaml is present                 -> SKIP configure (code-only
#                                                                  redeploy; launch reads the
#                                                                  on-disk yaml standalone — no
#                                                                  preceding configure needed,
#                                                                  confirmed against the toolkit:
#                                                                  launch_bedrock_agentcore does
#                                                                  load_config(config_path)).
# Escape hatch: FORCE_CONFIGURE=1 runs configure even on an existing runtime (deliberate reconfigure).
RUN_CONFIGURE=1
if [ "${RUNTIME_EXISTS}" = "1" ] && [ "${FORCE_CONFIGURE}" != "1" ] && [ -f "${AGENTCORE_CONFIG}" ]; then
  RUN_CONFIGURE=0
fi
if [ "${RUN_CONFIGURE}" = "1" ]; then
  echo "=== Step 1/2: agentcore configure ==="
  if [ "${RUNTIME_EXISTS}" = "1" ] && [ "${FORCE_CONFIGURE}" = "1" ]; then
    echo "(FORCE_CONFIGURE=1 — reconfiguring an existing runtime on purpose; step 2b will restore the inbound authorizer + env after launch.)"
  elif [ "${RUNTIME_EXISTS}" = "1" ]; then
    echo "(existing runtime but no local ${AGENTCORE_CONFIG} — running configure to regenerate it so launch can read it; step 2b will restore the inbound authorizer + env after launch.)"
  fi
  agentcore configure --entrypoint "${ENTRYPOINT}" --name "${AGENT_NAME}" --region "${AWS_REGION}"
else
  echo "=== Step 1/2: existing runtime + ${AGENTCORE_CONFIG} present -> SKIPPING agentcore configure (code-only redeploy); MAINTAINING existing runtime settings (set FORCE_CONFIGURE=1 to reconfigure) ==="
fi

# ---- 2. launch (ARM64) ----------------------------------------------------
# Builds linux/arm64, pushes to ECR, creates/updates the AgentCore Runtime.
#
# LANGFUSE: pass DISABLE_ADOT_OBSERVABILITY on the runtime via the toolkit's native --env flag
# (verified present: `agentcore launch --env KEY=VALUE`). This is the reliable place for the
# ADOT-disable knob — a real runtime env var, not a best-effort in-code line — so the Langfuse
# OTEL exporter (wired in agent.py) wins the global TracerProvider. ONLY DISABLE_ADOT goes here;
# the OTEL endpoint/headers/protocol stay hardcoded in agent.py (the secret-bearing
# OTEL_EXPORTER_OTLP_HEADERS deliberately never transits this script). On a redeploy, launch's
# full-replace would still wipe the backend MCP env, but step 2b below restores them AND
# re-asserts DISABLE_ADOT in the merged env, so all survive together.
echo "=== Step 2/2: agentcore launch (linux/arm64) ==="
LAUNCH_ENV_ARGS=()
if [ -n "${DISABLE_ADOT_OBSERVABILITY:-}" ]; then
  LAUNCH_ENV_ARGS+=(--env "DISABLE_ADOT_OBSERVABILITY=${DISABLE_ADOT_OBSERVABILITY}")
fi
# LANGFUSE (E26/T10): pass the NON-SECRET host + secret NAME so agent.py can read its per-agent
# project key from Secrets Manager. Only when set (empty ⇒ telemetry disabled). No secret VALUE here.
if [ -n "${LANGFUSE_HOST:-}" ]; then
  LAUNCH_ENV_ARGS+=(--env "LANGFUSE_HOST=${LANGFUSE_HOST}")
fi
if [ -n "${LANGFUSE_SECRET_NAME:-}" ]; then
  LAUNCH_ENV_ARGS+=(--env "LANGFUSE_SECRET_NAME=${LANGFUSE_SECRET_NAME}")
fi
# Expand safely even when the array is empty under `set -u` (macOS bash 3.2 trips on a bare
# "${arr[@]}" for an empty array): the +alternate form yields nothing when unset/empty.
agentcore launch ${LAUNCH_ENV_ARGS[@]+"${LAUNCH_ENV_ARGS[@]}"}

# ---- 2b. preserve the inbound authorizer AND env across launch ------------
# (T-REFAGENT-DEPLOY-PRESERVE) If the runtime pre-existed AND we captured something worth
# preserving before launch (a CUSTOM_JWT-style inbound and/or environmentVariables), re-read
# the runtime now and RESTORE whatever launch reset/changed. The crux: we replay the FRESH
# POST-LAUNCH agentRuntimeArtifact (the NEW code we just shipped — NOT the pre-launch artifact,
# which would revert the deploy) together with the PRE-LAUNCH authorizer and a MERGE of the
# environment (post-launch env overlaid by pre-launch env: pre wins — see the merge note in
# the jq below). LANGFUSE: the merge ALSO force-injects DISABLE_ADOT_OBSERVABILITY=true so the
# ADOT-disable knob ends up on the runtime on a REDEPLOY too (launch's --env handles the FIRST
# deploy; this re-asserts it after launch's full-replace, alongside the restored MCP env).
# Idempotent: if launch left the authorizer AND the env untouched AND DISABLE_ADOT is already
# correct, skip the restore. A single UpdateAgentRuntime restores everything at once.
if [ "${RUNTIME_EXISTS}" = "1" ] && [ "${CAPTURE_OK}" = "1" ]; then
  POSTLAUNCH_JSON="$(mktemp -t agp-refagent-postlaunch.XXXXXX)"
  if aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "${EXISTING_RUNTIME_ID}" --region "${AWS_REGION}" \
        --output json >"${POSTLAUNCH_JSON}" 2>/dev/null; then
    # Decide, independently, whether launch changed the authorizer and/or the env.
    #   authorizer: compare authorizerConfiguration ONLY (normalised, sorted-key JSON) — the
    #               sole carrier of inbound auth on the AgentRuntime API (no authorizerType
    #               field exists). A launch that drops the customJWTAuthorizer yields
    #               authorizerConfiguration == null POST vs the captured non-null PRE → fires
    #               the restore; an unchanged inbound compares equal → no-op (idempotent).
    #   env:        compare the environmentVariables maps (normalised, sorted-key JSON). A
    #               wiped/partial post-launch env (the common case) differs from the captured
    #               pre-launch one and triggers the restore.
    # We only treat each as "needs restore" if we actually captured it pre-launch (so we never
    # try to restore an authorizer/env that never existed).
    PRE_AUTH="$(jq -cS '.authorizerConfiguration // null' "${PRELAUNCH_JSON}" 2>/dev/null || echo "")"
    POST_AUTH="$(jq -cS '.authorizerConfiguration // null' "${POSTLAUNCH_JSON}" 2>/dev/null || echo "")"
    PRE_ENV="$(jq -cS '.environmentVariables // {}' "${PRELAUNCH_JSON}" 2>/dev/null || echo "")"
    POST_ENV="$(jq -cS '.environmentVariables // {}' "${POSTLAUNCH_JSON}" 2>/dev/null || echo "")"
    # LANGFUSE: what is DISABLE_ADOT_OBSERVABILITY on the runtime AFTER launch? If launch's --env
    # took, it is already "true"; if not (or it is unset/other), the restore below must assert it.
    POST_DISABLE_ADOT="$(jq -r '(.environmentVariables // {})["DISABLE_ADOT_OBSERVABILITY"] // ""' "${POSTLAUNCH_JSON}" 2>/dev/null || echo "")"
    # LANGFUSE (E26/T10): what are the NON-SECRET LANGFUSE_HOST / LANGFUSE_SECRET_NAME on the
    # runtime AFTER launch? If launch's --env took, they are already set; if not, the restore
    # below asserts them so agent.py can read its per-agent key from Secrets Manager on redeploy.
    POST_LF_HOST="$(jq -r '(.environmentVariables // {})["LANGFUSE_HOST"] // ""' "${POSTLAUNCH_JSON}" 2>/dev/null || echo "")"
    POST_LF_SECRET="$(jq -r '(.environmentVariables // {})["LANGFUSE_SECRET_NAME"] // ""' "${POSTLAUNCH_JSON}" 2>/dev/null || echo "")"

    AUTH_NEEDS_RESTORE=0
    ENV_NEEDS_RESTORE=0
    ADOT_NEEDS_SET=0
    LANGFUSE_NEEDS_SET=0
    if [ "${CAPTURED_HAS_AUTHORIZER}" = "1" ] && [ "${PRE_AUTH}" != "${POST_AUTH}" ]; then
      AUTH_NEEDS_RESTORE=1
    fi
    if [ "${CAPTURED_HAS_ENV}" = "1" ] && [ "${PRE_ENV}" != "${POST_ENV}" ]; then
      ENV_NEEDS_RESTORE=1
    fi
    # LANGFUSE: assert DISABLE_ADOT_OBSERVABILITY on the runtime unless it is already correct.
    # This is the idempotency guard: when DISABLE_ADOT is already the desired value AND launch
    # touched neither the authorizer nor the env, we skip a redundant full-replace below.
    if [ -n "${DISABLE_ADOT_OBSERVABILITY:-}" ] && [ "${POST_DISABLE_ADOT}" != "${DISABLE_ADOT_OBSERVABILITY}" ]; then
      ADOT_NEEDS_SET=1
    fi
    # LANGFUSE (E26/T10): assert LANGFUSE_HOST / LANGFUSE_SECRET_NAME too when they are configured
    # in the environment but not yet the desired value on the runtime (same idempotency logic as
    # DISABLE_ADOT). Empty env ⇒ never asserted (leave the runtime as-is; telemetry stays disabled).
    if { [ -n "${LANGFUSE_HOST:-}" ] && [ "${POST_LF_HOST}" != "${LANGFUSE_HOST}" ]; } \
       || { [ -n "${LANGFUSE_SECRET_NAME:-}" ] && [ "${POST_LF_SECRET}" != "${LANGFUSE_SECRET_NAME}" ]; }; then
      LANGFUSE_NEEDS_SET=1
    fi

    if [ "${AUTH_NEEDS_RESTORE}" = "0" ] && [ "${ENV_NEEDS_RESTORE}" = "0" ] && [ "${ADOT_NEEDS_SET}" = "0" ] && [ "${LANGFUSE_NEEDS_SET}" = "0" ]; then
      echo "Inbound authorizer, environment variables, DISABLE_ADOT_OBSERVABILITY, and Langfuse host/secret-name preserved by launch; no restore needed."
    else
      # Describe what we're restoring (breadcrumb — no secret values).
      RESTORE_WHAT=""
      [ "${AUTH_NEEDS_RESTORE}" = "1" ] && RESTORE_WHAT="inbound authorizer (customJWTAuthorizer / ${CAPTURED_AUTHORIZER_TYPE})"
      if [ "${ENV_NEEDS_RESTORE}" = "1" ]; then
        [ -n "${RESTORE_WHAT}" ] && RESTORE_WHAT="${RESTORE_WHAT} + "
        RESTORE_WHAT="${RESTORE_WHAT}environment variables [${CAPTURED_ENV_KEYS}]"
      fi
      if [ "${ADOT_NEEDS_SET}" = "1" ]; then
        [ -n "${RESTORE_WHAT}" ] && RESTORE_WHAT="${RESTORE_WHAT} + "
        RESTORE_WHAT="${RESTORE_WHAT}DISABLE_ADOT_OBSERVABILITY=${DISABLE_ADOT_OBSERVABILITY}"
      fi
      if [ "${LANGFUSE_NEEDS_SET}" = "1" ]; then
        [ -n "${RESTORE_WHAT}" ] && RESTORE_WHAT="${RESTORE_WHAT} + "
        # Breadcrumb only — the secret NAME/host are non-secret, but keep the log terse.
        RESTORE_WHAT="${RESTORE_WHAT}LANGFUSE_HOST/LANGFUSE_SECRET_NAME"
      fi
      echo "Restoring ${RESTORE_WHAT} after launch (launch reset/changed it)..."
      # Build ONE full-replace UpdateAgentRuntime input, MIRRORING the backend's
      # set_runtime_environment / _configure_runtime_authorizer replay (agent_identity_service.py):
      #   - required artifact/roleArn/networkConfiguration from the POST-launch GET (the NEW
      #     code — never revert the deploy);
      #   - authorizerConfiguration from the PRE-launch capture (preserve the gate). This is the
      #     SOLE inbound-auth field on the AgentRuntime API (no authorizerType — botocore 1.43.16;
      #     sending one would be rejected). When we did NOT capture an authorizer it is null in $q
      #     and we omit it, leaving launch's inbound as-is (nothing to preserve);
      #   - environmentVariables = MERGE: POST-launch env as the base, OVERLAID by the PRE-launch
      #     env so the backend-injected governance vars (CREDENTIAL_PROVIDER_NAME / MCP_GATEWAY_URL
      #     / MCP_AUDIENCE / AWS_REGION) survive the wipe. PRE WINS on key collisions because the
      #     governance vars are the ones at risk and `agentcore launch` itself sets NO app env;
      #     any env a launch legitimately introduced is post-only and is therefore KEPT, not
      #     clobbered (`(post // {}) * (pre // {})` is a recursive merge, right operand wins). We
      #     then FORCE-INJECT DISABLE_ADOT_OBSERVABILITY=$adot LAST (LANGFUSE): it must ALWAYS be on
      #     the merged env regardless of whether launch's --env landed it, and it never collides with
      #     the governance keys (distinct key), so this is additive — the MCP env is never clobbered.
      #   - the remaining optional fields from the POST-launch config ONLY if present (the runtime
      #     twin of the backend optional-field replay loop) — environmentVariables is NOT in this
      #     loop (handled explicitly above; sourcing it from POST here would re-introduce the wipe).
      RESTORE_JSON="$(mktemp -t agp-refagent-restore.XXXXXX)"
      # shellcheck disable=SC2016
      if jq -n \
            --slurpfile post "${POSTLAUNCH_JSON}" \
            --slurpfile pre "${PRELAUNCH_JSON}" \
            --arg rid "${EXISTING_RUNTIME_ID}" \
            --arg adot "${DISABLE_ADOT_OBSERVABILITY:-}" \
            --arg lfhost "${LANGFUSE_HOST:-}" \
            --arg lfsecret "${LANGFUSE_SECRET_NAME:-}" '
              ($post[0]) as $p
            | ($pre[0])  as $q
            | {
                agentRuntimeId:        $rid,
                agentRuntimeArtifact:  $p.agentRuntimeArtifact,
                roleArn:               $p.roleArn,
                networkConfiguration:  $p.networkConfiguration,
                environmentVariables:  (
                    (($p.environmentVariables // {}) * ($q.environmentVariables // {}))
                  + (if ($adot != "") then {DISABLE_ADOT_OBSERVABILITY: $adot} else {} end)
                  # LANGFUSE (E26/T10): force-inject the NON-SECRET host + secret NAME LAST, when
                  # configured, so they survive a launch full-replace on a redeploy. Distinct keys —
                  # never collide with the governance/MCP env. No secret VALUE is ever placed here.
                  # (No apostrophes in this jq comment — it lives inside a single-quoted bash string.)
                  + (if ($lfhost != "") then {LANGFUSE_HOST: $lfhost} else {} end)
                  + (if ($lfsecret != "") then {LANGFUSE_SECRET_NAME: $lfsecret} else {} end)
                )
              }
            # Preserve the captured authorizer ONLY if we captured one (pre had a non-null
            # authorizerConfiguration); otherwise omit so the launch inbound is left as-is. Emit
            # ONLY authorizerConfiguration — the UpdateAgentRuntime input shape has no authorizerType
            # member (botocore 1.43.16), so adding one would make the --cli-input-json invalid.
            # (NOTE: no apostrophes in this jq comment — it lives inside a single-quoted bash string.)
            + (if ($q.authorizerConfiguration != null)
               then {authorizerConfiguration: $q.authorizerConfiguration}
               else {} end)
            # Replay the optional fields from the POST-launch config ONLY if present, so we
            # never silently drop create-time settings. environmentVariables is intentionally
            # NOT here — it is merged explicitly above.
            + (reduce ([
                "protocolConfiguration","requestHeaderConfiguration",
                "lifecycleConfiguration","metadataConfiguration","filesystemConfigurations",
                "description"
              ][]) as $k ({};
                if ($p[$k] != null) then . + {($k): $p[$k]} else . end))
            ' >"${RESTORE_JSON}" 2>/dev/null \
         && [ -s "${RESTORE_JSON}" ]; then
        if aws bedrock-agentcore-control update-agent-runtime \
              --region "${AWS_REGION}" \
              --cli-input-json "file://${RESTORE_JSON}" >/dev/null 2>&1; then
          echo "Restored ${RESTORE_WHAT} replaying the post-launch (new-code) artifact."
        else
          echo "WARNING: UpdateAgentRuntime to RESTORE ${RESTORE_WHAT} FAILED. The runtime may be left with"
          echo "         launch's (possibly open) inbound and/or its env wiped — the agent may KeyError on"
          echo "         invoke. You MUST verify/restore the CUSTOM_JWT/Entra authorizer AND the env vars"
          echo "         manually (re-run platform provisioning / re-grant the agent, or SET_AUTHORIZER=1)."
        fi
      else
        echo "WARNING: failed to build the UpdateAgentRuntime restore input — the inbound authorizer and/or"
        echo "         environment variables were NOT restored. You MUST verify/restore the CUSTOM_JWT/Entra"
        echo "         authorizer AND the env vars manually (re-run platform provisioning / re-grant the agent)."
      fi
    fi
  else
    # We had a capture but cannot re-read post-launch — do NOT assume anything survived.
    echo "WARNING: could NOT re-read the runtime after launch to verify/restore the inbound authorizer or env."
    echo "         You MUST verify the inbound is still ${CAPTURED_AUTHORIZER_TYPE:-CUSTOM_JWT}/Entra and the"
    echo "         env vars are present, and restore them manually (re-run platform provisioning / re-grant"
    echo "         the agent, or SET_AUTHORIZER=1) if launch reset them."
  fi
fi
# Tempfile cleanup is handled by the EXIT trap set above.

# First-deploy gate reminder — only meaningful when there was nothing to preserve (no
# pre-existing CUSTOM_JWT inbound). On a redeploy of an already-gated runtime the
# preserve/restore above keeps it gated.
if [ "${RUNTIME_EXISTS}" != "1" ] || [ "${CAPTURE_OK}" != "1" ]; then
  echo "WARNING: this runtime is NOT gated by the platform Entra user tokens until the inbound CUSTOM_JWT authorizer is set (step 3 below — control-plane provisioning OR SET_AUTHORIZER=1). Do not route real user traffic to it before that step completes."
fi

# ---- 3. (optional) set the inbound Entra authorizer -----------------------
# Normally handled by our control-plane agent provisioning (the E6 UpdateAgentRuntime
# path). Enable only if setting it from here. (Orthogonal to the preserve-on-redeploy logic
# above: that PRESERVES an already-set authorizer across a code redeploy; this SETS one.)
if [ "${SET_AUTHORIZER}" = "1" ]; then
  : "${TENANT_ID:?SET_AUTHORIZER=1 requires TENANT_ID}"
  : "${AGENT_AUDIENCE:?SET_AUTHORIZER=1 requires AGENT_AUDIENCE the agent app audience}"
  : "${AGENT_RUNTIME_ID:?SET_AUTHORIZER=1 requires AGENT_RUNTIME_ID}"
  DISCOVERY="https://login.microsoftonline.com/${TENANT_ID}/v2.0/.well-known/openid-configuration"
  echo "=== Setting inbound CUSTOM_JWT authorizer (Entra) on the runtime ==="
  echo "NOTE: UpdateAgentRuntime is a full-replace PUT — replay agentRuntimeArtifact/roleArn/"
  echo "      networkConfiguration from GetAgentRuntime (the E6 path). See README.md."
  echo "Reading the current runtime config (GetAgentRuntime) to replay on update..."
  # The runtime's inbound auth is carried by authorizerConfiguration (its customJWTAuthorizer) —
  # there is NO authorizerType field on the AgentRuntime API (botocore 1.43.16). Project the real
  # fields you must replay on a full-replace UpdateAgentRuntime.
  aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${AGENT_RUNTIME_ID}" --region "${AWS_REGION}" \
    --query '{authorizerConfiguration:authorizerConfiguration,artifact:agentRuntimeArtifact,roleArn:roleArn,networkConfiguration:networkConfiguration}'
  echo "Apply the replay+update with the discovery URL ${DISCOVERY} and allowedAudience [${AGENT_AUDIENCE}]."
  echo "(Left as a documented step — the control-plane provisioning is the supported path.)"
fi

echo "=== Done. Next: ensure the runtime exec-role has the 3 token-util actions (see README),"
echo "    grant this agent the MCP in the UI, then invoke as an assigned user. ==="
echo "    agentcore invoke '{\"prompt\": \"list the available tools\"}'"
