"""Forgejo: workspace 범위 PAT. 자격은 파일에서 읽고 어디에도 다시 쓰지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import httpx

from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.config import RemoteSpec
from gitswarm.errors import RemoteError, Unsupported

TOKEN_NAME_PREFIX = "gitswarm-"
SCOPE_NAMES: dict[Scope, str] = {Scope.READ: "read:repository", Scope.WRITE: "write:repository"}
TIMEOUT_S = 10


@dataclass(frozen=True)
class ForgejoAdapter:
    api: str
    user: str
    credential: str = field(repr=False)
    client: httpx.Client | None = field(default=None, repr=False)

    @classmethod
    def from_spec(cls, spec: RemoteSpec) -> ForgejoAdapter:
        path = Path(spec.credential_file).expanduser()
        if not spec.credential_file or not path.is_file():
            raise Unsupported(f"forgejo credential file missing for {spec.api}")
        return cls(api=spec.api.rstrip("/"), user=spec.user, credential=path.read_text().strip())

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TOKEN})

    def _send(self, method: str, url: str, **kw: object) -> httpx.Response:
        # 주입된 client 는 호출자 소유라 닫지 않는다. 직접 만든 것만 닫는다.
        if self.client is not None:
            return self.client.request(method, url, **kw)
        auth = (self.user, self.credential)
        with httpx.Client(auth=auth, timeout=TIMEOUT_S) as http:
            return http.request(method, url, **kw)

    def _tokens_url(self) -> str:
        return f"{self.api}/api/v1/users/{self.user}/tokens"

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        body = {
            "name": TOKEN_NAME_PREFIX + ws_id,
            "scopes": [SCOPE_NAMES[scope]],
            "repositories": [repo],
        }
        r = self._send("POST", self._tokens_url(), json=body)
        if r.status_code != HTTPStatus.CREATED:
            raise RemoteError(f"forgejo token issue failed: HTTP {r.status_code}")
        j = r.json()
        return Token(id=str(j["id"]), secret=j["sha1"], scope=scope)

    def revoke_token(self, token_id: str) -> None:
        r = self._send("DELETE", f"{self._tokens_url()}/{token_id}")
        if r.status_code in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND):
            return
        raise RemoteError(f"forgejo token revoke failed: HTTP {r.status_code}")
