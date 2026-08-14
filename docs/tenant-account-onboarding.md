# Tenant and AWS account onboarding

A tenant is how the platform decides where an agent runs. This guide says what a tenant is, and what
you must have ready before you create one. It then covers the steps in the platform, and where the
support stops today. It is written for a platform admin who is onboarding one AWS account.

## What a tenant is

A tenant is a governed workspace. It is a line of business that owns agents, MCP servers and
projects. It maps to one or more identity-provider groups, and membership in any of those groups
makes a user a member of the tenant. It also names the AWS account each of its stages deploys into,
so a tenant's agents run in the tenant's own AWS account. Nothing deploys anywhere until a tenant
says where. A default install has one tenant, named `Default-Platform`, and one AWS account that acts as both
the platform account and the tenant account.

A tenant is typed by platform at creation, `aws` or `databricks`, and the type never changes
afterwards. This guide covers AWS tenants. A Databricks tenant names a workspace instead of an AWS
account and carries its own credentials and binding rules — see
[Databricks account onboarding](databricks-onboarding.md).

*Derived from `platform/control_plane/backend/src/models/tenant.py` and
`src/services/tenant_service.py`.*

## What you need first

Work through this checklist before you open the console. The platform creates none of these things
for you.

- **An AWS account.** The tenant's agents deploy into it. Each stage names its own account, and two
  stages may name the same one.
- **The deploy role, in that account.** A default single-account install already has one, created
  for you. For any other account, its owner creates it by hand. It has two hard requirements. Its
  name must begin with `agp-deployment-`, because the platform only permits its own roles to assume
  roles named that way. Its trust policy must admit exactly two principals: the platform's build
  role, which creates the agent's runtime, and the platform's backend task role, which deletes it.
  Read both ARNs from the platform's own deploy outputs. Do not assemble either ARN by hand. Run
  `terraform output -raw codebuild_role_arn` and `terraform output -raw backend_task_role_arn` in
  the platform's infrastructure directory. A role that admits only the build role still deploys.
  Every teardown in that account then fails, and the platform reports the failure rather than
  claiming it deleted anything. The deploy role also needs permission to create the agent's runtime
  and its execution role, to pull the image, and to delete that runtime and that execution role
  again. Derive it from the deploy role the platform creates for the default tenant.
- **A container-image repository, in that account.** A runtime pulls its image from it, and a
  promote that crosses accounts pushes into it. Note its address.
- **A CI image-push role, in that account.** You need it only if that stage's images are pushed from
  GitHub. Note its ARN. A Git connection can carry its own organization-wide push role. That role
  overrides this value, so a correct ARN here has no effect.
- **An identity-provider group.** Note its object id and add the tenant's members to it. See
  [Microsoft Entra ID setup](entra-setup.md). Nothing in the platform creates a group.
- **A platform admin.** Only the platform admin role can create or change a tenant. Members cannot
  even list tenants. They receive their own memberships instead.

Nothing verifies that any of this exists before the platform trusts it. It checks only the shape of
what you type. The first evidence of a wrong name, a wrong address, or a well-formed ARN naming a
role that is not there, is a failed deployment.

*Derived from `platform/control_plane/infrastructure/modules/{default_tenant, codebuild, agent_ecr}`
and `backend/src/services/graph_service.py`.*

## Create the tenant

The `Default-Platform` tenant comes from a one-off seed step, which you run while you bootstrap the
platform. That step writes data only and creates nothing in AWS, and re-running it leaves an
already-configured tenant untouched. So a fresh install already has its `Default-Platform` tenant,
and you use the steps below for every other tenant.

1. Sign in to the admin console as a platform admin.
2. Open the Tenants tab and start a new tenant.
3. Enter the name, the line of business, and an optional description. The name must be unique.
4. Find the groups box. Search the directory by group name. Click each group you want to link. It
   appears as a chip you can remove. Link at least one group.
5. Fill in both stages, `dev` and `prod`. Both are mandatory.
6. For each stage, enter the AWS account id, the region, the repository address, and the deploy
   role's ARN. Add the CI push role's ARN as well, if that stage pushes images from GitHub. The
   account id must have 12 digits. A deploy role ARN must be a whole IAM role ARN —
   `arn:aws:iam::<account-id>:role/<name>` — or blank; a bare role name is refused. The region
   defaults to `us-east-1`. The form marks the repository address and both ARNs optional. It accepts
   a stage that gives only an account id and a region. Fill them in anyway.
7. Save the tenant.
8. Register agents and projects into it. Each one names its tenant when you create it.

Every deployment from then on uses that stage's account, region, repository and deploy role.

*Derived from `platform/control_plane/frontend/src/components/governance/admin` and
`backend/scripts/seed_default_tenant.py`.*

## What the platform does — and what it does not

**What it does.**

- Stores the tenant, and uses it for every deployment.
- Resolves the tenant on the server, from the agent being deployed. A caller supplies only the
  stage, so nobody can aim a deployment at another tenant's account.
- Creates the agent's runtime, its execution role and its tracing secret inside the tenant account.
  It does this at deploy time, through the deploy role you built. One of each per agent per stage.
- Copies the agent's image into the tenant account when a promote crosses accounts.
- Deletes the agent's runtime and its execution role from the tenant account when you delete the
  agent or the project that owns it. It assumes the same deploy role to do it. When it cannot, it
  reports the step as failed and keeps the record, rather than reporting a deletion it did not make.
- Refuses to delete a tenant that agents, MCP servers or projects still reference.

**What it does not do yet.**

- Set up the tenant account for you. It creates none of the prerequisites above: not the deploy
  role, not the repository, not the push role. It creates no group in the identity provider either.
  A group lives in the identity provider, never in the AWS account.
- Check what you type, beyond the 12 digits of the account id and the shape of a deploy role ARN.
  Neither check asks AWS whether the thing exists.
- Offer a self-service flow. There is no request, no approval queue and no notification. A member
  who needs a tenant asks an admin.
- Onboard an account. The Tenants tab is a form over the tenant, not a workflow. It cannot create
  the deploy role, state the name and trust that role requires, or check that the role exists and is
  assumable.

*Derived from `platform/control_plane/backend/src/services/runtime_build_service.py`,
`src/api/routes/tenants.py` and `infrastructure/modules/agentcore_runtime`.*

## Warnings

- **A stage with no deploy role ARN deploys into the platform account.** The deployment does not
  fail, and nothing reports the swap. The stage still shows the account id you typed. Fill the ARN in
  for every stage that names another account.
- **The cross-account path has never been run against two real AWS accounts.** Treat a second
  account as an experiment. Be ready to clean it up by hand.
- **Deleting platform objects cleans up the tenant account only through the deploy role.** Deleting
  an agent or a project deletes its runtime and its execution role in that account, by assuming that
  stage's deploy role. When the platform cannot assume it — the usual cause is a trust policy that
  never admitted the backend task role — the step is reported as `failed`, with a reason that begins
  `assume_role_failed:`, and the record is kept so you can retry after fixing the trust. When the
  platform cannot work out which stage a resource belongs to, the reason begins `stage_unresolved:`
  instead. Either way nothing was removed and nothing claims otherwise: a step never reports
  `deleted` for something that survived. Fix the cause and delete again; remove by hand only what
  you deleted before this behaviour existed.
- **An update cannot remove a stage.** Stages merge, one stage at a time: a stage your update leaves
  out survives, and a stage you do send replaces the stored one whole, every field included. So
  clearing a role ARN means sending that stage with the ARN blank, and dropping a stage altogether
  means a direct write to the table.
- **The console edits exactly the two stages `dev` and `prod`.** Create any other stage set through
  the API. The Tenants table shows those two columns and prints an em dash for a stage a tenant does
  not have. Keep a stage named `dev` in every tenant. The platform reads that stage when it sets up
  a project's Git repository. A tenant without it fails that step.

*Derived from `platform/control_plane/backend/src/services/{project_service, agent_identity_service,
tenant_service, tenant_credentials}.py`.*

## Related

- [Microsoft Entra ID setup](entra-setup.md) — the identity provider, including the group a tenant
  needs before it can exist.
- [AWS service inventory](services.md) — which AWS services land in which account, and why.
- [Registering an agent or an MCP server](agentcore-registration.md) — what happens once a
  resource is registered into a tenant.
- [How an agent is deployed](agent-deployment.md) — the journey from a trunk merge to a running
  runtime, and why CI never touches prod.
