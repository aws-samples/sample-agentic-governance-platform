# Cedar tool policies

A tool policy is one allow-or-deny rule for one tool call. The gateway evaluates it, in AWS, on every
call an agent makes. That makes it the platform's finest-grained gate. Every other gate — a role, a
group, a consent grant, a gateway's inbound token check — is decided once per token, and then serves
every tool call on the connection it opened. The chain that delivers the token to the gateway is in
[token propagation](token-propagation.md#9-the-gateway-and-cedar-per-tool-call).

## What a policy says

A policy names three things.

- **Who.** One user, by object id in the identity provider. Microsoft Entra ID is the provider
  supported today. The gateway matches that id against a tag on the calling principal.
- **Which tool.** One tool on one gateway, under its namespaced name — the target name, three
  underscores, then the tool name, exactly as the gateway's tool scan produced it. A policy may also
  cover every tool on the gateway.
- **Under what conditions.** Zero or more tests on the tool call's own arguments. The parameters come
  from the tool's own schema, not from a name you type, and only its top-level ones. A number compares
  six ways, in whole numbers only; a string equals or not-equals; anything else cannot be tested at
  all. Conditions need a specific tool: the every-tool form has no single schema, so the console and
  the API refuse that pairing. The refusal is a short fixed message, and nothing is logged about why.

Here is a policy that lets one user call one tool, and only for amounts under a thousand.

```
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"payments___transfer",
  resource == AgentCore::Gateway::"<gatewayArn>"
)
when { principal.hasTag("oid") && principal.getTag("oid") == "<object id>"
       && context.input has amount && context.input.amount < 1000 };
```

A deny is the same shape with `forbid` in place of `permit`. The console says allow and deny; the
Cedar text says permit and forbid.

There is no all-users allow. The one policy that may leave the user out is a deny with conditions,
which blocks that tool for everybody whenever an argument looks a certain way. And no policy can reach
a value nested inside an object or an array, mention time, count anything, compare one call against
another, or name the agent. The principal is always a user.

## Where policies live

In one place: an AgentCore Policy Engine, an AWS resource, one per gateway. The platform keeps no
copy. Its own record holds three handles and no policy text — the engine's id, the engine's ARN, and
the gateway's enforcement mode. The console lists policies by reading them back out of the
engine. Which system owns which fact is in
[the data model](data-model.md#15-the-agentcore-policy-engine--cedar-text).

A policy that someone wrote into the engine by hand still appears, marked unmanaged. The console
cannot summarise it. The gateway evaluates it exactly like the ones the platform wrote.

## Enforcement, and the first policy

A gateway sits in one of three postures. **Off** is the default: no engine attached, and no per-tool
authorization at all. **Log only** attaches the engine and blocks nothing — whatever it records is
read in AWS, because nothing in the platform reads or stores an evaluation. **Enforce** blocks.

> **Adding the first policy switches that gateway to enforce, and Cedar denies by default.** So the
> first *let this one user call this one tool* click blocks every other user and every other tool on
> that gateway, for every agent connected to it. Only a matching allow policy lets a call through.
> This is intended, it is the most surprising thing here, and the console asks you to confirm it.

Deleting a policy is not the same as turning enforcement off. Engine, attachment and mode all survive,
so a gateway that reached enforce and then lost its last policy denies everything. Turning enforcement
off detaches the engine instead; nothing is deleted, and re-attaching restores every policy.

## What a deny looks like

Nothing user-facing. The gateway refuses the call, and the refusal reaches the agent as a tool error
inside its reasoning loop — the model retries, apologises, or picks another tool. Nothing in the
platform turns a deny into a governance message: no reason string, no policy id, no blocked screen.

The console warns you in advance instead. On an enforcing gateway the Policies tab carries a standing
note beside the posture: every call not covered by a policy is denied, for every agent connected to
that gateway, and the deny is not recorded there. Off and log only stay quiet, because neither
blocks. So the console tells you a deny is possible and why, never that one happened. You still
diagnose the individual call by reading, in this order.

1. The gateway's enforcement mode, on the Policies tab of its detail page. Enforce, with no policy
   covering the call, is the whole answer — and the standing note there says so.
2. The policy list on that same tab, unmanaged rows included.
3. The tool name. A policy written against the bare name never matches; use the namespaced one.
4. The identity. A call arriving without a user-bearing token carries no object id, so it matches no
   policy and is denied.

## Limits, and what is not proven

- **The user match is design intent.** The policy matches a tag on the principal, which AgentCore must
  fill in from the token, and nothing here demonstrates that it does. One live test settles it: on an
  enforcing gateway, allow one named user on one tool; that user should succeed, and a second user with
  the same grants should fail.
- **Policies cover gateway-fronted tools, and nothing else.** An MCP server of any other kind has no
  engine, no policy list and no per-call gate. The console drops the tab for it.

## Related

- [Token propagation](token-propagation.md#9-the-gateway-and-cedar-per-tool-call) — which token
  reaches the gateway, and why it must still carry a user.
- [Registering an agent or an MCP server](agentcore-registration.md#7-mcp-servers) — how a gateway
  becomes a governed MCP server, and where its tool list comes from.
- [Data model](data-model.md#15-the-agentcore-policy-engine--cedar-text) — why the Policy Engine is
  the only copy of a policy.
