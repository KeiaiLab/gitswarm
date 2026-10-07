# Forgejo: per-workspace tokens

With the `forgejo` adapter, `ws create` mints a personal access token
limited to one repository and to `write:repository`, returns it once in
`token`, stores only its id (`token_id`), and deletes it on `ws drop`.

## 1. A bot account

The adapter calls `POST /api/v1/users/<user>/tokens` with basic
authentication, so the credential it holds is a secret for the whole
account. Create an account that exists only for this:

- member of the target organization,
- write access to the target repositories only,
- no admin rights, no other memberships.

The scope to one repository comes from the `repositories` field of the
token request, sent as `[{"owner": …, "name": …}]` (the shape Forgejo 16
accepts). Use a Forgejo version that supports repository-scoped tokens;
gitswarm does not check that the server honoured the field.

## 2. The credential file

Put the bot's password in a file readable only by you:

```sh
mkdir -p ~/.config/gitswarm
install -m 600 /dev/null ~/.config/gitswarm/forgejo.cred
$EDITOR ~/.config/gitswarm/forgejo.cred
```

gitswarm reads the file on each command, keeps the value out of `repr`,
logs and error messages, and never writes it anywhere.

## 3. config.toml

```toml
[remote."git.example.com"]
adapter = "forgejo"
api = "https://git.example.com"
user = "gitswarm-bot"
credential_file = "~/.config/gitswarm/forgejo.cred"
```

The table key is the host of the remote URL. `gitswarm doctor` fails the
`config` check if `credential_file` is missing.

## 4. Use and revocation

- `ws create` → `token` (secret, once) and `token_id` in the record. The
  token name is `gitswarm-<id>`.
- `ws drop` deletes the token. If deletion fails (network, permission),
  the drop still completes, a line goes to stderr, and `token_id` stays in
  the record. `gitswarm stats` counts them (`unrevoked_tokens`),
  `gitswarm doctor` fails the `tokens` check, and running `ws drop <id>`
  again retries; success writes a `ws.revoked` event.
