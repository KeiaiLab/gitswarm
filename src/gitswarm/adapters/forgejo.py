"""Forgejo: workspace 범위 PAT. 자격은 파일에서 읽고 어디에도 다시 쓰지 않는다.

`repositories` 는 `RepoTargetOption` 객체(`{"owner", "name"}`) 배열이다. 이 모양은
Forgejo 16.0.5 swagger 로 검증했다(문자열 `"org/repo"` 는 HTTP 422).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import httpx

from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.config import RemoteSpec
from gitswarm.constants import TOKEN_ID_RE
from gitswarm.errors import InvalidState, RemoteError, Unsupported

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

    def _send(self, op: str, method: str, url: str, **kw: object) -> httpx.Response:
        # 매 요청에 자격을 싣는다(주입 client 에 auth 가 없어도 동일). 주입 client 는 닫지 않는다.
        auth = (self.user, self.credential)
        try:
            if self.client is not None:
                return self.client.request(method, url, auth=auth, **kw)
            with httpx.Client(timeout=TIMEOUT_S) as http:
                return http.request(method, url, auth=auth, **kw)
        except httpx.HTTPError as e:
            # 메시지에 URL·자격을 싣지 않는다: 실패 계열 이름만.
            raise RemoteError(f"forgejo {op} failed: {type(e).__name__}") from None

    def _tokens_url(self) -> str:
        return f"{self.api}/api/v1/users/{self.user}/tokens"

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        owner, sep, name = repo.rpartition("/")
        if not sep or not owner or not name:
            raise RemoteError("forgejo token issue failed: repo must be owner/name")

        body = {
            "name": TOKEN_NAME_PREFIX + ws_id,
            "scopes": [SCOPE_NAMES[scope]],
            "repositories": [{"owner": owner, "name": name}],
        }
        r = self._send("token issue", "POST", self._tokens_url(), json=body)
        if r.status_code != HTTPStatus.CREATED:
            raise RemoteError(f"forgejo token issue failed: HTTP {r.status_code}")
        try:
            j = r.json()
            return Token(id=str(j["id"]), secret=j["sha1"], scope=scope)
        except (KeyError, TypeError, ValueError):
            raise RemoteError("forgejo token issue failed: malformed response") from None

    def revoke_token(self, token_id: str) -> None:
        if not TOKEN_ID_RE.fullmatch(token_id):
            raise InvalidState(f"invalid token id {token_id!r}")
        r = self._send("token revoke", "DELETE", f"{self._tokens_url()}/{token_id}")
        if r.status_code in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND):
            return
        raise RemoteError(f"forgejo token revoke failed: HTTP {r.status_code}")
