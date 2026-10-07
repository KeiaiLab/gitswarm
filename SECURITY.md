# Security

## Threat model

gitswarm stores its state in a git remote and runs `git` against it.

- **Who can write state.** Anyone with push rights to
  `refs/heads/gitswarm/*` on the remote. That includes the meta branch
  (`gitswarm/meta`), which holds one JSON record per workspace. Treat
  remote meta as untrusted input.
- **What gitswarm validates** before a value reaches git or HTTP, and
  fails closed (`InvalidState` / `NotFound`) otherwise:
  - workspace `id` and `parent`: ULID alphabet and length; the record's `id`
    must match its path
  - `branch`: must equal the branch recomputed from `id`, so a forged record
    cannot aim a delete at another ref
  - `token_id`: digits only (it is placed in a URL path)
  - `created_at` (timezone required) and `ttl_s` (integer, bounded)
  - `--since` oid: 40 lowercase hex
  - file paths for `ws read` / `ws tree`: no absolute paths, no `..`
- **Deletes are leased.** Branch deletion is conditional on the last oid this
  host saw. `gc` never deletes a commit this host has not seen.
- **Hooks are respected.** A server-side hook decline is a hard error, never
  retried or bypassed.

## What gitswarm does not promise

- **Per-agent isolation.** A workspace is not bound to the agent that
  created it. Anyone who can push the namespace can publish or drop any
  workspace and rewrite any record. The boundary is the remote's push ACL.
  Narrow it with repository-scoped tokens or branch protection on
  `gitswarm/*`.
- **Confidentiality of workspace contents** beyond what the remote enforces.
- **Integrity of event sinks.** Webhooks and JSONL files are sent once, with
  no retry and no signature.

## Credentials

- The Forgejo credential file referenced by `config.toml` is a full-account
  secret (it mints tokens for the account). Use a dedicated bot account with
  no other access. Keep the file mode `600`.
- The credential is read from the file, kept with `repr=False`, and never
  logged or placed in error messages. Failures report the operation and the
  status code or exception class only.
- Per-workspace tokens are repository-scoped (`read:repository` or
  `write:repository`), returned once by `ws create`, and revoked on drop.
  Only the token id is stored in meta.
- CI: the clone token is removed from `.git/config` before the test suite
  runs, because tests execute pull-request code (`.forgejo/ci/verify.sh`).

## Reporting a vulnerability

Open a private issue on the Forgejo repository, or contact the maintainers
via the repository. Do not post details in a public issue. Include the
version, the git version, and a reproduction.

## Supported versions

0.x: the latest release only.
