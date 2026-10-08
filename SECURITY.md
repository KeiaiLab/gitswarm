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
  - `published_oid`: 40 lowercase hex
  - `created_at` (timezone required) and `ttl_s` (integer, bounded)
  - `--since` oid: 40 lowercase hex
  - file paths for `ws read` / `ws tree`: no absolute paths, no `..`
  - remote URLs (`--remote`, `$GITSWARM_REMOTE`, cwd discovery, MCP `remote`,
    `hive.toml`): only `ssh`, `git+ssh`, `https`, `http`, `file` URLs,
    scp-style `[user@]host:path` or absolute paths; user `[A-Za-z0-9._~+-]`,
    host `[A-Za-z0-9._-]` or a bracketed IPv6 literal (neither may start with
    `-`), digits-only port, no `%`; no leading `-`, control characters or
    `x::` transports. `git://` is refused: it carries no authentication and
    gives gitswarm no place to bound a stalled transfer. Credentials
    (userinfo) in http(s) URLs are refused
    (`https://user:token@host`, `https://token@host`) — use a credential
    helper; ssh login names (`ssh://git@host`, `git@host:path`) are fine.
    URLs in error messages and logs have their userinfo replaced by `***`. git also gets `--` before every
    URL and ref.
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
- GitHub: the App private key (PEM) is a full-installation secret; it can
  mint tokens for every repository the App is installed on. `from_spec`
  accepts only an RSA key, the PEM is held with `repr=False`, and it is
  never logged or put in errors. Keep the file mode `600` and install the
  App on the needed repositories only. The JWT is signed per call, valid
  540 s, and not cached.
- GitHub installation tokens last one hour and cannot be revoked by id.
  `drop` does not end access: a token issued for a dropped workspace works
  until it expires (the expiry is printed to stderr). Mitigate with
  repository-scoped installs and branch protection. `Token.secret` has
  `repr=False`; it is returned once by `ws create` and never stored.
- Per-workspace tokens are repository-scoped (`read:repository` or
  `write:repository`), returned once by `ws create`, and revoked on drop.
  Only the token id is stored in meta.
- SSH multiplexing: the control socket lives in `<hive>/.ssh-control/`
  (directory mode `700`). It is skipped when you set `GIT_SSH_COMMAND`,
  `GIT_SSH` or `core.sshCommand`.
- CI: the clone token is removed from `.git/config` before the test suite
  runs, because tests execute pull-request code (`.forgejo/ci/verify.sh`).

## Reporting a vulnerability

Open a private issue on the Forgejo repository, or contact the maintainers
via the repository. Do not post details in a public issue. Include the
version, the git version, and a reproduction.

## Supported versions

0.x: the latest release only.
