"""GitHub: App installation token. 레포 한정, 1시간 유효, id 로 폐기 불가.

PAT 는 API 로 발급할 수 없어 레포 범위 원시값은 installation token 뿐이다.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import httpx
import jwt

from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.config import RemoteSpec
from gitswarm.constants import TOKEN_ID_RE
from gitswarm.errors import InvalidState, RemoteError, Unsupported

DEFAULT_API = "https://api.github.com"
PERMISSIONS: dict[Scope, str] = {Scope.READ: "read", Scope.WRITE: "write"}
TIMEOUT_S = 10
JWT_BACKDATE_S = 60  # 시계 오차 흡수
JWT_TTL_S = 600  # GitHub 상한
USER_SEP = "/"
USER_SHAPE = "user must be '<app_id>/<installation_id>'"


@dataclass(frozen=True)
class GitHubAdapter:
    api: str
    app_id: int
    installation_id: int
    private_key: str = field(repr=False)
    client: httpx.Client | None = field(default=None, repr=False)

    @classmethod
    def from_spec(cls, spec: RemoteSpec) -> GitHubAdapter:
        # RemoteSpec.user 를 "<app_id>/<installation_id>" 로 재사용한다.
        ids = spec.user.split(USER_SEP)
        if len(ids) != 2 or not all(i.isdecimal() for i in ids):
            raise Unsupported(f"github {USER_SHAPE}")

        path = Path(spec.credential_file).expanduser()
        if not spec.credential_file or not path.is_file():
            raise Unsupported("github app pem file missing")

        return cls(
            api=(spec.api or DEFAULT_API).rstrip("/"),
            app_id=int(ids[0]),
            installation_id=int(ids[1]),
            private_key=path.read_text(),
        )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TOKEN})

    def _jwt(self) -> str:
        # 호출마다 새로 만든다. 캐시하지 않는다.
        now = int(time.time())
        claims = {"iat": now - JWT_BACKDATE_S, "exp": now + JWT_TTL_S, "iss": str(self.app_id)}
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def _send(self, url: str, body: dict[str, object]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._jwt()}"}
        try:
            if self.client is not None:
                return self.client.post(url, headers=headers, json=body)
            with httpx.Client(timeout=TIMEOUT_S) as http:
                return http.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            # 메시지에 URL·자격을 싣지 않는다: 실패 계열 이름만.
            raise RemoteError(f"github token issue failed: {type(e).__name__}") from None

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        # ws_id 는 보내지 않는다: installation token 은 이름이 없다.
        url = f"{self.api}/app/installations/{self.installation_id}/access_tokens"
        body = {
            "repositories": [repo.rpartition("/")[2]],
            "permissions": {"contents": PERMISSIONS[scope]},
        }
        r = self._send(url, body)
        if r.status_code != HTTPStatus.CREATED:
            raise RemoteError(f"github token issue failed: HTTP {r.status_code}")
        try:
            j = r.json()
            expires = datetime.fromisoformat(j["expires_at"])
            return Token(id=str(int(expires.timestamp())), secret=j["token"], scope=scope)
        except (KeyError, TypeError, ValueError):
            raise RemoteError("github token issue failed: malformed response") from None

    def revoke_token(self, token_id: str) -> None:
        """문서화된 no-op. 폐기 API 는 제시한 토큰만 폐기하는데 비밀은 보관하지 않는다.

        Unsupported 가 아니다: 서비스는 Unsupported 를 "토큰 없는 어댑터"로 읽는다.
        토큰은 id(만료 epoch)에 적힌 시각에 스스로 만료된다.
        """
        if not TOKEN_ID_RE.fullmatch(token_id):
            raise InvalidState(f"invalid token id {token_id!r}")
        at = datetime.fromtimestamp(int(token_id), UTC).isoformat()
        print(f"gitswarm: github token expires at {at}; cannot revoke by id", file=sys.stderr)
