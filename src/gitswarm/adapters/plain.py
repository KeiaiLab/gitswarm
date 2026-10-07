"""임의 git URL. 호스팅 API 가 없으므로 전부 Unsupported."""

from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.errors import Unsupported


class PlainAdapter:
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        raise Unsupported("plain remote cannot issue tokens")

    def revoke_token(self, token_id: str) -> None:
        raise Unsupported("plain remote cannot revoke tokens")
