# THIS MODULE NO LONGER OUTPUTS A REGISTRY ID OR ARN, DELIBERATELY.
#
# It used to output both, and their long-lived contract — "EMPTY STRING, never null, until an
# apply has successfully run the provisioner" — existed only to keep a value nobody could know at
# plan time from breaking `plan`. AWS mints the registryId, `local-exec` cannot return a value, so
# the id round-tripped through a gitignored capture file that this module read back with
# `fileexists`/`file` during the PLAN walk — i.e. before the provisioner that writes it had run.
# Everything awkward here followed from that one fact:
#
#   * `terraform apply` had to run TWICE from zero. Apply #1 rendered `""` into the ECS task
#     definition and the CodeBuild env, then created the registries; apply #2 substituted the
#     real ids. (Measured, not assumed.)
#   * The outputs had to coalesce to `""` rather than `null`, because a `null` reaching
#     `modules/codebuild`'s REQUIRED `aws_codebuild_project` → `environment_variable.value` is
#     reported as `Error: Missing required argument` — naming CodeBuild, never mentioning a
#     registry — and failed a fresh clone's very first `plan`. That needed an explicit `== null`
#     ternary, because `try()` catches errors and not nulls (`try(null, "")` is `null`) and
#     `coalesce("", "")` ERRORS outright ("no non-null, non-empty arguments").
#   * Every consumer had to tolerate an empty id, so a stack wired to nothing rendered an inert
#     UI instead of failing — the failure mode that motivated removing all of this.
#   * `depends_on` on each output bought only nicer `terraform output` at the end of apply #1; it
#     could never reach consumers, whose values were frozen into the plan before the provisioner
#     ran.
#
# All of it is gone because THE ID IS NO LONGER TERRAFORM'S TO PUBLISH. The backend resolves it
# from the registry NAME — a static tfvar, known at plan time — on first use, and memoises it
# (`backend/src/core/registry_resolver.py`). CodeBuild receives it as a per-build env override
# from the backend, which is the build's only trigger. `terraform apply` is single-pass from zero.
#
# DO NOT RE-ADD `registry_id` / `registry_arn` HERE. Nothing can produce them at plan time: the
# one construct that would, `data "external"`, was rejected because it would call CreateRegistry
# during `terraform plan`, giving speculative plans real AWS side effects. So re-adding these
# outputs means re-adding the capture file, the guarded read, the `""` sentinel and the second
# apply, together. If a future consumer needs an id, resolve it BY NAME where it is needed — the
# same one `ListRegistries` call the backend makes.

output "registry_name" {
  description = "The registry name this module instance manages — the ONLY identifier it publishes, and the one the backend resolves to an id at runtime. Echoed from var.name so callers can label outputs without re-deriving it; always known, even before the first apply, which is precisely why the whole stack is keyed on the name rather than the id."
  value       = var.name
}
