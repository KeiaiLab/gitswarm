"""--remote 가 없을 때 cwd 로 원격을 찾는다.

cwd ─┬─ 어떤 hive 의 wt/<id> 아래 ──▶ 그 hive 의 URL
     ├─ git 레포 안 ─────────────────▶ origin 의 URL
     └─ 둘 다 아님 ──────────────────▶ None (표면이 Usage 로 바꾼다)
"""

from __future__ import annotations

from pathlib import Path

from gitswarm.driver.git import Git
from gitswarm.store.hive import worktree_owner

ORIGIN = "origin"


def discover_remote(cwd: Path, home: Path) -> str | None:
    # hive worktree 의 origin 도 같은 URL 이지만, hive.toml 이 정본이고 git 호출이 없다
    return worktree_owner(cwd, home) or Git(cwd).remote_url(ORIGIN)
