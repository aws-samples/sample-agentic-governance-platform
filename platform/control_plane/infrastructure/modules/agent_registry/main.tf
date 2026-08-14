# ============================================================================
# AWS Agent Registry (E32) — script-backed, NOT a native resource
# ============================================================================
# There is no Terraform resource for the `agent-registry` namespace. The provider's
# `aws_bedrockagentcore_registry` targets the OLD `bedrock-agentcore` namespace and ships
# DEPRECATED ("will continue to work until September 17, 2026"); AWS also blocks brand-new
# accounts from that namespace entirely, so it cannot serve a clean-from-zero deploy.
# Provider issue #48694 tracks a successor: no milestone, no assignee, no PR. There is no
# `data` source for the new namespace either. Hence a script-backed custom resource — this
# is the only available option, not a shortcut. Do NOT "fix" this module by reaching for
# `aws_bedrockagentcore_registry`.
#
# THIS MODULE PUBLISHES NO REGISTRY ID, AND THAT IS THE POINT
# -----------------------------------------------------------
# It used to. AWS mints the registryId (`RegistryIdentifier` accepts an ARN or a generated
# 12-16 char id — never a name), and `local-exec` has no channel to return a value, so the id
# was written to a gitignored capture file that this module read back with `fileexists`/`file`.
# Those functions resolve during the PLAN walk — BEFORE the provisioner that writes the file has
# run — so apply #1 on a fresh account necessarily planned from a file that did not exist yet:
# it baked `AGENT_REGISTRY_ID=""` into the ECS task definition and the CodeBuild env, and only
# apply #2 filled in the real ids. **A from-zero deploy needed `terraform apply` twice.**
#
# That is fixed by REMOVING the id from Terraform's concern entirely, not by cleverer plumbing.
# The registry NAME is a static tfvar known at plan time, and name -> id is a single
# `ListRegistries` call, so the BACKEND resolves it at first use and memoises it
# (`backend/src/core/registry_resolver.py`). CodeBuild gets the id the same way: the backend is
# the build's only trigger (`RuntimeBuildService` → `StartBuild`, the EventBridge trigger was
# deleted in E22/T7) and injects `AGENT_REGISTRY_ID` as a per-build env override.
#
# So: no capture file, no gitignored sidecar, no plan-time read, no `""`-tolerating consumers,
# and `terraform apply` is SINGLE-PASS from zero. This module's whole job is now the create
# side-effect — "make sure a registry with this name exists, and wait for it to be READY".
#
# This mirrors the GitHub OIDC provider and the shared push role in the root `main.tf`: objects
# that cannot be clean Terraform objects are bootstrapped by the platform instead of being
# half-modelled here.
#
# TWO RULES THIS MODULE MUST NEVER BREAK (both are why the PREVIOUS module was deleted):
#
#  1. NOTHING THIS MODULE PRODUCES MAY BE READ AT PLAN TIME. The first agent_registry module
#     round-tripped its ARN through a gitignored `.registry_arn` file read by
#     `data.local_file`, so `terraform plan` failed outright on every fresh clone / clean
#     worktree / CI checkout — the file was gitignored, therefore never present, therefore
#     the data source errored before any apply could create it. Its successor made that read
#     `fileexists`-guarded, which fixed the hard failure but kept the underlying shape, and
#     the shape is what cost the second apply. There is now NO output file to read at all,
#     which is the only version of this rule that cannot be re-broken. Do not reintroduce a
#     capture file: if something downstream seems to need the id, it can resolve it by name
#     the way the backend does.
#
#  2. NO DESTROY-TIME PROVISIONER. Deleting a registry deletes every record inside it —
#     every agent and every MCP server the platform has ever registered. The old module
#     carried a destroy-time provisioner that would have done exactly that; the root
#     stack's `removed { ... destroy = false }` history exists to keep it from ever
#     running. Registry deletion stays a deliberate human act, never a side effect of
#     `terraform destroy` or of a resource being replaced. There is deliberately no
#     `when = destroy` provisioner in this file, and none may be added.

# `terraform_data` (built-in since Terraform 1.4) rather than `null_resource`: same
# lifecycle hooks, no third-party provider dependency.
resource "terraform_data" "registry" {
  lifecycle {
    # CREATION REGION MUST EQUAL READ REGION. A registry lives in exactly one region, and the
    # region Terraform creates it in is not automatically the region the backend queries: the
    # root creates BOTH instances with `var.agent_registry_region` while the backend reads the
    # MCP registry with `var.mcp_registry_region`. Set those to different values and everything
    # applies cleanly, then every MCP registry call fails at RUNTIME with "registry not found"
    # — a silent, badly-signposted failure. Asserting equality here turns it into a plan-time
    # error naming both values. A `variable`-level `validation` cannot do this (it cannot see
    # another variable in the caller) and a `check` block only WARNS, so a resource
    # `precondition` is the right mechanism: it blocks the apply.
    precondition {
      condition     = var.region == var.read_region
      error_message = "Registry '${var.name}' would be CREATED in '${var.region}' but the backend will READ it from '${var.read_region}'. A registry exists in one region only, so this silently breaks every call to it at runtime. Set agent_registry_region and mcp_registry_region to the same value."
    }

    # THE PROVISIONER'S TWO FILES MUST EXIST BEFORE THE APPLY REACHES IT (E32/T9 finding F1).
    #
    # `python_bin` defaults to the backend venv, which is GITIGNORED (`backend/.gitignore` →
    # `venv/`, zero tracked files), so on a fresh clone the interpreter DOES NOT EXIST. Without
    # the guard below, apply #1 dies inside the `local-exec` on a bare "No such file or
    # directory" that names neither the venv, nor this module, nor the registry it was creating.
    #
    # That is rule (1)'s archetype — a dependency absent from a fresh checkout — relocated from
    # PLAN time to APPLY time, which is exactly why nothing else catches it: `terraform validate`
    # does not execute provisioners. These preconditions are evaluated during the PLAN walk (both
    # operands are known inputs, and `fileexists` resolves at plan time), so the failure moves to
    # the earliest point this module can fail at all: before any AWS call, before the registry
    # exists, with a diagnostic that names the cause and the fix. These two `fileexists` calls
    # are the ONLY ones left in this module, and they read TRACKED INPUTS (an interpreter and a
    # script), never anything this module produces — which is what keeps them clear of rule (1).
    #
    # `try(..., false)` is not decoration. `fileexists` RAISES on a directory ("... is a
    # directory, not a file"), which is reachable — a `python_bin` pointing at a venv root or at
    # `venv/bin` is an ordinary operator slip — and a raised function error inside a condition
    # replaces the message below with a generic evaluation failure. The charset `validation` in
    # variables.tf already excludes the shell-metacharacter paths, so `try` is left with exactly
    # the shape cases: absent, or present-but-not-a-file.
    precondition {
      condition     = try(fileexists(var.python_bin), false)
      error_message = "Python interpreter '${var.python_bin}' does not exist (or is not a file), resolved relative to the Terraform working directory (infrastructure/). Registry '${var.name}' is created by running the bootstrap script through it, and the default is the backend venv — which is GITIGNORED and therefore absent on a fresh clone. Create it before applying: `cd ../backend && python3 -m venv venv && venv/bin/pip install -r requirements.txt` (or run `../backend/run_dev.sh` once). See 'Prerequisites' in infrastructure/README.md. Only point python_bin elsewhere if that interpreter already has boto3."
    }

    # The script IS tracked in git, so this fires for a different reason than the interpreter:
    # a wrong RELATIVE path. Both defaults are relative to the Terraform working directory, so
    # running `terraform` from anywhere other than `infrastructure/` (or vendoring this module
    # into another root) silently repoints them — one line catches it in the same pass.
    precondition {
      condition     = try(fileexists(var.script_path), false)
      error_message = "Bootstrap script '${var.script_path}' does not exist (or is not a file), resolved relative to the Terraform working directory (infrastructure/). Registry '${var.name}' cannot be created without it. This path IS tracked in git (backend/scripts/ensure_registry.py), so it almost always means Terraform is running from a different working directory: run it from platform/control_plane/infrastructure, or pass an absolute script_path. See 'Prerequisites' in infrastructure/README.md."
    }
  }

  # Re-run only when the IDENTITY of the registry changes. The script is idempotent
  # (find-or-create by name, then wait for READY), so a re-run against an existing
  # registry is a no-op read — but there is no reason to pay for it on every apply.
  #
  # `name` and `region` are the WHOLE trigger set because they are the whole input set: the
  # registry's description is derived from its name by the script, which takes no other
  # arguments (see variables.tf on why there is no `description` variable).
  triggers_replace = {
    name   = var.name
    region = var.region
  }

  provisioner "local-exec" {
    # /bin/bash explicitly: `set -o pipefail` is a bashism, and local-exec otherwise runs
    # /bin/sh (dash on Debian/Ubuntu CI), which rejects pipefail at line 1.
    interpreter = ["/bin/bash", "-c"]

    # THE SCRIPT'S STDOUT IS NO LONGER CAPTURED — nothing reads the id from Terraform any
    # more, so `--json`, the temp file, the `mv` and the empty-payload check that guarded that
    # capture are all gone with it (see the header: the backend resolves the id by NAME). What
    # remains is the side effect: find-or-create the registry, wait for READY, exit non-zero on
    # failure. `set -euo pipefail` is what makes the script's exit status fail the APPLY — a
    # provisioner failure taints the resource, so the next apply retries rather than silently
    # recording a registry that was never created.
    #
    # Both fds are left attached so an operator sees the script's progress in the apply log
    # (all of its logging already goes to stderr; without `--json` its one stdout line is a
    # human-readable `registryId: ...`, which is now purely informational).
    #
    # The script needs no PYTHONPATH: it puts the backend's `src/` on `sys.path` itself,
    # precisely so this invocation does not have to set an env var (that is why `python_bin`
    # points at the backend venv — it needs boto3 and nothing else).
    command = <<-EOT
      set -euo pipefail

      '${var.python_bin}' '${var.script_path}' \
        --name '${var.name}' \
        --region '${var.region}'
    EOT
  }
}
