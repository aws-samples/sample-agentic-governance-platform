# How an agent is deployed

An agent lives in its own Git repository. When a developer merges agent code, GitHub builds the
container image. The platform — never GitHub — deploys it.

That split is the whole design. GitHub proves the code is good and names the image it built. The
platform decides where that image runs, in whose account, and under whose authority. The two halves
meet at exactly one request.

## The journey

1. **A developer merges to the trunk.** A merge to the trunk deploys; a pull request only runs the
   checks. There is one dev runtime per agent, so two branches would race to overwrite it.
2. **GitHub builds and pushes the image.** It lints, tests, builds the container, and pushes it to
   the platform's one shared agent registry. The image tag names the agent and the exact source tree
   it came from.
3. **GitHub tells the platform.** One request, carrying the agent, the image tag, the image's
   content digest, and the stage.
4. **The platform runs a deployment build.** One build project serves the whole platform. It reads
   the agent's registry record, assumes the deploy role in the tenant's AWS account, and creates or
   updates the agent's runtime there — the runtime, its execution role, and its inbound token check.
   It pins the image by its content digest rather than its movable tag, so the bytes that were tested
   are the bytes that run.
5. **The platform's records update by themselves.** The build writes the runtime's address, the image
   now serving each stage, and a deployment history row. Nothing calls back into the platform's API:
   the build is the only party that knows the deployment succeeded, so the build records it.

The platform trusts almost nothing in that request. It derives the target tenant from the agent
itself, so no caller can point a deployment at another tenant's account by lying. An agent on a
Databricks tenant has no runtime here to build at all, so the platform refuses that request outright
rather than deploying toward a platform this pipeline does not serve. It checks that the
calling repository owns the agent it named, and that the image tag belongs to that agent — one
registry is shared by every agent, so the tag is the only boundary between them. Deployment state is
kept per agent and per stage, so a production deployment cannot quietly mutate the dev runtime.

## Why GitHub can never deploy

GitHub holds no long-lived AWS key and no platform user's credential. It can ask for a short-lived,
identity-based token, and that token buys exactly two things: permission to push an image, and the
right to be recognised when it asks the platform for a deployment. Asking is not deploying.

The push permission is deliberately narrow. Each connected Git organization gets its own role, whose
trust names only that organization's repositories. That role may push image layers to the one shared
agent registry and nothing else. It cannot read a secret, start a build, assume a deploy role, or
even list what the registry already holds.

The token the platform sees carries no person and no platform role, so it cannot pass a single
[console gate](authorization-layers.md). The console's identity provider is not on this boundary.

## Production is a human decision

Continuous integration can only reach dev. A production deployment is refused at that boundary,
because there is nobody behind it to hold accountable.

Production is reached by two operator verbs, both on the repository, both restricted to a project
owner. A project with no owners has nobody accountable for a release, and "not yet governed" must not
mean "anyone in the tenant may ship to production".

- **Promote** ships the recorded candidate: the image a trunk merge registered. It accepts no image
  name at all. Promoting whatever last landed on dev could ship code nobody reviewed. So promote
  ships the exact image that the trunk merge built, pinned to the digest recorded then. Nothing
  merged since the last release means nothing to promote — an ordinary answer, not a fault.
- **Rollback** ships a named earlier image. It is accepted only if that image has a succeeded
  deployment for this repository, in this stage. Every part of that matters: the registry is shared,
  so another repository's tag names a real, pullable image; something proven in dev is not thereby
  approved for production; and a deployment that merely started, or failed, is no evidence anything
  ever served traffic.

Both refuse a second delivery while one is still in flight, so two deployments cannot race the same
agent. A rollback does not consume the candidate — a rollback is not an approval of the trunk, and
clearing it would put the eventual fix out of reach.

## When a deployment fails

Read the deployment build's log in CloudWatch. Every check the build makes, and every error it hits,
is written there. The platform's own record tells you whether the build started; only the log tells
you what happened after that.

## Worth knowing

- **Renaming an agent replaces its runtime.** A new name forces a replacement rather than an update,
  and a replaced runtime comes up carrying only what the deployment itself declares. So every agent
  that had MCP server grants comes back unable to reach its tools. It fails closed — no tools, no
  unauthorized reach — so this is a governance divergence rather than a security hole, and nothing in
  the deployment reports it. The platform now repairs it from the other side: the next time anything
  reads that agent's runtime status, a runtime found missing the tool wiring its record grants has
  that wiring re-applied. This is a repair on a governance read — the deployment path still ends at
  the runtime's address and never re-enters. Two cases it cannot cover: a runtime the platform cannot
  reach at all (one deployed into an account of its own), and the window before anyone looks.
- **The build instructions live in the platform, not in the agent's repository.** So do the
  deployment's infrastructure definitions. Changing either is a platform update, and it must be
  rolled out before the next push; a commit in the agent's repository cannot change how that agent is
  deployed. Until the rollout happens, deployments keep running the previous instructions.
- **Only dev deploys automatically.** There is no approval queue, no scheduled release, and no
  automatic promotion on green. Every production release needs a project owner at a console.
- **Recording the runtime's address is the one write that must not fail**, so the build treats it as
  terminal. A runtime that is live but recorded nowhere cannot be found, updated, or cleaned up.

## Related

- [Registering an agent or an MCP server](agentcore-registration.md) — the record, the lifecycle
  states, and everything registration set up before a runtime was needed.
- [Tenant and AWS account onboarding](tenant-account-onboarding.md) — the tenant, and the deploy role
  this path assumes in its account.
- [AWS service inventory](services.md) — which AWS services this path uses, and which account each
  one lands in.
