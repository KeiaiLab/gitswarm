"""--remote 가 없을 때 cwd(와 명령이 든 workspace id)로 원격을 찾는다.

cwd ─┬─ 어떤 hive 의 wt/<id> 아래 ──▶ 그 hive 의 URL
     ├─ git 레포 안 ─────────────────▶ origin 의 URL
     └─ 둘 다 아님 ─┬─ ws id 를 든 hive 하나 ──▶ 그 hive 의 URL
                    ├─ 둘 이상 ─────────────▶ AmbiguousRemoteError (표면이 Usage 로 바꾼다)
                    └─ 없음 ───────────────▶ None (표면이 Usage 로 바꾼다)

ULID 는 겹치지 않으니 hives/*/wt/<id> 가 하나면 그 hive 다. "home 에 hive 가 하나뿐"으로
고르지는 않는다 — 두 번째 hive 가 생기는 순간 같은 명령의 뜻이 바뀐다.
"""

from __future__ import annotations

from pathlib import Path

from gitswarm.driver.git import Git
from gitswarm.service.workspace import ULID_RE
from gitswarm.store.hive import worktree_hives, worktree_owner
from gitswarm.urls import validate_remote_url

ORIGIN = "origin"


class AmbiguousRemoteError(ValueError):
    """workspace id 가 여러 hive 에 있다 — 고르지 않는다."""


def discover_remote(cwd: Path, home: Path, ws_id: str | None = None) -> str | None:
    # hive worktree 의 origin 도 같은 URL 이지만, hive.toml 이 정본이고 git 호출이 없다
    found = worktree_owner(cwd, home) or Git(cwd).remote_url(ORIGIN) or _ws_owner(ws_id, home)
    # 남의 레포 origin 은 외부 입력이다 — git 에 넘기기 전에 허용 목록(gitswarm.urls)을 지난다
    return validate_remote_url(found) if found is not None else None


def _ws_owner(ws_id: str | None, home: Path) -> str | None:
    """wt/<ws_id> 를 가진 hive 의 URL. id 가 ULID 가 아니면 경로로 쓰지 않는다."""
    if ws_id is None or not ULID_RE.fullmatch(ws_id):
        return None

    owners = worktree_hives(ws_id, home)
    if len(owners) > 1:
        raise AmbiguousRemoteError(f"workspace {ws_id} found in {len(owners)} hives; pass --remote")
    return owners[0] if owners else None
