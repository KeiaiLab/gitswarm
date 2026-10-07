# GitHub: App installation tokens

GitHub cannot create personal access tokens through its API. The only
repository-scoped credential gitswarm can mint is a GitHub App
installation token. With the `github` adapter, `ws create` returns one,
limited to the remote's repository and to `contents: write`.

## 1. Create the App

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App
(for an organization: the organization's Settings → Developer settings).

- Name and homepage URL: anything.
- Webhook: uncheck "Active".
- Repository permissions → Contents: **Read and write**. Nothing else is
  needed (Metadata: read-only is added automatically).
- Where can this App be installed: Only on this account.

Create it. The App's settings page shows the **App ID**.

## 2. Private key

On the same page, "Generate a private key". GitHub downloads a `.pem`.
It can mint tokens for every repository the App is installed on: keep it
mode `600`.

```sh
install -m 600 ~/Downloads/<app>.*.private-key.pem ~/.config/gitswarm/github-app.pem
```

gitswarm accepts only an RSA key and signs a fresh JWT (valid 540 s) per
call; it does not cache it.

## 3. Install

"Install App" → choose the account → "Only select repositories" → pick
the repositories agents will use. After installing, the browser is on
`https://github.com/settings/installations/<installation_id>` (for an
organization: `https://github.com/organizations/<org>/settings/installations/<installation_id>`).
That number is the **installation ID**.

## 4. config.toml

```toml
[remote."github.com"]
adapter = "github"
api = "https://api.github.com"
user = "<app_id>/<installation_id>"     # e.g. "123456/78901234"
credential_file = "~/.config/gitswarm/github-app.pem"
```

`user` is reused to carry the two ids. Both must be positive integers.

## Limits

- Tokens last one hour. A workspace that lives longer needs a new
  credential for further pushes; gitswarm does not refresh it.
- Tokens cannot be revoked by id (GitHub revokes only the token presented,
  and gitswarm does not keep it). `ws drop` prints the expiry time to
  stderr; the token keeps working until then. Narrow the exposure with a
  selected-repositories install and branch protection.
- git uses the token as the password with user `x-access-token`. Pass it
  through a credential helper, not the URL.
