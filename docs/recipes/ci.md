# CI: ignore gitswarm branches

gitswarm writes ordinary branches: `gitswarm/meta` on every state change
and `gitswarm/ws/<id>` for each workspace. A CI that runs on every push
starts a run for each create, publish, drop and meta write. Exclude them.

## Forgejo Actions / GitHub Actions

Same syntax on both:

```yaml
on:
  push:
    branches-ignore:
      - "gitswarm/**"
```

If the workflow already uses an allowlist (`branches: [main]`), gitswarm
branches are not matched and nothing changes. `branches` and
`branches-ignore` cannot be combined for one event.

To test workspace results, trigger on the result you land (a pull request
or a merge to your default branch), not on the workspace branch.

## Other systems

Match the same pattern, `gitswarm/**` (refs `refs/heads/gitswarm/*`), in
your CI's branch filter.
