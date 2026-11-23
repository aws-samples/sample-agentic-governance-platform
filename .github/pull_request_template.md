## What changed and why

<!-- One paragraph. Link the issue or design document this comes from. -->

Closes #

## Gates

Run every gate your change can affect and paste the real output — numbers, not "tests pass". The
gates are the test suites and the lint scripts under `scripts/lint/`, which
`pre-commit run --all-files` runs together; the commands and their expected baselines are in the
[README's Development section](../README.md#development).

- [ ] Backend suite
- [ ] Frontend types, tests, build, lint, audit
- [ ] Terraform fmt and validate
- [ ] Not applicable to this change (explain below)

```
paste output here
```

## Invariants

- [ ] I read [the non-negotiable invariants](../platform/control_plane/backend/README.md#invariants-a-change-must-not-break)
      and this change breaks none of them.
- [ ] Affected READMEs and setup docs are updated in this PR.
- [ ] No secrets, AWS account ids, or live identifiers are added anywhere, including examples.

## Notes for the reviewer

<!-- Anything you deliberately did not do, any invariant this comes near, any number that moved. -->
