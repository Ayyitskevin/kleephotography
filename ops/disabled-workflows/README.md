# Disabled workflows

`opencode.yml.disabled` is retained for history but deliberately not loaded by
GitHub Actions. The pinned action still downloads and executes the mutable
`https://opencode.ai/install` script, so pinning the action commit does not pin
the code that runs with repository OIDC and API credentials.

Do not restore the `.yml` extension until the installer artifact is versioned and
checksum-verified (or the action is replaced with a workflow whose full execution
chain is immutable).
