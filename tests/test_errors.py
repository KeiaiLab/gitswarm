import pytest

from gitswarm.errors import (
    EXIT_CODES,
    REMOTE_DETAIL_MAX,
    Conflict,
    ErrorKind,
    GitswarmError,
    InvalidState,
    NotFound,
    RemoteError,
    Unsupported,
)


@pytest.mark.parametrize(
    ("exc", "kind", "code"),
    [
        (NotFound, ErrorKind.NOT_FOUND, 2),
        (Conflict, ErrorKind.CONFLICT, 3),
        (InvalidState, ErrorKind.INVALID_STATE, 4),
        (Unsupported, ErrorKind.UNSUPPORTED, 5),
        (RemoteError, ErrorKind.REMOTE_ERROR, 6),
    ],
)
def test_error_kind_and_exit_code(exc, kind, code):
    err = exc("detail")
    assert isinstance(err, GitswarmError)
    assert err.kind is kind
    assert EXIT_CODES[err.kind] == code
    assert err.to_payload() == {
        "ok": False,
        "error": {"kind": kind.value, "detail": "detail"},
    }


def test_remote_error_detail_is_capped():
    err = RemoteError("remote: " + "x" * 10 * REMOTE_DETAIL_MAX)
    assert len(err.detail) <= REMOTE_DETAIL_MAX + 1 and err.detail.startswith("remote: x")
    assert RemoteError("short").detail == "short"
