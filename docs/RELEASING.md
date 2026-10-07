# Releasing

Version lives in one place: `[project].version` in `pyproject.toml`.

## 1. Prepare

1. Bump `version` in `pyproject.toml`.
2. Move the `Unreleased` entries in `CHANGELOG.md` under `## X.Y.Z`.
3. Land via the usual path (`land-direct`). CI runs the gate, including
   the sdist/wheel content checks.

## 2. Tag

Tag the landed `stable` sha, not a local commit.

```sh
git fetch origin stable
git tag -a vX.Y.Z -m "gitswarm X.Y.Z" origin/stable
git push origin vX.Y.Z
```

The push hook guards branch refspecs only; tag pushes pass.

## 3. Build

```sh
git switch --detach vX.Y.Z
rm -rf dist && uv build
```

Produces `dist/gitswarm-X.Y.Z-py3-none-any.whl` and
`dist/gitswarm-X.Y.Z.tar.gz`.

## 4. Forgejo release

Body = the `## X.Y.Z` section of `CHANGELOG.md`. Token: maintainer PAT
(`~/.config/keiailab/forgejo-admin.token`), never committed.

```sh
API=https://git.keiailab.com/api/v1/repos/keiailab-oss/gitswarm
TOKEN=$(cat ~/.config/keiailab/forgejo-admin.token)
BODY=$(awk '/^## X.Y.Z/{f=1;next} /^## /{f=0} f' CHANGELOG.md)

ID=$(jq -n --arg b "$BODY" '{tag_name:"vX.Y.Z",name:"vX.Y.Z",body:$b}' \
  | curl -fsS -H "Authorization: token $TOKEN" -H 'Content-Type: application/json' \
      -d @- "$API/releases" | jq -r .id)

for f in dist/*.whl dist/*.tar.gz; do
  curl -fsS -H "Authorization: token $TOKEN" \
    -F "attachment=@$f" "$API/releases/$ID/assets?name=$(basename "$f")"
done
```

## 5. PyPI

The upload token comes from the maintainer at publish time. Never store
it in the repo or shell history files.

Rehearse on TestPyPI first:

```sh
UV_PUBLISH_TOKEN=<testpypi-token> \
  uv publish --publish-url https://test.pypi.org/legacy/ dist/*
uvx --index https://test.pypi.org/simple/ --from gitswarm==X.Y.Z gitswarm --help
```

Then the real index:

```sh
UV_PUBLISH_TOKEN=<pypi-token> uv publish dist/*
```

PyPI versions are immutable; a bad release needs a new version.

## Until PyPI exists

Install straight from git:

```sh
uvx --from git+https://git.keiailab.com/keiailab-oss/gitswarm gitswarm --help
uvx --from git+https://git.keiailab.com/keiailab-oss/gitswarm@vX.Y.Z gitswarm mcp
```
