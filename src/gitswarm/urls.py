"""원격 URL 허용 목록. git 에 넘기기 전에 모든 진입점이 여기를 지난다.

git 은 원격 자리의 값을 옵션(`--upload-pack=…`)이나 명령 실행 transport(`ext::…`)로도
읽는다 — 외부에서 온 URL 은 모양을 좁혀 받는다. 프로세스를 띄우지 않는다.

    scheme://…      ssh · git+ssh · https · http · git · file
    [user@]host:path  scp 꼴(host 에 `/` 없음, path 비지 않음)
    /abs/path       로컬 절대 경로
"""

from __future__ import annotations

import re

from gitswarm.errors import InvalidState

ALLOWED_SCHEMES = frozenset({"ssh", "git+ssh", "https", "http", "git", "file"})
SSH_SCHEMES = frozenset({"ssh", "git+ssh"})
LOCAL_SCHEME = "file"  # authority 가 비어도 되는 유일한 scheme(file:///abs)
SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.DOTALL)
TRANSPORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")  # ext::, fd:: …

# host 검사를 ssh 에 맡기지 않는다(옵션 모양 host 거절은 OpenSSH 9.6+ 에만 있다) —
# 글자 집합을 좁히고 `-` 로 시작하지 못하게 한다. `%`·`:`·`;`·공백은 어디에도 없다.
USER = r"[A-Za-z0-9._~+][A-Za-z0-9._~+-]*"
HOST = r"(?:[A-Za-z0-9._][A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])"
AUTHORITY_RE = re.compile(rf"^(?:{USER}@)?{HOST}(?::[0-9]+)?$")
SCP_RE = re.compile(rf"^(?:{USER}@)?({HOST}):(.+)$", re.DOTALL)
OPTION_MARK = "-"
DEL = 0x7F
FIRST_PRINTABLE = 0x20


def _refuse(url: object) -> InvalidState:
    return InvalidState(f"invalid remote url: {url!r}")


def _printable_utf8(url: str) -> bool:
    """제어 문자 없음 + UTF-8 로 인코딩 가능(lone surrogate 거절)."""
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(c) < FIRST_PRINTABLE or ord(c) == DEL for c in url)


def validate_remote_url(url: str) -> str:
    """허용된 모양이면 그대로 돌려준다. 아니면 InvalidState."""
    if not isinstance(url, str) or not url or url.startswith(OPTION_MARK):
        raise _refuse(url)
    if not _printable_utf8(url) or TRANSPORT_RE.match(url):
        raise _refuse(url)

    # scheme://authority/path — authority 는 user@host:port 만
    m = SCHEME_RE.match(url)
    if m:
        scheme, rest = m.group(1).lower(), m.group(2)
        authority = rest.split("/", 1)[0]
        if scheme not in ALLOWED_SCHEMES:
            raise _refuse(url)
        if authority == "" and scheme == LOCAL_SCHEME:
            return url
        if not AUTHORITY_RE.match(authority):
            raise _refuse(url)
        return url

    if url.startswith("/"):
        return url

    # scp 꼴 [user@]host:path — path 가 `-` 로 시작하면 원격 쪽 인자로 읽힐 수 있다
    scp = SCP_RE.match(url)
    if scp is None or scp.group(2).startswith(OPTION_MARK):
        raise _refuse(url)
    return url


def is_ssh_url(url: str) -> bool:
    """ssh 로 붙는 URL 인가(ssh·git+ssh scheme 또는 scp 꼴). transport(`x::`)는 아니다."""
    if TRANSPORT_RE.match(url):
        return False
    m = SCHEME_RE.match(url)
    if m:
        return m.group(1).lower() in SSH_SCHEMES
    return not url.startswith("/") and SCP_RE.match(url) is not None
