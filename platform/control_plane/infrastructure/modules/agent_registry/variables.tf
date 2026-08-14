variable "name" {
  description = "Registry name to find-or-create in the `agent-registry` namespace (e.g. `agp-agents` or `agp-mcp-servers`). This is the identity of the registry: the bootstrap script matches on it, so changing it provisions a DIFFERENT registry rather than renaming this one."
  type        = string

  validation {
    # The name is interpolated into a shell command below. Single quotes already stop
    # word-splitting, but refusing anything outside the AWS registry-name charset
    # removes shell metacharacters (including the single quote itself) from the value
    # entirely, so no quoting subtlety can turn a tfvar into command injection.
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$", var.name))
    error_message = "name must start alphanumeric and contain only letters, digits, '-' or '_' (max 63 chars)."
  }
}

variable "region" {
  description = "AWS region hosting the registry. Passed through to the script's --region; the registry lives in exactly one region and every backend call must target the same one."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{1,32}$", var.region))
    error_message = "region must be a lowercase AWS region code (letters, digits, hyphens)."
  }
}

variable "read_region" {
  description = "The region the BACKEND will use to read this registry at runtime (the root passes `var.agent_registry_region` for the agents instance and `var.mcp_registry_region` for the mcp instance). It must equal `region`: a registry lives in exactly one region, so a mismatch means Terraform creates it in one place and the backend looks for it in another. Asserted by a `precondition` in main.tf so the mismatch is a plan-time error, not a silent runtime 'registry not found'."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{1,32}$", var.read_region))
    error_message = "read_region must be a lowercase AWS region code (letters, digits, hyphens)."
  }
}

# There is deliberately NO `description` variable. The registry description is DERIVED FROM THE
# NAME by the script (`ensure_registry.py`: `_DESCRIPTION_TEMPLATE`), which exposes no
# `--description` flag "rather than adding a CLI flag the Terraform contract does not include" —
# its words. So a `description` input here could not reach the API even in principle, while
# reading at the call site as though it configured something. `description` is also create-only in
# the API (it never updates an existing registry), so there is nothing for Terraform to converge.

variable "python_bin" {
  description = "Python interpreter used to run the bootstrap script, relative to the Terraform working directory (`infrastructure/`). Defaults to the backend venv, which is where boto3 is installed — the script imports boto3 lazily and needs nothing else (no PYTHONPATH, no src/ on the path). That venv is GITIGNORED, so it does not exist on a fresh clone; its existence is asserted by a `precondition` in main.tf so a missing interpreter is a plan-time error naming the venv, not a bare 'No such file or directory' from inside the provisioner mid-apply."
  type        = string
  default     = "../backend/venv/bin/python"

  validation {
    # Interpolated into the same shell command as `var.name`, so it gets the same treatment:
    # restrict to the charset a filesystem path actually needs and no shell metacharacter can
    # survive — in particular the single quote (which would terminate the quoting), plus `$`,
    # backtick, `;`, `&`, `|`, `>`, whitespace and newline. Not reachable from the root today
    # (it never sets this), which is exactly why the guard belongs here rather than at the caller.
    condition     = can(regex("^[A-Za-z0-9._/-]{1,256}$", var.python_bin))
    error_message = "python_bin must be a plain path: letters, digits, '.', '_', '/', '-' only (max 256 chars, no shell metacharacters)."
  }
}

variable "script_path" {
  description = "Path to the find-or-create bootstrap script, relative to the Terraform working directory (`infrastructure/`). Its `--json` mode is a machine contract: exactly one line of JSON on stdout, all logging on stderr. Tracked in git, unlike `python_bin` — but the path is relative, so its existence is asserted by the same `precondition` in main.tf to catch a Terraform run started from a different working directory."
  type        = string
  default     = "../backend/scripts/ensure_registry.py"

  validation {
    # Same charset, same reason as `python_bin` above.
    condition     = can(regex("^[A-Za-z0-9._/-]{1,256}$", var.script_path))
    error_message = "script_path must be a plain path: letters, digits, '.', '_', '/', '-' only (max 256 chars, no shell metacharacters)."
  }
}
