"""원격 호스팅이 git 프로토콜 밖에서 주는 기능의 계약. 없으면 Unsupported 를 '명시'한다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Capability(StrEnum):
    TOKEN = "token"


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Token:
    id: str
    secret: str
    scope: Scope


class RemoteAdapter(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token: ...

    def revoke_token(self, token_id: str) -> None: ...
