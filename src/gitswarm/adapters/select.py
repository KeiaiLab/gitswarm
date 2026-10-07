"""URL 의 host 로 어댑터를 고른다. 미선언 = plain."""

from __future__ import annotations

from urllib.parse import urlparse

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.remote import RemoteAdapter
from gitswarm.config import Config

FORGEJO = "forgejo"
GITHUB = "github"
SCP_SEP = ":"


def _split(url: str) -> tuple[str, str]:
    """(host, path). scp 꼴 git@host:org/repo 도 받는다."""
    if "://" in url:
        u = urlparse(url)
        return u.hostname or "", u.path
    user_host, sep, path = url.partition(SCP_SEP)
    if not sep:
        return "", url
    return user_host.rpartition("@")[2], path


def host_of(url: str) -> str:
    return _split(url)[0]


def repo_name(url: str) -> str:
    path = _split(url)[1].strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return "/".join(path.split("/")[-2:])


def adapter_for(url: str, config: Config) -> RemoteAdapter:
    spec = config.remotes.get(host_of(url))
    if spec is not None and spec.adapter == GITHUB:
        from gitswarm.adapters.github import GitHubAdapter

        return GitHubAdapter.from_spec(spec)

    if spec is None or spec.adapter != FORGEJO:
        return PlainAdapter()

    from gitswarm.adapters.forgejo import ForgejoAdapter  # Task 11

    return ForgejoAdapter.from_spec(spec)
