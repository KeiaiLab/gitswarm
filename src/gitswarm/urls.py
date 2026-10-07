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
SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.DOTALL)
TRANSPORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")  # ext::, fd:: …
SCP_RE = re.compile(r"^(?:([^@/]*)@)?([^@/:]+):(.+)$", re.DOTALL)
OPTION_MARK = "-"
DEL = 0x7F
FIRST_PRINTABLE = 0x20


def _refuse(url: object) -> InvalidState:
    return InvalidState(f"invalid remote url: {url!r}")


def _host_of_authority(rest: str) -> str:
    """scheme:// 뒤 authority 의 host 부분(user@ 를 뗀 것)."""
    authority = rest.split("/", 1)[0]
    return authority.rpartition("@")[2]


def validate_remote_url(url: str) -> str:
    """허용된 모양이면 그대로 돌려준다. 아니면 InvalidState."""
    if not isinstance(url, str) or not url or url.startswith(OPTION_MARK):
        raise _refuse(url)
    if any(ord(c) < FIRST_PRINTABLE or ord(c) == DEL for c in url):
        raise _refuse(url)
    if TRANSPORT_RE.match(url):
        raise _refuse(url)

    # scheme://
    m = SCHEME_RE.match(url)
    if m:
        scheme, rest = m.groups()
        if scheme.lower() not in ALLOWED_SCHEMES or _host_of_authority(rest).startswith("-"):
            raise _refuse(url)
        return url

    if url.startswith("/"):
        return url

    # scp 꼴 — host 가 옵션 모양이면 ssh 인자로 읽힐 수 있다
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
