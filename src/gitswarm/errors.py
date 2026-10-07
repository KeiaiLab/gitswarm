"""오류 어휘 5종. CLI 종료코드·MCP 페이로드가 같은 enum 을 쓴다."""

from enum import StrEnum


class ErrorKind(StrEnum):
    NOT_FOUND = "NotFound"
    CONFLICT = "Conflict"
    INVALID_STATE = "InvalidState"
    UNSUPPORTED = "Unsupported"
    REMOTE_ERROR = "RemoteError"


EXIT_CODES: dict[ErrorKind, int] = {
    ErrorKind.NOT_FOUND: 2,
    ErrorKind.CONFLICT: 3,
    ErrorKind.INVALID_STATE: 4,
    ErrorKind.UNSUPPORTED: 5,
    ErrorKind.REMOTE_ERROR: 6,
}


class GitswarmError(Exception):
    kind: ErrorKind

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def to_payload(self) -> dict:
        return {"ok": False, "error": {"kind": self.kind.value, "detail": self.detail}}


class NotFound(GitswarmError):
    kind = ErrorKind.NOT_FOUND


class Conflict(GitswarmError):
    kind = ErrorKind.CONFLICT


class InvalidState(GitswarmError):
    kind = ErrorKind.INVALID_STATE


class Unsupported(GitswarmError):
    kind = ErrorKind.UNSUPPORTED


class RemoteError(GitswarmError):
    kind = ErrorKind.REMOTE_ERROR
